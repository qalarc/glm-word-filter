#!/usr/bin/env python3
r"""
corpus_freq.py — rank filter-candidate terms by real-world occurrence in a
forum-thread corpus (sqlite, read-only).

SAFETY CONTRACT:
  - The corpus and candidate lists contain hate speech / political content.
  - NO term text is EVER printed to stdout or to the log file. Files only.
  - stdout and results/logs/corpus_freq.log carry counts, IDs and timings ONLY.
  - DB is opened read-only via URI (mode=ro).

Outputs (FILE ONLY, term text permitted inside files):
  vocab/corpus_freq.json        {"pol": [...threads>=2, sorted...], "nonpol": [top 200]}
  vocab/corpus_freq_top500.json bare list, top 500 by (threads desc, occurrences desc)

Matching rule (per spec):
  - case-insensitive (both sides lowercased)
  - terms with len >= 4 : plain substring matching
  - terms with len < 4  : word-boundary match  (?<!\w)TERM(?!\w)

Method: per board-group, concatenate all lowercased per-thread texts into one
big string with a NUL separator; record start offsets; use C-speed str.find /
regex finditer and map match positions to thread indices with a forward cursor
(positions arrive in non-decreasing order, so O(1) amortized per match).
"""

import bisect  # noqa: F401  (kept: forward cursor replaces it, see mapper())
import json
import logging
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import date, timedelta

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.expanduser("~/.cache/chanalyse/gmktec.db")
ONLINE_RAW = os.path.join(WORKDIR, "data", "online_raw.json")
CANDIDATE_POOL = os.path.join(WORKDIR, "vocab", "candidate_pool.json")
ISOLATED = os.path.join(WORKDIR, "vocab", "isolated_triggers_20260830_084738.json")
OUT_FREQ = os.path.join(WORKDIR, "vocab", "corpus_freq.json")
OUT_TOP500 = os.path.join(WORKDIR, "vocab", "corpus_freq_top500.json")
LOG_PATH = os.path.join(WORKDIR, "results", "logs", "corpus_freq.log")

SEP = "\n\x00\n"  # thread separator inside the big string
NONPOL_LIMIT = 500
WINDOW_DAYS = 14

log = logging.getLogger("corpus_freq")


def setup_logging() -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    log.setLevel(logging.INFO)
    if not log.handlers:
        fh = logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(fh)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(sh)
    log.info("=== run start ===")


# --------------------------------------------------------------------------- #
# Corpus loading
# --------------------------------------------------------------------------- #
def load_threads() -> tuple[list[str], list[str], str]:
    """Read-only pull of pol (last N days) + non-pol sample.

    Returns (pol_texts_lower, nonpol_texts_lower, cutoff) — never logged raw.
    """
    cutoff = (date.today() - timedelta(days=WINDOW_DAYS)).isoformat()
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    cur = con.cursor()
    t0 = time.time()
    pol_rows = cur.execute(
        """
        SELECT COALESCE(original_subject, ''),
               COALESCE(NULLIF(TRIM(COALESCE(original_comment, '')), ''),
                        COALESCE(cleaned_text, ''))
        FROM threads
        WHERE board = 'pol' AND first_seen_ts >= ?
        """,
        (cutoff,),
    ).fetchall()
    t1 = time.time()
    nonpol_rows = cur.execute(
        """
        SELECT COALESCE(original_subject, ''),
               COALESCE(NULLIF(TRIM(COALESCE(original_comment, '')), ''),
                        COALESCE(cleaned_text, ''))
        FROM threads
        WHERE board != 'pol'
        ORDER BY first_seen_ts DESC
        LIMIT ?
        """,
        (NONPOL_LIMIT,),
    ).fetchall()
    t2 = time.time()
    con.close()

    pol = [f"{s}\n{c}".lower() for s, c in pol_rows]
    nonpol = [f"{s}\n{c}".lower() for s, c in nonpol_rows]
    log.info(
        "db pull ok: pol threads=%d (%.1fs), nonpol threads=%d (%.1fs), cutoff=%s",
        len(pol),
        t1 - t0,
        len(nonpol),
        t2 - t1,
        cutoff,
    )
    return pol, nonpol, cutoff


def mb(texts: list[str]) -> float:
    return round(sum(len(t.encode("utf-8", "replace")) for t in texts) / 1e6, 2)


class Corpus:
    """One board-group corpus: big concatenated string + thread offsets."""

    def __init__(self, texts: list[str]):
        self.n = len(texts)
        self.bounds: list[int] = []
        pos = 0
        for t in texts:
            self.bounds.append(pos)
            pos += len(t) + len(SEP)
        self.big = SEP.join(texts)
        self.mb = mb(texts)

    @staticmethod
    def _mapper(bounds: list[int]):
        """Forward-cursor position -> thread index (positions non-decreasing)."""
        idx = 0
        n = len(bounds)

        def f(p: int) -> int:
            nonlocal idx
            while idx + 1 < n and p >= bounds[idx + 1]:
                idx += 1
            return idx

        return f

    def scan_substring(self, term: str) -> tuple[int, int]:
        """(threads containing term, total occurrences) via str.find."""
        big, bounds = self.big, self.bounds
        f = self._mapper(bounds)
        thr = occ = 0
        last = -1
        L = len(term)
        pos = big.find(term)
        while pos != -1:
            occ += 1
            ti = f(pos)
            if ti != last:
                thr += 1
                last = ti
            pos = big.find(term, pos + L)
        return thr, occ

    def scan_regex(self, pat: re.Pattern) -> tuple[int, int]:
        big, bounds = self.big, self.bounds
        f = self._mapper(bounds)
        thr = occ = 0
        last = -1
        for m in pat.finditer(big):
            occ += 1
            ti = f(m.start())
            if ti != last:
                thr += 1
                last = ti
        return thr, occ


# --------------------------------------------------------------------------- #
# Candidates
# --------------------------------------------------------------------------- #
def load_sources() -> dict[str, list[str]]:
    sources: dict[str, list[str]] = {}

    with open(ONLINE_RAW, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    for name, terms in raw.items():
        cleaned = [t for t in (str(x).strip() for x in terms) if t]
        sources[str(name)] = cleaned
    log.info(
        "online_raw loaded: lists=%d terms=%d",
        len(raw),
        sum(len(v) for v in raw.values()),
    )

    if os.path.exists(CANDIDATE_POOL):
        try:
            with open(CANDIDATE_POOL, "r", encoding="utf-8") as fh:
                cp = json.load(fh)
            items = cp.get("candidates", []) if isinstance(cp, dict) else cp
            terms: list[str] = []
            for it in items:
                if isinstance(it, str):
                    terms.append(it.strip())
                elif isinstance(it, dict):
                    for k in ("term", "text", "value", "candidate"):
                        v = it.get(k)
                        if isinstance(v, str) and v.strip():
                            terms.append(v.strip())
                            break
            sources["candidate_pool"] = [t for t in terms if t]
            log.info("candidate_pool loaded: terms=%d", len(sources["candidate_pool"]))
        except Exception as e:  # graceful: keep going without the pool
            log.warning(
                "candidate_pool present but unreadable (%s: %s) — skipped",
                type(e).__name__,
                e,
            )
    else:
        log.info("candidate_pool not present — skipped (graceful)")
    return sources


def build_unique(
    sources: dict[str, list[str]],
) -> tuple[dict[str, list[str]], list[str]]:
    owners: dict[str, list[str]] = {}
    order: list[str] = []
    skipped = 0
    for name, terms in sources.items():
        for t in terms:
            tl = t.lower()
            if not tl or "\x00" in tl:
                skipped += 1
                continue
            entry = owners.get(tl)
            if entry is None:
                owners[tl] = [name]
                order.append(tl)
            elif name not in entry:
                entry.append(name)
    if skipped:
        log.info("candidate terms skipped (empty/NUL): %d", skipped)
    return owners, order


# --------------------------------------------------------------------------- #
# Isolated triggers (values never printed)
# --------------------------------------------------------------------------- #
def load_isolated_triggers() -> list[tuple[int, str]]:
    """Return [(result_id, trigger_lower), ...] — ids only, no values logged."""
    with open(ISOLATED, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    results = data.get("results", []) if isinstance(data, dict) else data
    out: list[tuple[int, str]] = []
    for i, item in enumerate(results):
        trig = None
        rid = item.get("id", i) if isinstance(item, dict) else i
        if isinstance(item, dict):
            for key in ("isolated_minimal", "blocking_windows"):
                v = item.get(key)
                if isinstance(v, list):
                    for el in v:
                        if isinstance(el, str) and el.strip():
                            trig = el.strip()
                            break
                elif isinstance(v, str) and v.strip():
                    trig = v.strip()
                if trig:
                    break
        if trig is None:
            log.warning("isolated trigger result[%d]: no usable substring found", i)
            continue
        out.append((rid, trig.lower()))
    return out


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    setup_logging()
    t_start = time.time()

    pol_texts, nonpol_texts, cutoff = load_threads()
    pol = Corpus(pol_texts)
    nonpol = Corpus(nonpol_texts)
    del pol_texts, nonpol_texts
    log.info(
        "corpus built: pol threads=%d mb=%.2f | nonpol threads=%d mb=%.2f",
        pol.n,
        pol.mb,
        nonpol.n,
        nonpol.mb,
    )

    sources = load_sources()
    owners, order = build_unique(sources)
    n_sub = sum(1 for t in order if len(t) >= 4)
    n_re = len(order) - n_sub
    log.info(
        "unique candidates=%d (substring>=4ch: %d, word-boundary<4ch: %d)",
        len(order),
        n_sub,
        n_re,
    )

    # ---- scan both corpora -------------------------------------------------
    pol_hits: dict[str, tuple[int, int]] = {}
    nonpol_hits: dict[str, tuple[int, int]] = {}
    t0 = time.time()
    for i, term in enumerate(order):
        if len(term) >= 4:
            p = pol.scan_substring(term)
            q = nonpol.scan_substring(term)
        else:
            pat = re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)")
            p = pol.scan_regex(pat)
            q = nonpol.scan_regex(pat)
        if p[0]:
            pol_hits[term] = p
        if q[0]:
            nonpol_hits[term] = q
        if (i + 1) % 2500 == 0:
            log.info(
                "scan progress: %d/%d terms, %.1fs", i + 1, len(order), time.time() - t0
            )
    log.info(
        "scan done: %d terms in %.1fs | pol matched terms=%d nonpol matched=%d",
        len(order),
        time.time() - t0,
        len(pol_hits),
        len(nonpol_hits),
    )

    # ---- rank & write ------------------------------------------------------
    def item(t: str) -> dict:
        th, oc = pol_hits[t]
        return {
            "term": t,
            "list": "|".join(owners[t]),
            "threads": th,
            "occurrences": oc,
        }

    pol_qual = [t for t, (th, _) in pol_hits.items() if th >= 2]
    pol_qual.sort(key=lambda t: (-pol_hits[t][0], -pol_hits[t][1], t))
    pol_out = [item(t) for t in pol_qual]

    nonpol_all = sorted(
        nonpol_hits, key=lambda t: (-nonpol_hits[t][0], -nonpol_hits[t][1], t)
    )
    nonpol_out = []
    for t in nonpol_all[:200]:
        th, oc = nonpol_hits[t]
        nonpol_out.append(
            {"term": t, "list": "|".join(owners[t]), "threads": th, "occurrences": oc}
        )

    top500 = pol_out[:500]

    with open(OUT_FREQ, "w", encoding="utf-8") as fh:
        json.dump({"pol": pol_out, "nonpol": nonpol_out}, fh, ensure_ascii=False)
    with open(OUT_TOP500, "w", encoding="utf-8") as fh:
        json.dump(top500, fh, ensure_ascii=False)
    log.info(
        "wrote %s (pol=%d nonpol=%d) and %s (n=%d)",
        os.path.basename(OUT_FREQ),
        len(pol_out),
        len(nonpol_out),
        os.path.basename(OUT_TOP500),
        len(top500),
    )

    # ---- isolated trigger prevalence on pol corpus --------------------------
    trig_stats = []
    for rid, trig in load_isolated_triggers():
        th, oc = pol.scan_substring(trig)
        trig_stats.append((rid, th, oc))
        log.info(
            "isolated trigger id=%s: pol threads=%d occurrences=%d (%.2f%% of %d)",
            rid,
            th,
            oc,
            100.0 * th / pol.n if pol.n else 0.0,
            pol.n,
        )

    # ------------------------------------------------------------------ #
    # STDOUT REPORT — counts only, zero term text
    # ------------------------------------------------------------------ #
    print(
        "[1] corpus: pol_14d threads=%d mb=%.2f | nonpol threads=%d mb=%.2f"
        % (pol.n, pol.mb, nonpol.n, nonpol.mb)
    )
    print(
        "[2] candidates: online_raw lists=%d | candidate_pool present=%s | "
        "unique=%d (substring>=4ch=%d, boundary<4ch=%d)"
        % (
            len(sources) - (1 if "candidate_pool" in sources else 0),
            "yes" if "candidate_pool" in sources else "no",
            len(order),
            n_sub,
            n_re,
        )
    )

    print("[3] per-source-list qualifying (>=2 pol threads):")
    for name, terms in sources.items():
        qual = sum(1 for t in terms if pol_hits.get(t.lower(), (0, 0))[0] >= 2)
        print("    %s: %d/%d" % (name, qual, len(terms)))
    print("[4] total unique candidates qualifying: %d" % len(pol_qual))

    top10_hist = dict(sorted(Counter(len(t) for t in pol_qual[:10]).items()))
    all_hist = dict(sorted(Counter(len(t) for t in pol_qual).items()))
    print("[5] top-10 term length histogram: %s" % top10_hist)
    print("    all-qualifying length histogram: %s" % all_hist)

    print("[6] isolated triggers prevalence (pol 14d, %d threads):" % pol.n)
    for rid, th, oc in trig_stats:
        print(
            "    trigger[id=%s]: threads=%d occurrences=%d (%.2f%%)"
            % (rid, th, oc, 100.0 * th / pol.n if pol.n else 0.0)
        )

    print(
        "[7] outputs: %s (pol=%d, nonpol=%d) | %s (n=%d) | elapsed=%.1fs | cutoff=%s"
        % (
            os.path.basename(OUT_FREQ),
            len(pol_out),
            len(nonpol_out),
            os.path.basename(OUT_TOP500),
            len(top500),
            time.time() - t_start,
            cutoff,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
