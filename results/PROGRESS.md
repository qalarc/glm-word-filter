# GLM Word Filter Investigation — Progress Log (SANITIZED)

> Contract: no trigger vocabulary ever appears in this file or in orchestrator chat.
> Detail lives ONLY in vocab/ and results/ JSONs. Everything here is counts/verdicts.

## Setup
- Workspace: ~/projects/GLM_projects/investigation/glm_word_filter
- Test API key: dedicated key in .env (ZAI_API_KEY), overrides production for all probes
- Endpoint: api.z.ai Anthropic-compatible route, model glm-5.2
- Local model: Ollama glm-4.7-flash:32k (superlocal, no filter)
- Source app: ~/projects/MASTER_PROJECTS/chanalyse (ops/filter_probe.py reused)
- Sub-agent isolation in effect: orchestrator never touches sensitive content

## Phase A — C0/C1/C2 rescue probe + per-term synthetic (DONE 08:35)
- 93 API calls, 486s. Carrier baseline: pass.
- c0-blocked: 2 (IDs 136080, 138356). c1/c2 rescue with OLD 62-term map: 0/2 (map insufficient)
- Synthetic: 5/62 blocked alone — eth 0/37, pol 5/25

## Bisection — exact trigger isolation (DONE 08:47)
- 32 calls. Each thread: 8 windows → 1 blocking → 1 minimal piece (50 chars) → complement pass
- VERDICT: wordlist behavior (single substring carries the whole 1301)

## Term probes (DONE 11:21)
- Tier 1 decomposition: full isolated string blocks; 0 tokens, 0 bigrams, exactly 1 TRIGRAM blocks
- Tier 2: 0/500 corpus-frequent profanity/slur terms block

## Pool verification (DONE 15:19) — 391 calls, 408/408 resolved
- Blocked: 22/408 — ALL CN-political domain (19 political_cn + 3 political)
- ZERO blocks: ethnic 0/50, gender 0/30, religious 0/25, explicit 0/30, slang 0/39, violence 0/25
- Context layer: 15/24 blockers = raw substring matchers; 9 context-aware; 0 template-amplified FPs
- Total verified single blockers: 24 → results/VERIFIED_BLOCKERS.json

## Filter characterization (final)
- Z.AI 1301 = NARROW PHRASE-LEVEL POLITICAL N--GRAM LIST (China-political domain)
- NOT a slur/profanity filter: 550+ such terms tested clean
- Some entries phrase-level only (individual words pass; trigram blocks)

## Local vocab pipeline (DONE)
- Ollama blob repaired (parallel ranged fetch, SHA-verified)
- 318 local seeds (89 political_cn zh+en) + trigger expansion (30 variants, 22 related) → 408 pool
- "think":false required for glm-4.7-flash JSON calls

## Anti-scrape lexicon (DONE) — local GLM recall
- 1,311 distinct entries (1,222 new): zh 9 cats (276 events, 90 dissidents, 84 policies...), en 465, phrases 278
- vocab/antiscrape_lexicon.json + anti_scrape/lexicon_flat.txt

## Anti-scrape artifacts (DONE — LIVE-VALIDATED)
- block_verified.txt (24) → BLOCKED ✅ · embed.html (1,311, 61KB) → BLOCKED ✅ · 200-sample → BLOCKED ✅ · control pass ✅
- embed_minimal.html (24) · EMBED_GITHUB.md + README.md (sanitized, 0 term leaks)

## Acceptance rescue test (DONE 17:31) — PASSED round 1
- 136080: c0=BLK → c1=pass (1 swap) · 138356: c0=BLK → c1=pass (1 swap)
- Spot-check: 5/5 clean threads still pass
- Map deployed to chanalyse: 80 terms (old archived)
- 10 API calls total

## Production ops (DONE 18:26-19:00)
- Map shipped to cachyos-x8664 prod (sha-verified, service restarted, commit 97992a7)
- REGRESSION CAUGHT: ship had dropped 16/17 mining-loop entries → union map built
  (96 terms) from mining output + verified set, reshipped, commit a0f9b2d
- GLM_WIRE_SWAP=1 added to superlocal engine/.env (failover drift fix)
- qalarc/glm-word-filter PRIVATE repo created (78 files, RELEASE.md checklist)

## Deep lexicon v2 + mass verification (DONE 21:00-21:40)
- Deep generation: 26 categories via local GLM → +2,224 new, 2,542 total in v2
- v1 verification complete (incl. recovered aborted run): 1,372+ verdicts
- VERIFIED BLOCKERS: 337 unique terms (union of all sources) + 1 isolated phrase
  - precision: en ~60% / v1 overall ~28-38% / deep-gen ~18% and falling (expected)
  - 2,107+ tested total, 1,770 clean
- Artifacts v2 rebuilt + LIVE-VALIDATED (carrier pass; verified-block → 1301; embed → 1301):
  block_verified.txt (338) · block_full.txt (3,678 union) · embed.html (171KB)
  · embed_minimal.html (23.6KB)
- v2-delta verification running in background (cap 1,400 calls, ~2.5h) — will enrich
  verified subset; artifacts refreshable via scripts/build_artifacts_v2.py

## Collocation experiment (DONE 22:20) — DECISIVE NEGATIVE
- Question: do individually-CLEAN innocuous terms block as combinations?
- 108 pairs (6 semantic groups: person×event, org×event, region×policy,
  meme×meme, person×accusation, date×event): 0 blocked
- 30 triples (person+event+accusation — replication of the proven pattern): 0 blocked
- 20 stability controls: 0 blocked (filter consistent)
- CONCLUSION: filter phrase entries are SPECIFIC EXACT STRINGS, not category
  semantics. block_full's unverified bulk is clean-for-z.ai noise; its value is
  broad-spectrum (other Chinese filters) + mining pool only.
  Proven anti-scrape payload = block_verified / embed_minimal.

## Remaining (optional next)
- Background: v2 delta verification (lexicon_verdicts_v2.jsonl growing) + gap v3 after
- Tiered artifacts refresh when verification completes (scripts/build_artifacts_v2.py)
- Weekly mining loop on newly-blocked threads (Phase C of chanalyse plan)
- Local backstop backend (glm_local → Ollama over tailnet)
- Monitor: /pol/ coverage 71% → target 95%+; junk-sink KPI was 27.0% → 21.7%
  post-deploy (n=138, re-audit after ~500 more rows)

## Public-lists comparison (DONE 2026-08-31 morning)
- 14 additional public Chinese filter lists downloaded (data/pub/, SOURCES2.md)
  - biggest: houbb 65K, HaHaWTH 56K, tencent-sensitive-words 41K, ChatSensitiveWords 41K
- OVERLAP with our 560 verified z.ai blockers: UNION of all 20 public lists
  contains only 30/560 (5.4%) → 530 verified blockers appear in NO public list
- Best single predictors: tencent-sensitive-words / ChatSensitiveWords
  (28/560, ~25% of CJK slice — consumer-chat-style banks, not political censors)
- Citizen Lab WeChat lists: poor model (6/560)
- English/digit blockers (450): in ZERO standard profanity repos
- CONCLUSION: z.ai's filter is substantially custom; our verified set is a
  novel dataset. Public banks are complementary broad-spectrum coverage
  (Tencent/WeChat-style pipelines), not a substitute.
