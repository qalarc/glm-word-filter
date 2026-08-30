#!/usr/bin/env python3
"""verify_lexicon_v1.py — bulk-verify vocab/antiscrape_lexicon.json entries
against the live Z.AI 1301 content filter, then merge with ALL prior verdict
sources into results/VERIFIED_BLOCKERS_ALL.json.

SAFETY CONTRACT (do not break):
  - Term content NEVER reaches stdout. stdout = the counts json ONLY.
  - Progress/status lines -> stderr (counts/paths only, zero content).
  - Per-term verdicts -> results/lexicon_verdicts_v1.jsonl
      {"term","category","blocked","detail"} — fsync after every line.
  - Merge file -> results/VERIFIED_BLOCKERS_ALL.json (full term lists, file only).
  - Counts -> results/logs/verify_v1_counts.json; same json echoed to stdout.
  - Carrier EXACTLY as in scripts/probe_terms.py; message = carrier + " " + term.
  - blocked = status!=200 and ("1301" in body or "sensitive" in body.lower()).
  - Env: WORK/.env ZAI_* (os.environ overrides), never printed.
  - 1.0s sleep between calls; 1 retry on network-layer errors only.
  - Abort on 5 consecutive non-1301 4xx/5xx (filter/API drift) -> counts file.
  - Sanity gate: bare carrier must pass (HTTP 200) or the run aborts.
  - Hard cap 1400 HTTP calls (sanity + retries included).

Skip-set (previously tested terms, blocked or not):
  - results/term_verdicts_*.jsonl          (field "term"; tier-0 sanity excluded)
  - results/pool_verdicts_*.jsonl          (field "term")
  - chanalyse probe_results_20260830_083503.json synthetic[].term

Test list: lexicon entries (flatten zh/en/phrases subcategory lists, dedupe,
first category wins) MINUS skip-set, random.Random(20260830).shuffle order.
Run:  python3 scripts/verify_lexicon_v1.py [--plan]   # --plan: no HTTP, counts of the plan
"""

from __future__ import annotations

import glob
import json
import os
import random
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
WORK = HERE.parent
ENV_FILE = WORK / ".env"
LEXICON_FILE = WORK / "vocab/antiscrape_lexicon.json"
V1_OUT = WORK / "results/lexicon_verdicts_v1.jsonl"
MERGE_OUT = WORK / "results/VERIFIED_BLOCKERS_ALL.json"
COUNTS_OUT = WORK / "results/logs/verify_v1_counts.json"
CHANALYSE = (
    Path.home()
    / "projects/MASTER_PROJECTS/chanalyse/ops/filter_vocab/probe_results_20260830_083503.json"
)

CARRIER = (
    "The weather report for the coastal region mentioned strong winds and "
    "light rain expected by evening, with temperatures staying mild throughout."
)

CALL_CAP = 1400  # hard cap on HTTP attempts (sanity + retries included)
SLEEP = 1.0
MAX_TOKENS = 8192
TIMEOUT = 120
SEED = 20260830
DRIFT_LIMIT = 5  # consecutive non-1301 4xx/5xx responses -> abort


def log(msg: str) -> None:
    """Status to stderr ONLY — never term content."""
    print(msg, file=sys.stderr, flush=True)


def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    for k in ("ZAI_API_KEY", "ZAI_BASE_URL", "ZAI_MODEL"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


class Probe:
    """Sanitized API probe: (blocked, detail) — detail never contains content."""

    def __init__(self, env: dict):
        self.headers = {
            "x-api-key": env["ZAI_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        self.base, self.model = env["ZAI_BASE_URL"], env["ZAI_MODEL"]
        self.calls = 0
        self.net_errs = 0
        self.consec_http_err = 0
        self.cap_hit = False
        self.drift_abort = False

    def call(self, prompt: str) -> tuple[bool, str]:
        """One HTTP attempt (single retry on network errors only).

        Returns (blocked, detail). Tracks consecutive non-1301 4xx/5xx.
        """
        r: httpx.Response | None = None
        for attempt in (0, 1):
            self.calls += 1
            try:
                r = httpx.post(
                    self.base,
                    json={
                        "model": self.model,
                        "max_tokens": MAX_TOKENS,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    headers=self.headers,
                    timeout=TIMEOUT,
                )
                break
            except Exception as e:  # network-layer error
                if attempt == 1:
                    self.net_errs += 1
                    return False, f"net-err-{type(e).__name__}"
                time.sleep(2.0)
        if r is None:
            return False, "net-err-no-response"
        if r.status_code == 200:
            self.consec_http_err = 0
            return False, "ok"
        body = r.text[:300]  # inspected in-memory only
        blocked = ("1301" in body) or ("sensitive" in body.lower())
        if blocked:
            self.consec_http_err = 0
            return True, f"HTTP{r.status_code}-1301"
        self.consec_http_err += 1
        if self.consec_http_err >= DRIFT_LIMIT:
            self.drift_abort = True
        return False, f"HTTP{r.status_code}"

    def probe(self, term: str) -> tuple[bool | None, str]:
        """None verdict => cap hit or persistent net error (could not test)."""
        if self.calls >= CALL_CAP:
            self.cap_hit = True
            return None, "cap"
        blocked, detail = self.call(CARRIER + " " + term)
        time.sleep(SLEEP)
        if detail.startswith("net-err"):
            return None, detail
        return blocked, detail


def iter_jsonl(path: str):
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def load_prior() -> tuple[set[str], dict[str, set[str]], set[str]]:
    """skip-set, {pool: blocked_terms, prior: blocked_terms}, tested_all."""
    skip: set[str] = set()
    tested_all: set[str] = set()
    blocked: dict[str, set[str]] = {"pool": set(), "prior": set()}

    # own partial outputs from earlier runs count as tested too (append-mode
    # resume: never re-probe a term that already has a verdict line)
    for f in sorted(glob.glob(str(WORK / "results/lexicon_verdicts_v*.jsonl"))):
        for rec in iter_jsonl(f):
            t = rec.get("term")
            if not t:
                continue
            skip.add(t)
            tested_all.add(t)
            if rec.get("blocked") is True:
                blocked["prior"].add(t)

    for f in sorted(glob.glob(str(WORK / "results/term_verdicts_*.jsonl"))):
        for rec in iter_jsonl(f):
            if rec.get("tier") == 0 or rec.get("label") == "sanity":
                continue  # carrier sanity record, not a term
            t = rec.get("term")
            if not t:
                continue
            skip.add(t)
            tested_all.add(t)
            if rec.get("blocked") is True:
                blocked["prior"].add(t)

    for f in sorted(glob.glob(str(WORK / "results/pool_verdicts_*.jsonl"))):
        for rec in iter_jsonl(f):
            t = rec.get("term")
            if not t:
                continue
            skip.add(t)
            tested_all.add(t)
            if rec.get("blocked") is True:
                blocked["pool"].add(t)

    if CHANALYSE.exists():
        d = json.loads(CHANALYSE.read_text())
        for e in d.get("synthetic", []):
            t = e.get("term")
            if not t:
                continue
            skip.add(t)
            tested_all.add(t)
            if e.get("blocked") is True:
                blocked["prior"].add(t)

    return skip, blocked, tested_all


def lexicon_items() -> list[tuple[str, str]]:
    """(term, top-level-category) — flatten zh/en/phrases, dedupe, first wins."""
    lx = json.loads(LEXICON_FILE.read_text())
    items: list[tuple[str, str]] = []
    seen: set[str] = set()
    for cat in ("zh", "en", "phrases"):
        sub = lx.get(cat)
        if not isinstance(sub, dict):
            continue
        for _subcat, lst in sub.items():
            if not isinstance(lst, list):
                continue
            for t in lst:
                if isinstance(t, str) and t and t not in seen:
                    seen.add(t)
                    items.append((t, cat))
    return items


def merge_all() -> dict:
    """Merge this run + all prior sources into VERIFIED_BLOCKERS_ALL.json."""
    v1_blocked: set[str] = set()
    v1_tested: set[str] = set()
    if V1_OUT.exists():
        for rec in iter_jsonl(str(V1_OUT)):
            t = rec.get("term")
            if not t:
                continue
            v1_tested.add(t)
            if rec.get("blocked") is True:
                v1_blocked.add(t)

    _skip, prior_blk, tested_prior = load_prior()
    verified = sorted(v1_blocked | prior_blk["pool"] | prior_blk["prior"])
    tested_total = len(v1_tested | tested_prior)
    doc = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "verified_single": verified,
        "by_source": {
            "v1_lexicon": len(v1_blocked),
            "pool": len(prior_blk["pool"]),
            "prior": len(prior_blk["prior"]),
        },
        "tested_total": tested_total,
        "clean": tested_total - len(verified),
    }
    MERGE_OUT.write_text(
        json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    return doc


def write_counts(
    counts: dict, pr: Probe | None, t0: float, aborted: str | None
) -> None:
    counts["calls"] = pr.calls if pr is not None else counts.get("calls", 0)
    counts["wall_s"] = round(time.time() - t0)
    if pr is not None and pr.net_errs:
        counts["net_errs"] = pr.net_errs
    if pr is not None and pr.cap_hit:
        counts["cap_hit"] = True
    if aborted:
        counts["aborted_reason"] = aborted
    if "--plan" not in sys.argv:  # plan mode: no counts file (keeps polling clean)
        COUNTS_OUT.parent.mkdir(parents=True, exist_ok=True)
        COUNTS_OUT.write_text(
            json.dumps(counts, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
    print(json.dumps(counts, ensure_ascii=False))  # stdout: counts ONLY


def main() -> int:
    t0 = time.time()
    counts: dict = {
        "tested": 0,
        "blocked": 0,
        "by_category": {},
        "prior_skipped": 0,
        "calls": 0,
        "wall_s": 0,
    }
    pr: Probe | None = None

    env = load_env()
    missing = [
        k for k in ("ZAI_API_KEY", "ZAI_BASE_URL", "ZAI_MODEL") if not env.get(k)
    ]
    if missing:
        log(f"FATAL: missing env keys: {missing}")
        write_counts(counts, pr, t0, "env_missing")
        return 2

    skip, _prior_blk, _tested = load_prior()
    items = lexicon_items()
    counts["prior_skipped"] = sum(1 for t, _c in items if t in skip)
    test_items = [(t, c) for (t, c) in items if t not in skip]
    random.Random(SEED).shuffle(test_items)
    log(
        f"PLAN lexicon_unique={len(items)} prior_skipped={counts['prior_skipped']} "
        f"to_test={len(test_items)} cap={CALL_CAP} seed={SEED}"
    )
    if "--plan" in sys.argv:
        write_counts(counts, pr, t0, None)
        return 0

    pr = Probe(env)

    # ── sanity gate: bare carrier must pass ──────────────────────────────
    s_blocked, s_detail = pr.call(CARRIER)
    time.sleep(SLEEP)
    log(f"SANITY {'blocked' if s_blocked else s_detail}")
    if s_blocked:
        merge_all()
        write_counts(counts, pr, t0, "sanity_blocked")
        return 1
    if s_detail.startswith("net-err"):
        merge_all()
        write_counts(counts, pr, t0, "sanity_net_err")
        return 1

    aborted: str | None = None
    fh = open(V1_OUT, "a", encoding="utf-8")

    def rec(term: str, category: str, blocked: bool, detail: str) -> None:
        fh.write(
            json.dumps(
                {
                    "term": term,
                    "category": category,
                    "blocked": bool(blocked),
                    "detail": detail,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        fh.flush()
        os.fsync(fh.fileno())  # line survives process death

    for term, cat in test_items:
        verdict, detail = pr.probe(term)
        if verdict is None:
            if detail == "cap":
                break
            rec(term, cat, False, detail)  # net-err: attempted, unknown verdict
            continue
        rec(term, cat, verdict, detail)
        counts["tested"] += 1
        counts["blocked"] += int(verdict)
        st = counts["by_category"].setdefault(cat, {"tested": 0, "blocked": 0})
        st["tested"] += 1
        st["blocked"] += int(verdict)
        if counts["tested"] % 25 == 0:
            log(
                f"PROGRESS tested={counts['tested']} blocked={counts['blocked']} "
                f"calls={pr.calls} elapsed={int(time.time() - t0)}s"
            )
        if pr.drift_abort:
            aborted = "five_consecutive_non1301_http_err"
            log(f"ABORT {aborted} calls={pr.calls}")
            break

    fh.close()
    log(
        f"LOOP done tested={counts['tested']} blocked={counts['blocked']} "
        f"calls={pr.calls} cap_hit={pr.cap_hit} aborted={aborted}"
    )

    merged = merge_all()
    log(
        f"MERGE verified={len(merged['verified_single'])} "
        f"by_source={merged['by_source']} tested_total={merged['tested_total']} "
        f"clean={merged['clean']} -> {MERGE_OUT.relative_to(WORK)}"
    )
    write_counts(counts, pr, t0, aborted)
    return 3 if aborted else 0


if __name__ == "__main__":
    sys.exit(main())
