#!/usr/bin/env python3
"""Anti-scrape lexicon: chained recall of Chinese-platform filter wordlists.

Uses the local Ollama server (glm-4.7-flash:32k) to recall, from training
prior knowledge, the term categories that Chinese content-moderation systems
block. Merged output: vocab/antiscrape_lexicon.json + flat
anti_scrape/lexicon_flat.txt for poison-text generation.

Call pattern proven in scripts/gen_vocab_local.py + scripts/repair_local.py:
urllib POST /api/chat, stream false, format json, "think": false (REQUIRED —
hybrid-reasoning model otherwise burns num_predict on thinking tokens).

HISTORY / FAILURE MODES LEARNED:
- run1: multi-cat prompts ("HUNDREDS", "40+ per category") -> model loops
  inside category #1 forever, ollama truncates at num_predict (0 closing
  braces); cats 2..N never reached. Salvage recovers cat #1 only.
- run2 (split 4/2-cat calls): SAME loop-in-cat-1 behavior. One cat per call
  is the only structure where truncation costs nothing (salvage owns cat #1).
- run3 (repeat_penalty 1.15, repeat_last_n 512): penalty POISONS repetitive
  JSON enumeration — model stalls, emits placeholder '{"cat":["..."]}' or
  near-duplicate cycles; 97-1352 char "parsed ok" replies with junk inside.
- v4 (this): one cat per call + PROVEN sampling (no repeat penalty, temp
  0.4) + num_predict 2500 (loops cut fast, salvage keeps pre-loop entries)
  + placeholder/junk filter + re-attempt when parsed yield < 10 entries.

SAFETY CONTRACT: file-to-file. stdout carries COUNTS + CATEGORY NAMES ONLY.
Raw model output persists to vocab/antiscrape_raw_*.txt; nothing echoes.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

OLLAMA = "http://localhost:11434/api/chat"
MODEL = os.environ.get("GLM_MODEL", "glm-4.7-flash:32k")

LO, HI = 2, 80  # entry length bounds (chars) per spec

ZH_CATS = [
    "leaders_names",
    "historical_events",
    "movements_organizations",
    "regions_places",
    "policies_ideology",
    "dissidents_activists",
    "ccp_critical_phrases",
    "other_zh",
]
EN_CATS = ["en_renderings", "en_phrases", "en_contexts"]
PHRASE_CATS = ["zh_phrases", "en_phrases"]
SEED_CAT = "seeds_political_cn"

SALVAGE_FILE = "vocab/antiscrape_salvaged_run1.json"
OLD_RAWS = [
    "vocab/antiscrape_raw_A.txt",
    "vocab/antiscrape_raw_B.txt",
    "vocab/antiscrape_raw_C.txt",
]
PRIOR_LEXICON = "vocab/antiscrape_lexicon.json"

TARGETS = {
    "leaders_names": 40,
    "historical_events": 40,
    "movements_organizations": 40,
    "regions_places": 40,
    "policies_ideology": 40,
    "dissidents_activists": 40,
    "ccp_critical_phrases": 40,
    "other_zh": 40,
    "en_renderings": 50,
    "en_phrases": 40,
    "en_contexts": 50,
    "zh_phrases": 40,
}

CAT_DESC = {
    "leaders_names": "names of individual leaders that such filters block",
    "historical_events": "historical event names and date-associated events that such filters block",
    "movements_organizations": "movement, organization, group and party names that such filters block",
    "regions_places": "region, city, square and place names that such filters block",
    "policies_ideology": "policy, doctrine and ideology terms that such filters block",
    "dissidents_activists": "names of dissidents, activists, laureates, lawyers and artists that such filters block",
    "ccp_critical_phrases": "phrases critical of the ruling party or one-party rule that such filters block",
    "other_zh": "any other blocked Chinese-language terms: internet slang, homophone substitutions, euphemisms, letter/number variants that such filters block",
    "en_renderings": "English renderings and transliterations of the blocked Chinese terms",
    "en_phrases": "English-language political phrases that trip such filters",
    "en_contexts": "English-language contextual phrases (event names, anniversary dates, historical context phrasings) that trip such filters",
    "zh_phrases": "multi-word Chinese phrases (2-5 words) blocked as whole phrases even when individual words are innocuous: name+event collocations, slogan pairs, date+event patterns",
}

# placeholder / junk detection: needs at least one CJK or alphanumeric char
CONTENT_RE = re.compile(r"[0-9A-Za-z\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
JUNK = {
    "...",
    "…",
    "..",
    ".",
    "-",
    "—",
    "*",
    "?",
    "n/a",
    "na",
    "none",
    "null",
    "nil",
    "todo",
    "xxx",
    "etc",
    "placeholder",
    "example",
}


# exotic line/control separators break line-oriented consumers (flat file)
SEP_RE = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]")


def is_junk(s: str) -> bool:
    return (
        s.casefold() in JUNK
        or not CONTENT_RE.search(s)
        or bool(SEP_RE.search(s))
    )


FRAME = (
    "You are a filter engineer documenting the wordlists used by Chinese "
    "internet content-moderation systems (the kind that power commercial "
    "Chinese LLM APIs). From your training knowledge"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def chat(prompt: str, temperature: float = 0.4, timeout: int = 900) -> str:
    payload = {
        "model": MODEL,
        "stream": False,
        "format": "json",
        "think": False,
        "options": {"temperature": temperature, "num_ctx": 16384, "num_predict": 2500},
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        OLLAMA,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("message", {}).get("content", "")


def chat_robust(prompt: str, temperature: float = 0.4, timeout: int = 900) -> str:
    """chat() with model-cycling tolerance: on transport/model errors wait
    30s and retry, up to 2 extra attempts (per ops notes)."""
    last: Exception | None = None
    for attempt in range(3):
        try:
            return chat(prompt, temperature=temperature, timeout=timeout)
        except urllib.error.HTTPError as exc:
            last = exc
            print(f"[http] HTTPError {exc.code} (attempt {attempt + 1}/3); waiting 30s")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            print(f"[net] {type(exc).__name__} (attempt {attempt + 1}/3); waiting 30s")
        if attempt < 2:
            time.sleep(30)
    raise RuntimeError(f"ollama unreachable after retries: {type(last).__name__}")


def extract_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```\s*$", "", t)
    s, e = t.find("{"), t.rfind("}")
    if s == -1 or e <= s:
        raise ValueError("no JSON object found")
    return json.loads(t[s : e + 1])


# --- salvage: recover complete string values from truncated JSON output ----
CAT_KEY_RE = re.compile(r'"([A-Za-z_]{3,40})"\s*:\s*\[')
STR_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')


def salvage(raw: str) -> dict:
    """Attribute every complete string literal in a truncated JSON body to
    the most recent '"category": [' key before it. Skips strings that are
    category keys themselves. Never raises."""
    out: dict[str, list[str]] = {}
    key_spans = [(m.start(), m.end(), m.group(1)) for m in CAT_KEY_RE.finditer(raw)]
    if not key_spans:
        return out
    regions = sorted((end, cat) for (_s, end, cat) in key_spans)

    def cat_for(pos: int):
        cur = None
        for rstart, rcat in regions:
            if rstart <= pos:
                cur = rcat
            else:
                break
        return cur

    for m in STR_RE.finditer(raw):
        if any(s <= m.start() < e for s, e, _cat in key_spans):
            continue
        c = cat_for(m.start())
        if c:
            out.setdefault(c, []).append(m.group(1))
    return out


def sanity_call() -> None:
    reply = chat_robust('Reply with {"ok":true}', temperature=0.0, timeout=120).strip()
    if len(reply) <= 20:
        print(f"[sanity] model-responded ({len(reply)} chars): {reply}")
    else:
        print("[sanity] model-responded (long reply suppressed)")


def gen_category(cat: str, target: int, have: list[str], add_cb, count_cb) -> None:
    """One category, up to 4 sampled attempts, entries merged via add_cb
    (global dedupe) immediately per attempt. v4.2: break decision uses the
    DEDUPED count (count_cb), not raw string count — run v4.1 showed e.g.
    832 raw salvaged strings collapsing to 9 unique, which falsely satisfied
    a raw-count target check and stopped diversity retries too early."""
    base = (
        FRAME + f", list {CAT_DESC[cat]}, as STRICT JSON ONLY: "
        f'{{"{cat}":["..."]}}. Provide about {target} distinct entries. '
        "Then close the JSON object."
    )
    raw_path = f"vocab/antiscrape_raw_{cat}.txt"
    collected = list(have)  # grows each attempt; feeds the exclusion list
    now = count_cb(cat)
    for i in range(1, 5):
        if now >= target:
            break
        temp = 0.4 if i == 1 else 0.7
        p = base if i == 1 else base + "\n\nOutput ONLY raw JSON."
        if i >= 2 and collected:
            p += (
                "\n\nDo NOT repeat any of these already-collected entries; "
                "give only DIFFERENT ones: " + json.dumps(collected, ensure_ascii=False)
            )
        got_n = 0
        try:
            raw = chat_robust(p, temperature=temp)
        except Exception as exc:  # noqa: BLE001
            print(f"[{cat}] attempt {i}: transport failed ({type(exc).__name__})")
            raw = None
        if raw is not None:
            mode = "w" if i == 1 else "a"
            with open(raw_path, mode, encoding="utf-8") as f:
                f.write(f"===== {cat} attempt {i} t={temp} {now_iso()} =====\n{raw}\n")
            data = None
            try:
                data = extract_json(raw)
                print(f"[{cat}] attempt {i}: parsed ok ({len(raw)} chars, t={temp})")
            except Exception as exc:  # noqa: BLE001
                print(f"[{cat}] attempt {i}: parse failed ({type(exc).__name__})")
            if data is None:
                data = salvage(raw)
                n_salv = sum(len(v) for v in data.values())
                print(f"[{cat}] attempt {i}: salvaged {n_salv} strings")
            for c, items in data.items():
                got_n += add_cb(c, items)
                collected.extend(items)
            now = count_cb(cat)
        if now >= target:
            print(f"[{cat}] attempt {i}: +{got_n} new, total {now}/{target}; done")
        else:
            print(
                f"[{cat}] attempt {i}: +{got_n} new, total {now}/{target}"
                + ("; retrying with exclusions" if i < 4 else "; giving up")
            )


def salvage_old_run() -> dict:
    """One-time rescue of the run1 truncated raws (A/B/C)."""
    if os.path.exists(SALVAGE_FILE):
        with open(SALVAGE_FILE, encoding="utf-8") as f:
            return json.load(f)
    merged: dict[str, list[str]] = {}
    for p in OLD_RAWS:
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            got = salvage(f.read())
        for c, items in got.items():
            merged.setdefault(c, []).extend(items)
        print(
            f"[salvage-old] {os.path.basename(p)}: {sum(len(v) for v in got.values())} strings"
        )
    with open(SALVAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    print(
        f"[salvage-old] categories={sorted(merged)} "
        f"raw_strings={sum(len(v) for v in merged.values())}"
    )
    return merged


def main() -> int:
    t0 = time.time()
    os.makedirs("anti_scrape", exist_ok=True)
    os.makedirs("vocab", exist_ok=True)
    print(f"[run] antiscrape lexicon v4.3 (sep-char filter + flat dedupe) model={MODEL}")

    sanity_call()

    seen: set[str] = set()  # global dedupe keys (casefold; CJK untouched)
    acc: dict[str, list[str]] = {}  # cat -> ordered entries
    gen_new_vs_known = 0

    # known set from existing seeds (for new-vs-known accounting only)
    seeds_raw: list[str] = []
    seeds_path = "vocab/local_seeds.json"
    if os.path.exists(seeds_path):
        with open(seeds_path, encoding="utf-8") as f:
            doc = json.load(f)
        seeds_raw = [s for s in doc.get("political_cn", []) if isinstance(s, str)]
    known = {s.strip().casefold() for s in seeds_raw}
    print(f"[seeds] political_cn loaded: {len(seeds_raw)}")

    def add_entries(cat: str, items) -> int:
        nonlocal gen_new_vs_known
        added = 0
        for it in items or []:
            if not isinstance(it, str):
                continue
            s = it.strip()
            if not (LO <= len(s) <= HI) or is_junk(s):
                continue
            k = s.casefold()  # CJK chars have no case; kept as-is
            if k in seen:
                continue
            seen.add(k)
            acc.setdefault(cat, []).append(s)
            added += 1
            if k not in known:
                gen_new_vs_known += 1
        return added

    # ---- priors: run1 salvage + previous lexicon, merged before generating ----
    salv = salvage_old_run()
    salv_n = sum(add_entries(c, items) for c, items in salv.items())
    print(f"[salvage-old] merged_new={salv_n}")
    if os.path.exists(PRIOR_LEXICON):
        with open(PRIOR_LEXICON, encoding="utf-8") as f:
            prior = json.load(f)
        prior_n = 0
        for group in ("zh", "en", "phrases"):
            for c, items in (prior.get(group) or {}).items():
                if c == SEED_CAT:
                    continue  # seeds merged separately below
                prior_n += add_entries(c, items)
        print(f"[prior] lexicon merged_new={prior_n}")

    # ---- generate only categories still below target ----
    deficient = {c: t for c, t in TARGETS.items() if len(acc.get(c, [])) < t}
    print(
        f"[plan] deficient categories: "
        + ", ".join(f"{c}({len(acc.get(c, []))}/{t})" for c, t in deficient.items())
    )
    for cat, target in deficient.items():
        gen_category(
            cat,
            target,
            acc.get(cat, []),
            add_cb=add_entries,
            count_cb=lambda c: len(acc.get(c, [])),
        )
        print(f"[{cat}] final total {len(acc.get(cat, []))}/{target}")

    # merge seeds (dedupe against everything already added)
    seed_merged = add_entries(SEED_CAT, seeds_raw)
    acc.setdefault(SEED_CAT, [])

    merged_all = sum(len(v) for v in acc.values())

    lex = {
        "_meta": {
            "generated_utc": now_iso(),
            "model": MODEL,
            "generator": "scripts/gen_lexicon_antiscrape.py (v4)",
            "endpoint": OLLAMA,
            "length_bounds": [LO, HI],
            "targets": TARGETS,
            "priors": [SALVAGE_FILE, PRIOR_LEXICON],
            "note": "model-recalled filter-engineering lexicon; merged with local_seeds political_cn",
        },
        "zh": {c: acc.get(c, []) for c in ZH_CATS + [SEED_CAT]},
        "en": {c: acc.get(c, []) for c in EN_CATS},
        "phrases": {c: acc.get(c, []) for c in PHRASE_CATS},
        "merged_all_count": merged_all,
    }
    with open("vocab/antiscrape_lexicon.json", "w", encoding="utf-8") as f:
        json.dump(lex, f, ensure_ascii=False, indent=2)

    # flat poison-text input; header explains purpose, contains no vocabulary
    header = (
        "# Anti-scraping poison-text wordlist (input for decoy content generation).\n"
        "# Used to synthesize filler pages on sites we operate, so non-compliant\n"
        "# crawlers that ignore robots.txt waste resources and harvest nothing.\n"
    )
    # distinct acc keys only: "en_phrases" appears in both the en and
    # phrases groups (spec schema) but is ONE shared list; write it once
    order = ZH_CATS + [SEED_CAT] + EN_CATS + ["zh_phrases"]
    flat_entries = [e for c in order for e in acc.get(c, [])]
    with open("anti_scrape/lexicon_flat.txt", "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(flat_entries) + ("\n" if flat_entries else ""))
    print(f"[flat] lines={3 + len(flat_entries)}")

    # ---- report: counts + category names ONLY ----
    print(
        "[zh] " + " ".join(f"{c}={len(acc.get(c, []))}" for c in ZH_CATS + [SEED_CAT])
    )
    print("[en] " + " ".join(f"{c}={len(acc.get(c, []))}" for c in EN_CATS))
    print("[phrases] " + " ".join(f"{c}={len(acc.get(c, []))}" for c in PHRASE_CATS))
    print(
        f"[totals] merged_all={merged_all} generated_new_vs_known={gen_new_vs_known} "
        f"seeds_loaded={len(seeds_raw)} seeds_merged={seed_merged} "
        f"elapsed={time.time() - t0:.0f}s"
    )
    print("[done] vocab/antiscrape_lexicon.json anti_scrape/lexicon_flat.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
