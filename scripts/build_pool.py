#!/usr/bin/env python3
"""Merge all vocabulary sources into vocab/candidate_pool.json.

SAFETY CONTRACT: file-to-file merge. stdout carries COUNTS ONLY.
"""

import json
import sys
from datetime import datetime, timezone

OUT = "vocab/candidate_pool.json"


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[warn] missing source: {path}")
        return None


def flatten_values(node, acc):
    if isinstance(node, str):
        acc.append(node)
    elif isinstance(node, list):
        for x in node:
            flatten_values(x, acc)
    elif isinstance(node, dict):
        for v in node.values():
            flatten_values(v, acc)


def main() -> int:
    pool = []
    seen = set()
    per_source = {}

    def add(term, source):
        if not isinstance(term, str):
            return
        s = term.strip()
        if not (2 <= len(s) <= 80):
            return
        k = s.casefold()
        if k in seen:
            return
        seen.add(k)
        pool.append(s)
        per_source[source] = per_source.get(source, 0) + 1

    # 1. trigger_expanded: core_terms + variants + related
    te = load_json("vocab/trigger_expanded.json")
    if te:
        n0 = len(pool)
        for a in te.get("analysis", []):
            for key in ("core_terms", "variants", "related"):
                for t in a.get(key, []):
                    add(t, "trigger_expanded")
        print(f"[merge] trigger_expanded: +{len(pool) - n0}")

    # 2. local_seeds: every category
    ls = load_json("vocab/local_seeds.json")
    if ls:
        n0 = len(pool)
        for k, v in ls.items():
            if k.startswith("_"):
                continue
            if isinstance(v, list):
                for t in v:
                    add(t, f"local_seeds:{k}")
        print(f"[merge] local_seeds: +{len(pool) - n0}")

    # 3. swap_map: map keys
    sm = load_json("vocab/swap_map.json")
    if sm:
        n0 = len(pool)
        m = sm.get("map", sm)
        if isinstance(m, dict):
            for k in m.keys():
                add(k, "swap_map_keys")
        elif isinstance(m, list):
            for t in m:
                add(t, "swap_map_keys")
        print(f"[merge] swap_map keys: +{len(pool) - n0}")

    # 4. political_terms.txt: lines
    n0 = len(pool)
    try:
        with open("vocab/political_terms.txt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                add(line, "political_terms_txt")
    except FileNotFoundError:
        print("[warn] missing source: vocab/political_terms.txt")
    print(f"[merge] political_terms.txt: +{len(pool) - n0}")

    # 5. seed_slurs: values
    ss = load_json("vocab/seed_slurs.json")
    if ss:
        n0 = len(pool)
        vals = []
        flatten_values(ss, vals)
        for t in vals:
            add(t, "seed_slurs")
        print(f"[merge] seed_slurs values: +{len(pool) - n0}")

    doc = {
        "_meta": {
            "generated": datetime.now(timezone.utc).isoformat(),
            "generator": "scripts/build_pool.py",
            "dedupe": "case-insensitive, original form kept, first-seen order",
            "per_source_added": per_source,
        },
        "candidates": pool,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    with open(OUT, encoding="utf-8") as f:
        check = json.load(f)
    uniq = len({c.casefold() for c in check["candidates"]})
    print(
        f"[counts] candidate_pool.json: total={len(check['candidates'])} unique_ci={uniq}"
    )
    for src, n in sorted(per_source.items()):
        print(f"  {src}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
