#!/usr/bin/env python3
"""map_union_analyze.py — reconcile prod swap-map regression. PRINTS COUNTS ONLY.

Situation discovered during fetch:
  - /tmp/prod_map_79.json        : actually the 62-term SEED map (archive mislabeled)
  - /tmp/prod_map_mined_17.json  : mining loop's 17 verified-mined entries (MINER-B)
  - local swap_map.json          : current 80-term verified map (62 seed + 18 verified)

Reconstructed pre-ship prod map (79) = seed62 + mined17.
Union target = current80 U seed62 U mined17  (placeholder: current-local wins conflicts).

Usage: python3 scripts/map_union_analyze.py [--json out.json]
"""

import json, sys, os

PROD_SEED = "/tmp/prod_map_79.json"
PROD_MINED = "/tmp/prod_map_mined_17.json"
LOCAL_CUR = os.path.expanduser(
    "~/projects/MASTER_PROJECTS/chanalyse/ops/filter_vocab/swap_map.json"
)


def load_map(path):
    d = json.load(open(path))
    return d.get("map", {}), d


def main():
    seed62, _ = load_map(PROD_SEED)
    mined17, _ = load_map(PROD_MINED)
    cur80, cur_doc = load_map(LOCAL_CUR)

    # the reconstructed 79-term prod map
    prod79 = dict(seed62)
    prod79.update(
        mined17
    )  # mined entries override seed on key collision (they came later)

    prod_only = {k: prod79[k] for k in prod79 if k not in cur80}
    cur_only = {k: cur80[k] for k in cur80 if k not in prod79}
    union_map = dict(prod79)
    union_map.update(cur80)  # conflicts -> current-local placeholder wins

    # detail: how much of prod_only is mined vs seed
    prod_only_mined = [k for k in prod_only if k in mined17]
    prod_only_seed = [k for k in prod_only if k in seed62]

    print(f"seed62_keys: {len(seed62)}")
    print(f"mined17_keys: {len(mined17)}")
    print(f"reconstructed_prod79_keys: {len(prod79)}")
    print(f"current80_keys: {len(cur80)}")
    print(f"prod_only_total: {len(prod_only)}")
    print(f"  prod_only_from_mined: {len(prod_only_mined)}")
    print(f"  prod_only_from_seed: {len(prod_only_seed)}")
    print(f"current_only: {len(cur_only)}")
    print(f"union_size: {len(union_map)}")
    print(f"union_equals_current: {len(union_map) == len(cur80) and not prod_only}")

    if "--json" in sys.argv:
        out = sys.argv[sys.argv.index("--json") + 1]
        with open(out, "w") as f:
            json.dump(
                {
                    "_meta": {
                        "source": "union prod-mined + verified investigation",
                        "built": "map_union_analyze.py reconcile 2026-08-30",
                        "counts": {
                            "prod_only": len(prod_only),
                            "current_only": len(cur_only),
                            "union": len(union_map),
                        },
                    },
                    "map": union_map,
                },
                f,
                indent=2,
                ensure_ascii=False,
            )
        print(f"union_written: {out}")


if __name__ == "__main__":
    main()
