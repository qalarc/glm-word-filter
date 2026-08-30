#!/usr/bin/env python3
"""Reconcile swap-map regression — union of archived prod 79-term map and current local 80-term map.

SAFETY: prints COUNTS ONLY. Never prints map keys/values (trigger vocabulary).
Conflict rule: if a key exists in both, keep the CURRENT-LOCAL placeholder.
If a key is prod-only, its placeholder comes from the prod map.

Actions:
  1. Print prod/current/prod-only/current-only/union counts.
  2. If prod-only entries exist: archive current map -> archive/swap_map_pre_union_<ts>.json,
     then write union map in-place to ops/filter_vocab/swap_map.json with meta.source set.
  3. If no prod-only entries: NO-OP (nothing was lost).
"""

import json
import shutil
import time
from pathlib import Path

PROD = Path("/tmp/prod_map_79.json")
CUR = Path.home() / "projects/MASTER_PROJECTS/chanalyse/ops/filter_vocab/swap_map.json"
ARCH = CUR.parent / "archive"

prod_doc = json.loads(PROD.read_text(encoding="utf-8"))
cur_doc = json.loads(CUR.read_text(encoding="utf-8"))

prod_map = prod_doc.get("map", {})
cur_map = cur_doc.get("map", {})

prod_keys = set(prod_map)
cur_keys = set(cur_map)
prod_only = prod_keys - cur_keys
cur_only = cur_keys - prod_keys
union_keys = prod_keys | cur_keys

print(f"prod(archived 79) entries: {len(prod_keys)}")
print(f"current(80) entries:       {len(cur_keys)}")
print(f"prod-only entries:         {len(prod_only)}")
print(f"current-only entries:      {len(cur_only)}")
print(f"union size:                {len(union_keys)}")

if not prod_only:
    print("RESULT: union == current — no prod-only entries, nothing lost. NO-OP.")
    raise SystemExit(0)

# Step 4: archive current local map, then write union in place
ts = time.strftime("%Y%m%d_%H%M%S")
arch_path = ARCH / f"swap_map_pre_union_{ts}.json"
shutil.copy2(CUR, arch_path)
print(f"archived current -> {arch_path.name}")

union_map = {}
for k in union_keys:
    # conflict -> current-local placeholder wins; prod-only keys take prod placeholder
    union_map[k] = cur_map[k] if k in cur_map else prod_map[k]

meta = dict(cur_doc.get("_meta", {}))
meta["source"] = "union prod-mined + verified investigation"
meta["union_ts"] = ts
meta["counts"] = {
    "prod_archived": len(prod_keys),
    "current": len(cur_keys),
    "prod_only": len(prod_only),
    "current_only": len(cur_only),
    "union": len(union_keys),
}
CUR.write_text(
    json.dumps({"_meta": meta, "map": union_map}, indent=2, ensure_ascii=False) + "\n",
    encoding="utf-8",
)
print(f"union written -> {CUR} ({len(union_map)} entries)")
