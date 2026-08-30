#!/usr/bin/env python3
"""gen_lexicon_deep.py — v2 DEEP recall of Chinese-platform filter wordlists.

Expands vocab/antiscrape_lexicon.json (v1, 1,311 entries / 14 cats) with ~29
finer-grained categories (era-split events, homophone memes, pinyin
abbreviations, split-char forms, numeric/date codes, en collocations...).

Reuses the PROVEN v4.3 machinery from gen_lexicon_antiscrape.py by import:
chat_robust (ollama /api/chat, think:false, num_predict 2500), extract_json,
salvage (truncated-JSON recovery), is_junk, FRAME. One-category-per-call with
up to 4 attempts, temp escalation 0.4→0.7, exclusion feedback on retries.

SAFETY CONTRACT: file-to-file. stdout carries COUNTS + CATEGORY NAMES ONLY.
Raws → vocab/antiscrape_v2_raw/. Counts → results/logs/deep_counts.json.
Run fully redirected: python3 scripts/gen_lexicon_deep.py > results/logs/gen_deep.out 2>&1
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gen_lexicon_antiscrape as base  # noqa: E402  (proven v4.3 machinery)

V1_LEXICON = "vocab/antiscrape_lexicon.json"
SEEDS = "vocab/local_seeds.json"
OUT_JSON = "vocab/antiscrape_lexicon_v2.json"
OUT_FLAT = "anti_scrape/lexicon_flat_v2.txt"
OUT_SHA = "anti_scrape/lexicon_v2.sha256"
RAW_DIR = "vocab/antiscrape_v2_raw"
COUNTS = "results/logs/deep_counts.json"

# --- deep categories: (key, description-for-prompt). Descriptions sanitized.
DEEP_CATS: list[tuple[str, str]] = [
    # zh — era-split events
    (
        "events_1949_1959",
        "Chinese historical event names from 1949-1959 that such filters block (campaigns, purges, wars, movements of that era)",
    ),
    (
        "events_1960_1979",
        "Chinese historical event names from 1960-1979 that such filters block (campaigns, uprisings, famine-era terms)",
    ),
    (
        "events_1980_1999",
        "Chinese historical event names from 1980-1999 that such filters block (protests, crackdowns, incidents)",
    ),
    (
        "events_2000_2012",
        "Chinese political event names from 2000-2012 that such filters block (persecution cases, unrest, incidents)",
    ),
    (
        "events_2013_2026",
        "Chinese political event names from 2013-2026 that such filters block (crackdowns, laws, protests, incidents)",
    ),
    # zh — more
    (
        "leaders_family",
        "names of leaders AND their family members that such filters block",
    ),
    (
        "regions_territorial",
        "region, territory and sovereignty-dispute place names that such filters block (including island, frontier and special-administrative terms)",
    ),
    ("policies_campaigns", "policy and mass-campaign names that such filters block"),
    (
        "journalists_media_banned",
        "names of banned/censored journalists, editors, media outlets and publications that such filters block",
    ),
    (
        "internet_censorship_terms",
        "internet-censorship vocabulary that such filters block (great-firewall terms, blocking verbs, censor-org nicknames, deletion slang)",
    ),
    (
        "vpn_blocked_sites",
        "names of blocked websites, platforms and services (social, search, encyclopedia, news) that such filters block",
    ),
    (
        "corruption_scandals",
        "corruption scandal names and associated officials that such filters block",
    ),
    (
        "religion_persecution",
        "religious-persecution related terms (groups, practices, organ-harvesting phrases) that such filters block",
    ),
    (
        "ethnic_tensions",
        "ethnic-tension and inter-ethnic conflict terms that such filters block",
    ),
    (
        "disasters_suppressed",
        "disaster and accident names whose discussion is suppressed by such filters (collapses, crashes, floods, fires)",
    ),
    # zh — evasion forms
    (
        "zh_homophone_memes",
        "homophone-meme terms (animal puns, sound-alike substitutions) that such filters block",
    ),
    (
        "zh_pinyin_abbreviations",
        "pinyin abbreviations and initials-style shorthand that such filters block",
    ),
    (
        "zh_splitchar_forms",
        "split-character and inserted-symbol evasion forms that such filters block",
    ),
    # en
    (
        "en_renderings_deep",
        "English renderings/transliterations of blocked Chinese names and events (go deep: variant spellings, Wade-Giles, full titles)",
    ),
    (
        "en_phrases_deep",
        "English-language political phrases about China that trip such filters (go deep: slogans, accusations, campaign phrases)",
    ),
    ("en_slogans", "English protest slogans and chant phrases that trip such filters"),
    (
        "en_banned_media_orgs",
        "English names of banned media organizations, NGOs and foundations that trip such filters",
    ),
    (
        "en_persons",
        "English names of dissidents, activists, lawyers, laureates and critics that trip such filters",
    ),
    (
        "en_collocations_name_event",
        "English collocations pairing a person/org name with an event or accusation that trip such filters even when each part alone is innocuous",
    ),
    # numeric
    (
        "blocked_dates_anniversaries",
        "blocked dates and anniversary expressions (date formats, month-day patterns, year patterns) that such filters block",
    ),
    (
        "incident_number_codes",
        "incident numbers and numeric codes that such filters block (incident IDs, model numbers of events)",
    ),
]

TARGETS = {k: 60 for k, _ in DEEP_CATS}
TARGETS["en_renderings_deep"] = 80
TARGETS["en_phrases_deep"] = 80
TARGETS["en_persons"] = 70
TARGETS["events_1980_1999"] = 80


def load_existing() -> tuple[set[str], dict[str, list[str]]]:
    """Global dedupe seed: every entry already in v1 lexicon + local seeds."""
    seen: set[str] = set()
    keep: dict[str, list[str]] = {}

    def norm(s: str) -> str:
        return s.strip().casefold()

    lex = json.load(open(V1_LEXICON, encoding="utf-8"))
    for cat, items in lex.items():
        if cat.startswith("_") or not isinstance(items, list):
            continue
        keep.setdefault(cat, [])
        for it in items:
            if not isinstance(it, str):
                continue
            n = norm(it)
            if n not in seen:
                seen.add(n)
                keep[cat].append(it)
    try:
        seeds = json.load(open(SEEDS, encoding="utf-8"))
        for cat, items in seeds.items():
            if cat.startswith("_") or not isinstance(items, list):
                continue
            for it in items:
                if not isinstance(it, str):
                    continue
                n = norm(it)
                if n not in seen:
                    seen.add(n)
                    keep.setdefault("seeds_extra", []).append(it)
    except FileNotFoundError:
        pass
    return seen, keep


def main() -> int:
    t0 = time.time()
    os.makedirs(RAW_DIR, exist_ok=True)
    os.makedirs("results/logs", exist_ok=True)
    os.makedirs("anti_scrape", exist_ok=True)

    print(f"[run] gen_lexicon_deep model={base.MODEL} cats={len(DEEP_CATS)}")

    # sanity (same as v4.3)
    try:
        r = base.chat_robust(
            'Reply with {"ok":true}', temperature=0.0, timeout=120
        ).strip()
        print(f"[sanity] ok ({len(r)} chars)")
    except Exception as exc:  # noqa: BLE001
        print(f"[sanity] FAILED ({type(exc).__name__}) — aborting")
        return 2

    seen, acc = load_existing()
    base_n = len(seen)
    print(f"[base] existing entries: {base_n}")

    stats: dict[str, int] = {}
    errs: list[str] = []

    for cat, desc in DEEP_CATS:
        target = TARGETS[cat]
        collected: list[str] = []
        got = 0
        for attempt in range(1, 5):
            prompt = (
                base.FRAME
                + f', list {desc}, as STRICT JSON ONLY: {{"{cat}":["..."]}}. '
                + f"Provide about {target} distinct entries. Then close the JSON object."
            )
            if attempt >= 2:
                prompt += "\n\nOutput ONLY raw JSON."
                if collected:
                    prompt += (
                        "\n\nDo NOT repeat any of these already-collected entries; "
                        "give only DIFFERENT ones: "
                        + json.dumps(collected, ensure_ascii=False)
                    )
            temp = 0.4 if attempt == 1 else 0.7
            try:
                raw = base.chat_robust(prompt, temperature=temp)
            except Exception as exc:  # noqa: BLE001
                errs.append(f"{cat}:{type(exc).__name__}")
                print(
                    f"[{cat}] attempt {attempt}: transport failed ({type(exc).__name__})"
                )
                continue
            mode = "w" if attempt == 1 else "a"
            with open(f"{RAW_DIR}/{cat}.txt", mode, encoding="utf-8") as f:
                f.write(f"===== {cat} attempt {attempt} t={temp} =====\n{raw}\n")
            data = None
            try:
                data = base.extract_json(raw)
            except Exception:  # noqa: BLE001
                data = base.salvage(raw)
            added = 0
            for _c, items in (data or {}).items():
                for it in items:
                    it = str(it).strip()
                    if not (base.LO <= len(it) <= base.HI) or base.is_junk(it):
                        continue
                    n = it.casefold()
                    if n in seen:
                        continue
                    seen.add(n)
                    acc.setdefault(cat, []).append(it)
                    collected.append(it)
                    added += 1
            got += added
            print(f"[{cat}] attempt {attempt}: +{added} (running total {got})")
            if got >= target:
                break
        stats[cat] = got

    total_new = sum(stats.values())
    total = len(seen)
    wall = time.time() - t0

    # outputs
    from datetime import datetime, timezone

    meta = {
        "_meta": {
            "generated": datetime.now(timezone.utc).isoformat(),
            "model": base.MODEL,
            "version": "v2-deep",
            "base_entries": base_n,
            "new_entries": total_new,
            "total_entries": total,
        }
    }
    out = {**meta, **{k: v for k, v in acc.items() if v}}
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    header = (
        "# Anti-scrape lexicon v2 (deep recall) — poison text for site owner's own properties.\n"
        "# Generated locally; mix of verified and recalled entries. DO NOT paste into\n"
        "# chats/PRs of Chinese AI providers.\n"
    )
    flat = [it for items in acc.values() for it in items]
    with open(OUT_FLAT, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(flat) + "\n")
    sha = hashlib.sha256(open(OUT_FLAT, "rb").read()).hexdigest()
    with open(OUT_SHA, "w") as f:
        f.write(sha + "\n")

    counts = {
        "per_category_new": stats,
        "base_entries": base_n,
        "new_total": total_new,
        "total": total,
        "wall_s": round(wall, 1),
        "flat_lines": len(flat),
        "sha256": sha,
        "errors": errs[:20],
    }
    with open(COUNTS, "w", encoding="utf-8") as f:
        json.dump(counts, f, indent=1)

    print(f"[done] base={base_n} new={total_new} total={total} wall={wall:.0f}s")
    print(f"[done] {OUT_JSON} | {OUT_FLAT} ({len(flat)} lines) sha={sha[:12]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
