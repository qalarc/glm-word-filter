#!/usr/bin/env python3
"""verify_lexicon_v1_gap.py — probe the lexicon entries that the main
verify_lexicon_v1.py run cannot see.

WHY: the main script flattens only the top-level categories
zh/en/phrases (dict-of-lists each). vocab/antiscrape_lexicon.json has
exactly ONE additional list-bearing top-level category (2 entries) that
those three do not contain (verified: 0 dups of trio). The task spec
says "flatten every list-valued category", so those entries must be
probed too.

SAFETY: identical contract to verify_lexicon_v1.py — Probe class is
IMPORTED from it (same carrier, same blocked rule, same retry/drift).
Term content never reaches stdout; records append to the SAME
results/lexicon_verdicts_v1.jsonl; afterwards merge_all() from the main
module refreshes results/VERIFIED_BLOCKERS_ALL.json so the gap terms are
folded into the union. A tiny counts file goes to
results/logs/verify_v1_gap_counts.json. by_category keys are anonymized
as other_cat_N (the real category name is kept only inside the jsonl).

MUST be run AFTER the main verify_lexicon_v1.py process has exited.
Run: python3 scripts/verify_lexicon_v1_gap.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_lexicon_v1 import (  # noqa: E402
    CALL_CAP,
    V1_OUT,
    Probe,
    load_env,
    load_prior,
    merge_all,
)

WORK = Path(__file__).resolve().parent.parent
LEXICON_FILE = WORK / "vocab/antiscrape_lexicon.json"
GAP_COUNTS = WORK / "results/logs/verify_v1_gap_counts.json"
COVERED = {"zh", "en", "phrases"}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def gap_items() -> list[tuple[str, str]]:
    """(term, real_category) from every list-bearing top-level category
    outside COVERED, deduped, also dropping anything already inside the
    trio (paranoia; overlap already verified = 0)."""
    lx = json.loads(LEXICON_FILE.read_text())
    trio: set[str] = set()
    for cat in COVERED:
        sub = lx.get(cat)
        if isinstance(sub, dict):
            for lst in sub.values():
                if isinstance(lst, list):
                    trio.update(t for t in lst if isinstance(t, str) and t)
    items: list[tuple[str, str]] = []
    seen: set[str] = set()
    anon: dict[str, str] = {}
    for key, val in lx.items():
        if key in COVERED or not isinstance(val, dict):
            continue
        for lst in val.values():
            if not isinstance(lst, list):
                continue
            for t in lst:
                if isinstance(t, str) and t and t not in trio and t not in seen:
                    seen.add(t)
                    anon.setdefault(key, f"other_cat_{len(anon) + 1}")
                    items.append((t, anon[key]))
    return items


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
    env = load_env()
    if not all(env.get(k) for k in ("ZAI_API_KEY", "ZAI_BASE_URL", "ZAI_MODEL")):
        counts["aborted_reason"] = "env_missing"
        GAP_COUNTS.write_text(json.dumps(counts, indent=1) + "\n")
        print(json.dumps(counts))
        return 2

    skip, _blk, _tested = load_prior()
    items = gap_items()
    todo = [(t, c) for (t, c) in items if t not in skip]
    counts["prior_skipped"] = len(items) - len(todo)
    log(
        f"GAP PLAN found={len(items)} prior_skipped={counts['prior_skipped']} to_test={len(todo)} cap={CALL_CAP}"
    )

    if not todo:
        counts["wall_s"] = round(time.time() - t0)
        GAP_COUNTS.write_text(json.dumps(counts, indent=1) + "\n")
        print(json.dumps(counts))
        return 0

    pr = Probe(env)
    # sanity: bare carrier must pass before probing gap terms
    s_blocked, s_detail = pr.call(
        "The weather report for the coastal region mentioned strong winds and "
        "light rain expected by evening, with temperatures staying mild throughout."
    )
    time.sleep(1.0)
    if s_blocked or s_detail.startswith("net-err"):
        counts["aborted_reason"] = "sanity_failed"
        counts["calls"] = pr.calls
        GAP_COUNTS.write_text(json.dumps(counts, indent=1) + "\n")
        print(json.dumps(counts))
        return 1

    fh = open(V1_OUT, "a", encoding="utf-8")
    aborted: str | None = None
    for term, cat in todo:
        verdict, detail = pr.probe(term)
        if verdict is None:
            if detail == "cap":
                break
            rec = {"term": term, "category": cat, "blocked": False, "detail": detail}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
            continue
        rec = {
            "term": term,
            "category": cat,
            "blocked": bool(verdict),
            "detail": detail,
        }
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
        counts["tested"] += 1
        counts["blocked"] += int(verdict)
        st = counts["by_category"].setdefault(cat, {"tested": 0, "blocked": 0})
        st["tested"] += 1
        st["blocked"] += int(verdict)
        if pr.drift_abort:
            aborted = "five_consecutive_non1301_http_err"
            break
    fh.close()
    counts["calls"] = pr.calls
    counts["wall_s"] = round(time.time() - t0)
    if aborted:
        counts["aborted_reason"] = aborted

    merged = merge_all()  # refresh union incl. gap terms
    log(
        f"GAP MERGE verified={len(merged['verified_single'])} tested_total={merged['tested_total']}"
    )
    GAP_COUNTS.write_text(json.dumps(counts, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps(counts, ensure_ascii=False))  # stdout: counts ONLY
    return 3 if aborted else 0


if __name__ == "__main__":
    sys.exit(main())
