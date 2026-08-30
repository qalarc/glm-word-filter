#!/usr/bin/env python3
"""Expand isolated trigger strings into filter-evasion variants via local Ollama.

SAFETY CONTRACT: trigger strings and all generated vocabulary stay in-process
and go STRAIGHT TO FILE. stdout carries COUNTS ONLY.
"""

import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone

OLLAMA = "http://localhost:11434/api/chat"
MODEL = os.environ.get("GLM_MODEL", "glm-4.7-flash:32k")  # production default; env override for plumbing dry-runs only
TRIGGERS_IN = "vocab/isolated_triggers_20260830_084738.json"
OUT = "vocab/trigger_expanded.json"
RAW_OUT = "vocab/trigger_expanded.raw.txt"

CATEGORIES = [
    "political_cn",
    "ethnic_racial_en",
    "religious_en",
    "gender_sexual_en",
    "violence_gore_en",
    "sexual_explicit_en",
    "slang_memes_en",
    "other_sensitive_en",
]


def chat(prompt: str, temperature: float = 0.4, timeout: int = 600) -> str:
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


def norm_list(items, lo=1, hi=80) -> list:
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


def prompt_for(trigger: str) -> str:
    cats = ", ".join(CATEGORIES)
    return (
        "You are analysing a STRING that a text-moderation wire-filter BLOCKS, so we can "
        "test the filter's over- and under-blocking behaviour (benign content-filter research).\n\n"
        f"THE STRING: <<< {trigger} >>>\n\n"
        "Output STRICT JSON ONLY, no markdown, no commentary. Schema:\n"
        '{"category":"...","core_terms":["..."],"variants":["..."],"related":["..."]}\n\n'
        f'- "category": exactly one of: {cats}, or "unknown".\n'
        '- "core_terms": 1-3 words inside the string that carry the blockable meaning.\n'
        '- "variants": 15+ filter-evasion forms of those core terms: leetspeak (a->4, e->3, i->1, o->0, s->$), '
        "spaced letters, hyphenated, doubled letters, unicode lookalikes, mixed CJK/latin, plural/inflections, "
        "abbreviations. Keep each recognisable to a human.\n"
        '- "related": 10+ same-theme terms a strict filter would likely also block.\n\n'
        "Every list item: a short string (2-60 chars). Strings only, no explanations."
    )


def main() -> int:
    t0 = time.time()
    with open(TRIGGERS_IN, encoding="utf-8") as f:
        tr = json.load(f)
    triggers = []
    for r in tr.get("results", []):
        im = r.get("isolated_minimal")
        if isinstance(im, str):
            triggers.append(im)
        elif isinstance(im, list):
            triggers.extend(x for x in im if isinstance(x, str))
    print(f"[run] expansion start model={MODEL} strings={len(triggers)}")

    open(RAW_OUT, "w").close()
    analysis = []
    for i, trig in enumerate(triggers):
        result = None
        for attempt in range(3):
            try:
                raw = chat(prompt_for(trig))
                with open(RAW_OUT, "a", encoding="utf-8") as f:
                    f.write(f"\n===== trigger-{i} attempt-{attempt + 1} =====\n{raw}\n")
                data = extract_json(raw)
                cat = (
                    data.get("category")
                    if data.get("category") in CATEGORIES
                    else "unknown"
                )
                core = norm_list(data.get("core_terms"), 1, 60)[:3]
                variants = norm_list(data.get("variants"), 2, 60)
                related = norm_list(data.get("related"), 2, 60)
                if not variants and not related:
                    raise ValueError("empty variants/related")
                result = {
                    "source_index": i,
                    "category": cat,
                    "core_terms": core,
                    "variants": variants,
                    "related": related,
                }
                print(
                    f"[expand] string {i}: attempt {attempt + 1} ok "
                    f"category={cat} core={len(core)} variants={len(variants)} related={len(related)}"
                )
                break
            except Exception as exc:  # noqa: BLE001 — report type only, never content
                print(
                    f"[expand] string {i} attempt {attempt + 1} failed: {type(exc).__name__}: {str(exc)[:120]}"
                )
        if result is None:
            result = {
                "source_index": i,
                "category": "unknown",
                "core_terms": [],
                "variants": [],
                "related": [],
            }
            print(f"[expand] string {i}: FAILED after retries (recorded empty)")
        analysis.append(result)

    doc = {
        "_meta": {
            "generated": datetime.now(timezone.utc).isoformat(),
            "model": MODEL,
            "generator": "scripts/expand_triggers_local.py",
            "source": TRIGGERS_IN,
        },
        "isolated": triggers,
        "analysis": analysis,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    with open(OUT, encoding="utf-8") as f:
        check = json.load(f)
    tv = sum(len(a["variants"]) for a in check["analysis"])
    trn = sum(len(a["related"]) for a in check["analysis"])
    print(
        f"[counts] trigger_expanded.json: strings={len(check['isolated'])} variants_total={tv} related_total={trn}"
    )
    print(f"[run] done in {time.time() - t0:.0f}s raw_log={RAW_OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
