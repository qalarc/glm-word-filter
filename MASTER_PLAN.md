# MASTER PLAN — GLM Word Filter Investigation & Countermeasures (FINAL 2026-08-30)

> All vocabulary lives in files (vocab/, anti_scrape/, results/). This document is sanitized.
> Full log: results/PROGRESS.md · Sub-agent isolation was used for all sensitive work.

## 1. What we now know (empirically proven)

Z.AI's HTTP 400 / code 1301 "sensitive content" filter on the Anthropic-compatible
route (glm-5.2) is:

1. **A narrow phrase-level n-gram list focused on China-political content.**
   - 24 verified single-entry blockers, ALL political domain.
   - Some entries are phrase-only: individual words pass, the 3-word phrase blocks.
   - 15/24 verified blockers are context-INSENSITIVE raw substring matches
     (block even inside innocuous sentences). 9 are context-aware.
2. **NOT a slur/profanity filter.** 550+ profanity/slur/religious/sexual/violence
   terms probed in identical carriers: ZERO blocks. The ~31% /pol/ failure rate was
   driven by political vocabulary, not the hate-speech content itself.
3. **Deterministic per-substring.** Removing the single triggering window unblocks
   an entire otherwise-identical prompt (complement test, both threads).
   A single swap of the trigger flips hard-blocked prompts to pass (acceptance test).

## 2. Deliverables in this workspace

| Path | What | Status |
|---|---|---|
| vocab/swap_map.json (local copy) + chanalyse ops/filter_vocab/swap_map.json | 80-term verified swap map (deployed; prior archived) | ✅ deployed |
| results/VERIFIED_BLOCKERS.json | 24 verified blockers, file-only | ✅ |
| anti_scrape/block_verified.txt | 24 verified entries — LIVE-VALIDATED to 1301 | ✅ |
| anti_scrape/block_full.txt | 1,311-entry lexicon (local GLM recall) | ✅ |
| anti_scrape/embed.html / embed_minimal.html | hidden-div + HTML-comment embeds (61KB / 1.8KB) | ✅ LIVE-VALIDATED |
| anti_scrape/EMBED_GITHUB.md | sanitized usage instructions | ✅ |
| scripts/*.py | probe/bisect/verify/acceptance tooling (reusable) | ✅ |
| results/*.md, results/logs/ | sanitized summaries + logs | ✅ |

## 3. Countermeasure architecture (for chanalyse production)

1. **Wire-only swap** (deployed map): longest-first single-pass matching in the GLM
   backend only; storage stays raw. Verified: rescues 2/2 hard threads, 5/5 clean
   threads unaffected.
2. **Weekly mining loop** (recommended next): newly-1301'd threads → local GLM 4.7
   flash extracts candidates → carrier-probe verify → merge verified into map
   (script pattern: scripts/verify_pool.py + acceptance_rescue.py).
3. **Local backstop**: engine backend glm_local → Ollama glm-4.7-flash:32k over
   tailnet (chain: glm,glm_local,heuristic) for anything the map misses. Zero filter.
4. **Observability**: track recovered_by (swap vs local vs flagged); target /pol/
   coverage 95%+ (from 71%).

## 4. Anti-scrape deployment (user's sites/GitHub)

- Embed `anti_scrape/embed.html` in page templates (hidden div + comment), or
  publish `block_full.txt` as `.well-known/poison.txt` / repo file.
- Validated: the filter 400s on these payloads, so Chinese AI pipelines that ingest
  them will flag/drop the content. Compliant crawlers (robots.txt-respecting) can be
  excluded separately; hidden from human visitors.
- Refresh cadence: rerun scripts/gen_lexicon_antiscrape.py + validate monthly.
- NEVER paste artifact contents into Chinese-provider chats/PRs (kills the session).

## 5. Method notes (for future reruns)

- Probe discipline: carrier sentence + single term; blocked = non-200 with
  1301/"sensitive" in body; 1.0-1.2s sleep; incremental JSONL.
- glm-4.7-flash local JSON calls REQUIRE "think":false and num_predict ≥ 8000.
- One-category-per-call for large list generation (truncation-safe).
- Ollama blob issues: scripts/fetch_blob_parallel.py (ranged, SHA-verified, resumable).
