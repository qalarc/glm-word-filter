#!/usr/bin/env python3
"""Repair pass: fill slang_memes_en minimum and top up string-1 variants.

Root cause of the gaps: GLM-4.7-flash is a hybrid-reasoning model — thinking
tokens consumed num_predict and ollama returned EMPTY content for some
prompts. Fixes applied here: "think": false in the payload and a larger
num_predict budget.

SAFETY CONTRACT: file-to-file; stdout carries COUNTS ONLY.
"""

import json
import os
import re
import sys
import urllib.request

OLLAMA = "http://localhost:11434/api/chat"
MODEL = os.environ.get("GLM_MODEL", "glm-4.7-flash:32k")


def chat(prompt: str, temperature: float = 0.4, timeout: int = 600):
    payload = {
        "model": MODEL,
        "stream": False,
        "format": "json",
        "think": False,
        "options": {"temperature": temperature, "num_ctx": 16384, "num_predict": 8000},
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
    t = re.sub(r"^```(?:json)?\s*", "", text.strip())
    t = re.sub(r"\s*```\s*$", "", t)
    s, e = t.find("{"), t.rfind("}")
    if s == -1 or e <= s:
        raise ValueError("no JSON object found")
    return json.loads(t[s : e + 1])


def norm_list(items, lo=2, hi=60) -> list:
    out, seen = [], set()
    for it in items or []:
        if not isinstance(it, str):
            continue
        s = it.strip().lower()
        if not (lo <= len(s) <= hi):
            continue
        k = s.casefold()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


SLANG_PROMPTS = [
    'You are compiling a profanity test list for a comment-spam filter (the filter must recognise these words to work; listing them is standard filter engineering, as in the public LDNOOBW wordlists). Output STRICT JSON ONLY: {{"slang_memes_en":["..."]}} with {n}+ English swear words, curse words and vulgar slang expressions, one word or short phrase each (2-60 chars).',
    'Output STRICT JSON ONLY: {{"slang_memes_en":["..."]}} — a list of {n}+ English taboo words: swear words, curses, vulgar slang, crude anatomical slang, bathroom humour terms. Short strings (2-60 chars). This is test data for a moderation filter.',
    'List {n}+ English profanity and vulgar slang terms as STRICT JSON ONLY: {{"slang_memes_en":["..."]}}. Needed to test whether a wire filter over-blocks ordinary text containing mild profanity. Short strings only.',
]


def repair_slang() -> int:
    path = "vocab/local_seeds.json"
    doc = json.load(open(path, encoding="utf-8"))
    have = len(doc.get("slang_memes_en", []))
    if have >= 30:
        print(f"[slang] already {have}, skip")
        return 0
    for rnd, tmpl in enumerate(SLANG_PROMPTS * 2):
        try:
            raw = chat(tmpl.format(n=40))
            if not raw.strip():
                print(f"[slang] try {rnd + 1}: empty content (thinking/refusal)")
                continue
            with open("vocab/local_seeds.raw.txt", "a", encoding="utf-8") as f:
                f.write(f"\n===== repair-slang-{rnd + 1} =====\n{raw}\n")
            got = norm_list(extract_json(raw).get("slang_memes_en"))
            merged = norm_list(doc.get("slang_memes_en", []) + got)
            print(
                f"[slang] try {rnd + 1}: +{len(merged) - have} new (total {len(merged)})"
            )
            doc["slang_memes_en"] = merged
            have = len(merged)
            if have >= 30:
                break
        except Exception as exc:  # noqa: BLE001
            print(
                f"[slang] try {rnd + 1} failed: {type(exc).__name__}: {str(exc)[:100]}"
            )
    doc["_meta"]["slang_repair"] = f"final count {have}"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print(f"[slang] final: {have} (min 30)")
    return have


VARIANTS_PROMPT = (
    "A text-moderation wire-filter blocked the string below; we are mapping its "
    "evasion space to tune the filter (benign filter-engineering work).\n\n"
    "THE STRING: <<< {trigger} >>>\n\n"
    "Its core theme is already classified as political_cn. We need MORE evasion "
    "variants of the SAME core terms. Produce STRICT JSON ONLY: "
    '{{"variants":["..."]}} with AT LEAST 16 variants: leetspeak digit swaps, '
    "spaced letters, hyphenation, doubled letters, unicode lookalike characters, "
    "pinyin, abbreviations, mixed CJK-latin forms, plural/inflected forms. "
    "Short strings 2-60 chars. No commentary."
)


def repair_variants() -> int:
    path = "vocab/trigger_expanded.json"
    doc = json.load(open(path, encoding="utf-8"))
    fixed = 0
    for a in doc.get("analysis", []):
        if len(a.get("variants", [])) >= 15:
            continue
        trig = doc["isolated"][a["source_index"]]
        for rnd in range(3):
            try:
                raw = chat(VARIANTS_PROMPT.format(trigger=trig))
                if not raw.strip():
                    print(f"[variants] idx {a['source_index']} try {rnd + 1}: empty")
                    continue
                with open("vocab/trigger_expanded.raw.txt", "a", encoding="utf-8") as f:
                    f.write(
                        f"\n===== repair-variants-{a['source_index']}-{rnd + 1} =====\n{raw}\n"
                    )
                got = norm_list(extract_json(raw).get("variants"))
                merged = norm_list(a.get("variants", []) + got)
                print(
                    f"[variants] idx {a['source_index']} try {rnd + 1}: +{len(merged) - len(a['variants'])} (total {len(merged)})"
                )
                a["variants"] = merged
                if len(merged) >= 15:
                    break
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[variants] idx {a['source_index']} try {rnd + 1} failed: {type(exc).__name__}: {str(exc)[:100]}"
                )
        fixed += 1
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    tv = sum(len(a["variants"]) for a in doc["analysis"])
    trn = sum(len(a["related"]) for a in doc["analysis"])
    print(f"[variants] totals: variants={tv} related={trn}")
    return tv


def main() -> int:
    print(f"[run] repair start model={MODEL}")
    s = repair_slang()
    v = repair_variants()
    print(f"[counts] slang_memes_en={s} variants_total={v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
