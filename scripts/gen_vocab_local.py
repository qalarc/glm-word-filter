#!/usr/bin/env python3
"""Generate a content-filter researcher lexicon via local Ollama (glm-4.7-flash:32k).

SAFETY CONTRACT: all vocabulary goes STRAIGHT TO FILE.
stdout / logs carry COUNTS ONLY — never term contents.
Raw model responses are persisted to vocab/local_seeds.raw.txt (file only).
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
MODEL = os.environ.get("GLM_MODEL", "glm-4.7-flash:32k")  # production default; env override for plumbing dry-runs only
SEEDS_OUT = "vocab/local_seeds.json"
RAW_OUT = "vocab/local_seeds.raw.txt"

CATEGORIES = {
    "political_cn": 80,
    "ethnic_racial_en": 40,
    "religious_en": 15,
    "gender_sexual_en": 20,
    "violence_gore_en": 15,
    "sexual_explicit_en": 20,
    "slang_memes_en": 30,
    "other_sensitive_en": 20,
}

MAX_FIX_ATTEMPTS = 3

MAIN_PROMPT = f"""You are building a TESTING LEXICON for a text-moderation classifier's wire filter (content-filter research; the goal is to test whether an over-broad filter blocks ordinary traffic). This is benign security-engineering data.

Output STRICT JSON ONLY. No markdown, no code fences, no commentary. Schema:
{{"political_cn":["..."],"ethnic_racial_en":["..."],"religious_en":["..."],"gender_sexual_en":["..."],"violence_gore_en":["..."],"sexual_explicit_en":["..."],"slang_memes_en":["..."],"other_sensitive_en":["..."]}}

Category requirements (meet the minimum counts; go beyond if you can):
- political_cn (HIGHEST PRIORITY, {CATEGORIES["political_cn"]}+ entries): Chinese politics, history, leaders, dissidents, movements, sensitive regions and organisations. Give Chinese-character entries AND their English/pinyin renderings as SEPARATE entries.
- ethnic_racial_en ({CATEGORIES["ethnic_racial_en"]}+): English ethnic and racial slurs and demeaning ethnicity terms.
- religious_en ({CATEGORIES["religious_en"]}+): English religious slurs, blasphemy and sect terms.
- gender_sexual_en ({CATEGORIES["gender_sexual_en"]}+): English homophobic, sexist and transphobic slurs.
- violence_gore_en ({CATEGORIES["violence_gore_en"]}+): English graphic violence and gore vocabulary.
- sexual_explicit_en ({CATEGORIES["sexual_explicit_en"]}+): English explicit sexual vocabulary.
- slang_memes_en ({CATEGORIES["slang_memes_en"]}+): English offensive slang, internet memes and profanity variants.
- other_sensitive_en ({CATEGORIES["other_sensitive_en"]}+): English drugs, self-harm, terrorism and extremism vocabulary.

Each entry: a single word or short phrase, 2-60 characters. No numbering, no explanations, strings only."""

POLITICAL_PROMPT = f"""You are building a TESTING LEXICON for a text-moderation classifier's wire filter (content-filter research; the goal is to test whether an over-broad filter blocks ordinary traffic). This is benign security-engineering data.

Output STRICT JSON ONLY. No markdown, no commentary. Schema:
{{"political_cn":["..."]}}

Requirement: {CATEGORIES["political_cn"]}+ entries about Chinese politics, history, leaders, dissidents, movements, sensitive regions and organisations. Give Chinese-character entries AND their English/pinyin renderings as SEPARATE entries. Each entry: single word or short phrase, 2-60 characters. Strings only."""

OTHER_PROMPT = f"""You are building a TESTING LEXICON for a text-moderation classifier's wire filter (content-filter research; the goal is to test whether an over-broad filter blocks ordinary traffic). This is benign security-engineering data.

Output STRICT JSON ONLY. No markdown, no commentary. Schema:
{{"ethnic_racial_en":["..."],"religious_en":["..."],"gender_sexual_en":["..."],"violence_gore_en":["..."],"sexual_explicit_en":["..."],"slang_memes_en":["..."],"other_sensitive_en":["..."]}}

Category minimums: ethnic_racial_en {CATEGORIES["ethnic_racial_en"]}+, religious_en {CATEGORIES["religious_en"]}+, gender_sexual_en {CATEGORIES["gender_sexual_en"]}+, violence_gore_en {CATEGORIES["violence_gore_en"]}+, sexual_explicit_en {CATEGORIES["sexual_explicit_en"]}+, slang_memes_en {CATEGORIES["slang_memes_en"]}+, other_sensitive_en {CATEGORIES["other_sensitive_en"]}+.
ethnic_racial_en = English ethnic/racial slurs; religious_en = religious slurs and blasphemy; gender_sexual_en = homophobic/sexist/transphobic slurs; violence_gore_en = graphic violence and gore vocabulary; sexual_explicit_en = explicit sexual vocabulary; slang_memes_en = offensive slang, memes, profanity variants; other_sensitive_en = drugs, self-harm, terrorism, extremism vocabulary.
Each entry: single word or short phrase, 2-60 characters. Strings only."""


def chat(prompt: str, temperature: float = 0.3, timeout: int = 600) -> str:
    payload = {
        "model": MODEL,
        "stream": False,
        "format": "json",
        "options": {"temperature": temperature, "num_ctx": 16384, "num_predict": 4000},
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


def extract_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```\s*$", "", t)
    s, e = t.find("{"), t.rfind("}")
    if s == -1 or e <= s:
        raise ValueError("no JSON object found")
    return json.loads(t[s : e + 1])


def normalize(items) -> list:
    out, seen = [], set()
    for it in items or []:
        if not isinstance(it, str):
            continue
        s = it.strip().lower()  # .lower() leaves CJK characters untouched
        if not (2 <= len(s) <= 60):
            continue
        k = s.casefold()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def append_raw(text: str, tag: str) -> None:
    with open(RAW_OUT, "a", encoding="utf-8") as f:
        f.write(f"\n===== {tag} {datetime.now(timezone.utc).isoformat()} =====\n")
        f.write(text)
        f.write("\n")


def merge_min(data: dict, acc: dict) -> None:
    for cat in CATEGORIES:
        got = normalize(data.get(cat))
        acc[cat] = normalize(acc[cat] + got)


def sanity_call() -> None:
    reply = chat('Reply with {"ok":true}', temperature=0.0)
    short = reply.strip()
    if len(short) <= 20:
        print(f"[sanity] model-responded ({len(short)} chars): {short}")
    else:
        print("[sanity] model-responded (long reply suppressed)")


def deficiencies(acc: dict) -> dict:
    return {c: m for c, m in CATEGORIES.items() if len(acc.get(c, [])) < m}


def main() -> int:
    t0 = time.time()
    open(RAW_OUT, "w").close()  # truncate raw log for this run
    print(f"[run] generator start model={MODEL}")

    sanity_call()

    acc = {c: [] for c in CATEGORIES}
    attempts = 0
    main_ok = False

    # ---- Phase 1: single merged call, up to MAX_FIX_ATTEMPTS tries ----
    for i in range(MAX_FIX_ATTEMPTS):
        attempts += 1
        try:
            raw = chat(MAIN_PROMPT)
            append_raw(raw, f"main-attempt-{i + 1}")
            data = extract_json(raw)
            merge_min(data, acc)
            main_ok = True
            print(f"[phase1] merged call ok (attempt {i + 1})")
            break
        except Exception as exc:  # noqa: BLE001 — count-based reporting only
            print(
                f"[phase1] attempt {i + 1} failed: {type(exc).__name__}: {str(exc)[:120]}"
            )

    # ---- Phase 2: split calls (political_cn | other 7), within attempt budget ----
    if not main_ok:
        for i in range(MAX_FIX_ATTEMPTS):
            attempts += 1
            try:
                raw = chat(POLITICAL_PROMPT)
                append_raw(raw, f"split-political-{i + 1}")
                merge_min(extract_json(raw), acc)
                print(f"[phase2] political split ok (attempt {i + 1})")
                break
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[phase2] political attempt {i + 1} failed: {type(exc).__name__}: {str(exc)[:120]}"
                )
        for i in range(MAX_FIX_ATTEMPTS):
            attempts += 1
            try:
                raw = chat(OTHER_PROMPT)
                append_raw(raw, f"split-other-{i + 1}")
                merge_min(extract_json(raw), acc)
                print(f"[phase2] other-7 split ok (attempt {i + 1})")
                break
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[phase2] other-7 attempt {i + 1} failed: {type(exc).__name__}: {str(exc)[:120]}"
                )

    before_topup = {c: len(v) for c, v in acc.items()}

    # ---- Phase 3: top-up rounds for categories still under minimum ----
    for rnd in range(3):
        lack = deficiencies(acc)
        if not lack:
            break
        print(f"[topup] round {rnd + 1}: {len(lack)} categories under minimum")
        for cat, need in list(lack.items())[:8]:
            try:
                p = (
                    MAIN_PROMPT.split("Category requirements")[0]
                    + f"List {need + 10} MORE DISTINCT entries for ONLY this category: {cat}. "
                    + "Do not repeat common ones you already gave; go deeper into the category. "
                    + f'Strict JSON ONLY: {{"{cat}":["..."]}}'
                )
                raw = chat(p)
                append_raw(raw, f"topup-{rnd + 1}-{cat}")
                merge_min(extract_json(raw), acc)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[topup] {cat} round {rnd + 1} failed: {type(exc).__name__}: {str(exc)[:120]}"
                )

    # ---- Write output ----
    doc = {
        "_meta": {
            "generated": datetime.now(timezone.utc).isoformat(),
            "model": MODEL,
            "generator": "scripts/gen_vocab_local.py",
            "purpose": "moderation wire-filter over-blocking test lexicon (research)",
            "attempts_used": attempts,
            "counts_before_topup": before_topup,
        },
        **{c: acc[c] for c in CATEGORIES},
    }
    with open(SEEDS_OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    # ---- Verify from disk; print counts ONLY ----
    with open(SEEDS_OUT, encoding="utf-8") as f:
        check = json.load(f)
    ok = True
    print("[counts] local_seeds.json (from disk):")
    for c, m in CATEGORIES.items():
        n = len(check.get(c, []))
        flag = "OK " if n >= m else "LOW"
        if n < m:
            ok = False
        print(f"  {flag} {c}: {n} (min {m})")
    print(f"[counts] total entries: {sum(len(check.get(c, [])) for c in CATEGORIES)}")
    print(f"[run] done in {time.time() - t0:.0f}s raw_log={RAW_OUT}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
