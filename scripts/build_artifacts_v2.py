#!/usr/bin/env python3
"""build_artifacts_v2.py — final anti-scrape artifacts from the FULL verified set.

Inputs:
  results/VERIFIED_BLOCKERS_ALL.json  (verified_single = union of all sources)
  vocab/isolated_triggers_*.json      (mechanically-isolated phrases)
  anti_scrape/lexicon_flat.txt        (v1, 1,311)
  anti_scrape/lexicon_flat_v2.txt     (v2, 2,542)

Outputs (anti_scrape/):
  block_verified.txt   — all verified blockers + isolated phrases
  block_full.txt       — union of v1+v2 flats, deduped
  embed.html           — hidden-div + HTML-comment embed (full union)
  embed_minimal.html   — verified-only embed
  counts json          — results/logs/artifacts_v2_counts.json

Validation (separate step in this script, 3 API calls):
  v1 carrier control (expect pass) · v2 verified-block payload (expect 1301)
  · v3 embed.html payload (expect 1301)  → results/antiscrape_validation_v2.json

SAFETY: file-to-file. stdout = counts only.
"""

from __future__ import annotations

import glob
import json
import os
import sys
import time

import httpx

WORK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WORK)

ANTI = "anti_scrape"
CARRIER = (
    "The weather report for the coastal region mentioned strong winds and "
    "light rain expected by evening, with temperatures staying mild throughout."
)


def load_env() -> dict:
    env = {}
    for line in open(".env"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def call(env: dict, prompt: str) -> tuple[bool, str]:
    try:
        r = httpx.post(
            env["ZAI_BASE_URL"],
            json={
                "model": env["ZAI_MODEL"],
                "max_tokens": 64,
                "messages": [{"role": "user", "content": prompt}],
            },
            headers={
                "x-api-key": env["ZAI_API_KEY"],
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=120,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"net-err-{type(e).__name__}"
    if r.status_code == 200:
        return False, "ok"
    body = r.text[:300]
    blocked = ("1301" in body) or ("sensitive" in body.lower())
    return blocked, f"HTTP{r.status_code}{'-1301' if blocked else ''}"


def main() -> int:
    # ── inputs ────────────────────────────────────────────────────────────
    ver = json.load(open("results/VERIFIED_BLOCKERS_ALL.json"))
    verified = sorted(set(ver.get("verified_single", [])))
    isolated: list[str] = []
    for f in sorted(glob.glob("vocab/isolated_triggers_*.json")):
        d = json.load(open(f))
        for r in d.get("results", []):
            isolated.extend(r.get("isolated_minimal", []))
    isolated = [s for s in dict.fromkeys(isolated) if s]

    flats: list[str] = []
    for p in ("anti_scrape/lexicon_flat.txt", "anti_scrape/lexicon_flat_v2.txt"):
        if os.path.exists(p):
            for ln in open(p, encoding="utf-8"):
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    flats.append(ln)
    seen: set[str] = set()
    full: list[str] = []
    for t in flats:
        n = t.casefold()
        if n not in seen:
            seen.add(n)
            full.append(t)

    block_ver = sorted(set(verified) | set(isolated), key=str.casefold)

    # ── artifacts ─────────────────────────────────────────────────────────
    hdr_v = (
        "# Anti-scrape poison text (VERIFIED entries) — for the site owner's own properties.\n"
        "# Every entry below empirically confirmed to trip the live filter (HTTP 1301).\n"
        "# DO NOT paste into chats/PRs of Chinese AI providers.\n"
    )
    hdr_f = (
        "# Anti-scrape poison text (FULL lexicon) — mix of verified and locally-recalled entries.\n"
        "# For the site owner's own properties. DO NOT paste into chats/PRs of Chinese AI providers.\n"
    )
    with open(f"{ANTI}/block_verified.txt", "w", encoding="utf-8") as f:
        f.write(hdr_v + "\n".join(block_ver) + "\n")
    with open(f"{ANTI}/block_full.txt", "w", encoding="utf-8") as f:
        f.write(hdr_f + "\n".join(full) + "\n")

    def html_payload(entries: list[str]) -> str:
        blob = ", ".join(entries)
        return (
            "<!-- anti-scrape poison text: verified/recalled filter-trigger vocabulary.\n"
            "     Hidden from humans; poisons non-compliant AI scrapers. -->\n"
            f'<div style="display:none" aria-hidden="true" data-antiscrape>{blob}</div>\n'
            f"<!-- {blob} -->\n"
        )

    with open(f"{ANTI}/embed.html", "w", encoding="utf-8") as f:
        f.write(html_payload(full))
    with open(f"{ANTI}/embed_minimal.html", "w", encoding="utf-8") as f:
        f.write(html_payload(block_ver))

    # ── validation (3 calls) ─────────────────────────────────────────────
    env = load_env()
    val = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    val["v1_carrier"] = dict(zip(("blocked", "detail"), call(env, CARRIER)))
    time.sleep(1.2)
    ver_payload = "Here is my site's footer content for review:\n\n" + "\n".join(
        block_ver
    )
    val["v2_verified_block"] = dict(zip(("blocked", "detail"), call(env, ver_payload)))
    time.sleep(1.2)
    emb = open(f"{ANTI}/embed.html", encoding="utf-8").read()
    val["v3_embed_html"] = dict(zip(("blocked", "detail"), call(env, emb[:100_000])))
    val["all_pass"] = (
        not val["v1_carrier"]["blocked"]
        and val["v2_verified_block"]["blocked"]
        and val["v3_embed_html"]["blocked"]
    )
    json.dump(val, open("results/antiscrape_validation_v2.json", "w"), indent=1)

    counts = {
        "verified_entries": len(block_ver),
        "isolated_phrases": len(isolated),
        "full_entries": len(full),
        "embed_html_bytes": os.path.getsize(f"{ANTI}/embed.html"),
        "embed_minimal_bytes": os.path.getsize(f"{ANTI}/embed_minimal.html"),
        "validation": {k: v for k, v in val.items() if k != "ts"},
    }
    json.dump(counts, open("results/logs/artifacts_v2_counts.json", "w"), indent=1)
    print(json.dumps(counts, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
