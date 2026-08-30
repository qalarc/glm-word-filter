#!/usr/bin/env python3
"""aggregate_pool.py — aggregate results/pool_verdicts_<ts>.jsonl into
results/VERIFIED_BLOCKERS.json + results/verified_summary.md.

Buckets (terms appear in FILES only, never on stdout):
  verified_single  — carrier-probe blocked (main layer, or T3 == carrier).
  context_sensitive — carrier PASS but >=1 innocuous template (T1/T2) blocks.
  clean            — count of carrier-pass terms with no template block.

stdout: counts only. Usage:
  python3 scripts/aggregate_pool.py [pool_verdicts.jsonl]
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = HERE.parent
RES = WORK / "results"

TMPL_IDS = {"T1", "T2", "T3"}


def main() -> int:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1]).resolve()
    else:
        cands = sorted(
            RES.glob("pool_verdicts_*.jsonl"), key=lambda p: p.stat().st_mtime
        )
        if not cands:
            print("FATAL: no pool_verdicts_*.jsonl found")
            return 2
        path = cands[-1]
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]

    main_rows = [r for r in rows if r.get("category") not in TMPL_IDS]
    t_rows = [r for r in rows if r.get("category") in TMPL_IDS]

    # ── main layer: per-category stats + carrier verdict per term ─────────
    per_cat: dict[str, dict] = {}
    carrier: dict[str, tuple[bool, str]] = {}
    for r in main_rows:
        cat = str(r.get("category", "unmapped"))
        detail = str(r.get("detail", ""))
        st = per_cat.setdefault(cat, {"tested": 0, "blocked": 0, "net_err": 0})
        if detail.startswith("net-err") or detail == "cap":
            st["net_err"] += 1
            continue
        st["tested"] += 1
        b = bool(r.get("blocked"))
        st["blocked"] += int(b)
        carrier[r["term"]] = (b, detail)

    # ── context layer: per-template stats + template verdicts per term ────
    tpl_stats = {k: {"tested": 0, "blocked": 0, "net_err": 0} for k in TMPL_IDS}
    tpl: dict[str, dict[str, bool]] = {}
    for r in t_rows:
        k = str(r.get("category"))
        detail = str(r.get("detail", ""))
        st = tpl_stats[k]
        if detail.startswith("net-err") or detail == "cap":
            st["net_err"] += 1
            continue
        st["tested"] += 1
        b = bool(r.get("blocked"))
        st["blocked"] += int(b)
        tpl.setdefault(r["term"], {})[k] = b

    # ── buckets ───────────────────────────────────────────────────────────
    verified_single: list[str] = []
    context_sensitive: list[str] = []
    clean = 0
    unknown_carrier = 0
    for t in sorted(set(carrier) | set(tpl)):
        cb = carrier.get(t, (None, ""))[0]
        if cb is None and "T3" in tpl.get(t, {}):
            # T3 is literally carrier+term — a valid carrier source for terms
            # that only appear in the context layer (e.g. prior non-pool
            # blockers carried in via reuse-prior rows).
            cb = tpl[t]["T3"]
        tpl_block = bool(tpl.get(t, {}).get("T1")) or bool(tpl.get(t, {}).get("T2"))
        if cb is True:
            verified_single.append(t)
        elif cb is False:
            if tpl_block:
                context_sensitive.append(t)
            else:
                clean += 1
        else:
            unknown_carrier += 1

    blocked_still_template = sum(
        1
        for t in verified_single
        if bool(tpl.get(t, {}).get("T1")) or bool(tpl.get(t, {}).get("T2"))
    )

    doc = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": str(path.relative_to(WORK)),
        "verified_single": verified_single,
        "context_sensitive": context_sensitive,
        "clean": clean,
        "counts": {
            "verified_single": len(verified_single),
            "context_sensitive": len(context_sensitive),
            "clean": clean,
            "unknown_carrier": unknown_carrier,
            "pool_terms_in_file": len({r["term"] for r in main_rows}),
            "per_category": per_cat,
            "template_layer": tpl_stats,
            "carrier_blocked_still_template_blocked": blocked_still_template,
        },
    }
    out_json = RES / "VERIFIED_BLOCKERS.json"
    out_json.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")

    total_tested = sum(s["tested"] for s in per_cat.values())
    total_blocked = sum(s["blocked"] for s in per_cat.values())
    total_net = sum(s["net_err"] for s in per_cat.values())
    lines = [
        "# Verified blockers — pool verification",
        "",
        f"Generated: {doc['generated']}  |  Source: `{doc['source']}`",
        "",
        "## Main layer (carrier + term) — per category",
        "",
        "| category | tested | blocked | net_err |",
        "|---|---|---|---|",
    ]
    for cat in sorted(per_cat):
        s = per_cat[cat]
        lines.append(f"| {cat} | {s['tested']} | {s['blocked']} | {s['net_err']} |")
    lines += [
        f"| **TOTAL** | **{total_tested}** | **{total_blocked}** | **{total_net}** |",
        "",
        "## Context layer (innocuous templates x blocked/core terms)",
        "",
        "| template | tested | blocked | net_err |",
        "|---|---|---|---|",
    ]
    for k in ("T1", "T2", "T3"):
        s = tpl_stats[k]
        lines.append(f"| {k} | {s['tested']} | {s['blocked']} | {s['net_err']} |")
    lines += [
        "",
        f"- verified_single (blocked in plain carrier): {len(verified_single)}",
        f"- context_sensitive (carrier-pass but block in >=1 template): {len(context_sensitive)}",
        f"- clean (carrier-pass, no template block): {clean}",
        f"- carrier-blocked terms that STILL block in >=1 template: {blocked_still_template}",
        "",
        "*(No term content appears in this file by design; see",
        f"`{doc['source']}` and `results/VERIFIED_BLOCKERS.json`.)*",
        "",
    ]
    (RES / "verified_summary.md").write_text("\n".join(lines))

    # stdout: counts only
    print(f"AGG source={doc['source']}")
    for cat in sorted(per_cat):
        s = per_cat[cat]
        print(
            f"AGG_CAT {cat} tested={s['tested']} blocked={s['blocked']} net_err={s['net_err']}"
        )
    for k in ("T1", "T2", "T3"):
        s = tpl_stats[k]
        print(
            f"AGG_TPL {k} tested={s['tested']} blocked={s['blocked']} net_err={s['net_err']}"
        )
    print(
        f"AGG verified_single={len(verified_single)} "
        f"context_sensitive={len(context_sensitive)} clean={clean} "
        f"unknown_carrier={unknown_carrier} "
        f"blocked_still_template={blocked_still_template}"
    )
    print(f"AGG wrote results/VERIFIED_BLOCKERS.json results/verified_summary.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
