#!/usr/bin/env python3
"""probe_terms.py — probe candidate terms (tier 1: isolated-piece decomposition,
tier 2: corpus top-500) against the GLM 1301 content filter.

SAFETY CONTRACT (do not break):
  - Term content NEVER reaches stdout. stdout = counts/verdicts/ids/paths only.
  - Terms are written ONLY to results/term_verdicts_<ts>.jsonl (one JSON/line,
    flushed after every verdict — crash safe).
  - Env: WORK/.env (ZAI_API_KEY / ZAI_BASE_URL / ZAI_MODEL); values never printed.
  - blocked = status!=200 and ("1301" in body or "sensitive" in body.lower()).
  - 1.0s sleep between calls; single retry on network errors; hard cap 700 calls.
  - Sanity gate: bare carrier sentence must pass (HTTP 200) or the run aborts.

Call pattern mirrors scripts/bisect_blocked.py (headers x-api-key +
anthropic-version 2023-06-01; POST {model, max_tokens, messages}; timeout 120).

Run:
  python3 scripts/probe_terms.py 2>&1 | tee results/logs/probe_terms.log
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
WORK = HERE.parent
ENV_FILE = WORK / ".env"
ISOLATED_FILE = WORK / "vocab/isolated_triggers_20260830_084738.json"
CORPUS_FILE = WORK / "vocab/corpus_freq_top500.json"

CARRIER = (
    "The weather report for the coastal region mentioned strong winds and "
    "light rain expected by evening, with temperatures staying mild throughout."
)

CALL_CAP = 700  # hard cap on HTTP attempts (retries included)
SLEEP = 1.0  # seconds between API calls
MAX_TOKENS = 8192  # mirrors scripts/bisect_blocked.py
TIMEOUT = 120


def load_env() -> dict:
    """WORK/.env only; os.environ ZAI_* overrides, mirroring bisect_blocked."""
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
        self.cap_hit = False

    def call(self, prompt: str) -> tuple[bool, str]:
        """One HTTP attempt; single retry on network-layer errors only."""
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
        if r is None:  # unreachable, but keeps the type checker honest
            return False, "net-err-no-response"
        if r.status_code == 200:
            return False, "ok"
        body = r.text[:300]  # inspected in-memory only, never returned/printed
        blocked = ("1301" in body) or ("sensitive" in body.lower())
        return blocked, f"HTTP{r.status_code}{'-1301' if blocked else ''}"

    def probe_term(self, term: str) -> tuple[bool | None, str]:
        """None verdict => cap hit or persistent net error (could not test)."""
        if self.calls >= CALL_CAP:
            self.cap_hit = True
            return None, "cap"
        blocked, detail = self.call(CARRIER + " " + term)
        time.sleep(SLEEP)
        if detail.startswith("net-err"):
            return None, detail
        return blocked, detail


def tier1_items() -> list[tuple[str, str, str]]:
    """(term, label, src_id) — full/token(len>=3)/bigram/trigram, deduped."""
    data = json.loads(ISOLATED_FILE.read_text())
    items: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for res in data["results"]:
        src = str(res["id"])
        for s in res["isolated_minimal"]:
            toks = s.split()
            cands: list[tuple[str, str]] = [("full", s)]
            cands += [("token", t) for t in toks if len(t) >= 3]
            cands += [
                ("bigram", " ".join(toks[i : i + 2])) for i in range(len(toks) - 1)
            ]
            cands += [
                ("trigram", " ".join(toks[i : i + 3])) for i in range(len(toks) - 2)
            ]
            for label, term in cands:
                if term in seen:
                    continue
                seen.add(term)
                items.append((term, label, src))
    return items


def main() -> int:
    t0 = time.time()
    env = load_env()
    missing = [
        k for k in ("ZAI_API_KEY", "ZAI_BASE_URL", "ZAI_MODEL") if not env.get(k)
    ]
    if missing:
        print(f"FATAL: missing env keys: {missing}")
        return 2

    pr = Probe(env)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = WORK / "results" / f"term_verdicts_{ts}.jsonl"
    fh = open(out_path, "a", encoding="utf-8")

    def rec(
        tier: int, label: str, lst: str, term: str, blocked: bool, detail: str
    ) -> None:
        fh.write(
            json.dumps(
                {
                    "tier": tier,
                    "label": label,
                    "list": lst,
                    "term": term,
                    "blocked": bool(blocked),
                    "detail": detail,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        fh.flush()  # + fsync => line survives process death
        os.fsync(fh.fileno())

    # ── sanity gate: bare carrier must pass ─────────────────────────────
    blocked, detail = pr.call(CARRIER)
    time.sleep(SLEEP)
    rec(0, "sanity", "none", CARRIER, blocked, detail)
    if blocked:
        print("SANITY: BLOCKED -> ABORT")
        print(
            f"TOTAL calls={pr.calls} wall={time.time() - t0:.0f}s out={out_path.relative_to(WORK)}"
        )
        return 1
    if detail.startswith("net-err"):
        print("SANITY: net-err -> ABORT")
        return 1
    print("SANITY: pass")

    # ── tier 1: isolated-piece decomposition ────────────────────────────
    t1_by_label: dict[str, list[int]] = {}
    t1_tested = t1_blocked = t1_neterr = t1_cap = 0
    for term, label, src in tier1_items():
        verdict, detail = pr.probe_term(term)
        if verdict is None:
            if detail == "cap":
                t1_cap += 1
                break
            t1_neterr += 1
            rec(1, label, src, term, False, detail)
            continue
        rec(1, label, src, term, verdict, detail)
        t1_tested += 1
        t1_blocked += int(verdict)
        st = t1_by_label.setdefault(label, [0, 0])
        st[0] += 1
        st[1] += int(verdict)
    print(
        f"TIER1 tested={t1_tested} blocked={t1_blocked} "
        f"net_err={t1_neterr} cap_skipped={t1_cap}"
    )
    for label in ("full", "token", "bigram", "trigram"):
        if label in t1_by_label:
            a, b = t1_by_label[label]
            print(f"TIER1_BY_LABEL {label} tested={a} blocked={b}")

    # ── tier 2: corpus top-500 ──────────────────────────────────────────
    corpus = json.loads(CORPUS_FILE.read_text())
    t2_by_list: dict[str, list[int]] = {}
    t2_tested = t2_blocked = t2_neterr = t2_cap = 0
    t2_dupes = 0
    seen_t2: set[str] = set()
    for entry in corpus:
        term = entry["term"]
        lst = entry["list"]
        if term in seen_t2:
            t2_dupes += 1
            continue
        seen_t2.add(term)
        verdict, detail = pr.probe_term(term)
        if verdict is None:
            if detail == "cap":
                t2_cap += 1
                break
            t2_neterr += 1
            rec(2, "corpus", lst, term, False, detail)
            continue
        rec(2, "corpus", lst, term, verdict, detail)
        t2_tested += 1
        t2_blocked += int(verdict)
        st = t2_by_list.setdefault(lst, [0, 0])
        st[0] += 1
        st[1] += int(verdict)
    print(
        f"TIER2 tested={t2_tested} blocked={t2_blocked} "
        f"net_err={t2_neterr} cap_skipped={t2_cap} dupes_skipped={t2_dupes}"
    )
    for lst in sorted(t2_by_list):
        a, b = t2_by_list[lst]
        print(f"TIER2_BY_LIST {lst} tested={a} blocked={b}")

    print(f"CAP hit={pr.cap_hit} calls={pr.calls}/{CALL_CAP}")
    print(
        f"TOTAL calls={pr.calls} net_errs={pr.net_errs} "
        f"wall={time.time() - t0:.0f}s out={out_path.relative_to(WORK)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
