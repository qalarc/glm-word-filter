# RESEARCH SUMMARY — The GLM Word-Filter Investigation
**Project:** glm_word_filter · qalarc research · 2026-08-30 → 08-31
**Status:** Complete (verification passes continue in background)
**Sanitized:** counts, methods and verdicts only — no vocabulary appears in this document.

---

## 1. What this project is

An empirical, fully-reproducible investigation of the undocumented content filter
on a commercial Chinese LLM API (Z.AI, serving GLM models — HTTP 400, code
**1301** "unsafe or sensitive content"), which was rejecting ~31% of our
production forum-analysis classifier calls. The project set out to answer three
questions:

1. **What exactly trips the filter** — individual words, phrases, or meaning?
2. **Can the blocked calls be rescued** without touching stored data?
3. **Can the knowledge be weaponized defensively** — poison text that degrades
   non-consensual scraping and AI training on content we own?

All three were answered, and two systems were built on the answers.

---

## 2. The target system: what the 1301 filter actually is

Characterized by ~4,000 controlled API probes over two days:

| # | Finding | Evidence |
|---|---------|----------|
| 1 | It is a **string list**, not a semantic classifier | Removing one specific substring unblocks an otherwise-identical prompt; a single token swap flips hard-blocked prompts to passing |
| 2 | It is **narrowly political** | 550+ English slurs/profanity/religious/sexual terms: **0 blocks**; every verified blocker is in the China-political domain |
| 3 | Some entries are **phrase-level only** | Individual words pass; only the exact 3-word collocation blocks |
| 4 | **No combination layer** | 108 cross-category pairs + 30 triples of individually-clean terms: 0 blocks |
| 5 | ~2/3 of blockers are **context-insensitive** | Fire inside an innocuous weather sentence — raw substring matching |
| 6 | The list **drifts** over days | Re-probes show entry churn; snapshots need refresh cadence |

The verified dataset: **560+ terms empirically confirmed** to block individually
(3,505 tested; precision by source: ~60% English renderings, ~30-38% v1
lexicon, ~16-18% deep-recall entries).

---

## 3. Method (all reproducible from scripts/)

- **Carrier-sentence probing** — one term at a time appended to an innocuous
  sentence; 1301 = independently-sufficient trigger.
- **Bisection isolation** — blocked prompts split into ~300-char windows,
  recursively bisected to minimal substrings; complement test (prompt minus
  trigger) proves causality.
- **Combination controls** — pairs/triples of clean terms across related
  categories, plus stability re-probes.
- **Local-model recall** — a locally-hosted GLM-4.7-flash (Zhipu-trained, so it
  holds strong priors on Chinese filter lists) generated candidate vocabulary
  across ~40 categories; 2,542-entry deep lexicon after dedup.
- **Verification pipeline** — every candidate gets a live verdict; incremental
  JSONL, resume-capable, drift-aborting.

---

## 4. Public-lists research: is this filter's vocabulary public anywhere?

We downloaded and normalized **20 public Chinese content-filter wordlists**
(~250K entries total) and measured overlap with our 560 verified blockers.

**The selected systems (public list ecosystem):**

| System / list | Entries | Covers of our 560 | Character |
|---|---|---|---|
| cjh0613/tencent-sensitive-words | 41,268 | 28 (5.0%) | Consumer-chat moderation bank |
| kaixindelele/ChatSensitiveWords | 41,376 | 28 (5.0%) | LLM-chat moderation bank (near-duplicate of above) |
| xwg666/Sensitive-words | 8,909 | 26 (4.8%) | Curated; best hit-density (4× the big banks) |
| mimikin/AI-Sensitive-Word-Bank | 11,765 | 22 (3.9%) | AI-context bank |
| houbb/sensitive-word dict (2024) | 65,141 | 17 (3.0%) | Java-library default dictionary |
| HaHaWTH/AdvancedSensitiveWords | 56,656 | 16 (3.0%) | Aggregated mega-bank |
| qloog/sensitive_words (Baidu) | 2,467 | 12 (2.3%) | Old forum-era list |
| 57ing/Sensitive-word | 13,993 | 8 (1.6%) | Generic bank |
| Citizen Lab WeChat keywords (2020) | 5,058 | 6 (1.1%) | Measured political-discussion censor |
| Citizen Lab Douyin restricted-search | 354 | 2 (0.4%) | Measured, platform-specific |
| fwwdn / selfcs stop-word packs | 303–434 | ≤0.7% | Small curated packs |
| gfwlist (domains, control) | 4,336 | 0 | URL-based, not word-based |
| English profanity repos (LDNOOBW, GPW, zacanger) | 30–2,725 | **0** | Irrelevant to this filter |

**Verdicts:**
1. **Union of all 20 lists covers only 30/560 (5.4%)** — 530 of our verified
   blockers appear in **no public list anywhere**. The z.ai filter is
   substantially custom.
2. Closest flavor: **consumer-chat moderation banks** (Tencent-style), not the
   classic political-censorship lists — and clearly not the WeChat
   discussion censor (Citizen Lab lists are a poor predictor).
3. Our 450 English/numeric blockers exist in **zero** standard profanity repos —
   this dataset is novel.
4. Practical: public banks remain useful as a **complementary broad-spectrum
   layer** (they model Tencent/ByteDance-style pipelines, which differ from
   z.ai's), but they cannot substitute for empirical verification.

---

## 5. Systems built on the findings

### 5.1 Production countermeasure (chanalyse, live on cachyos-x8664)
- **Wire-only swap map** — 96 terms (verified + mining-loop union) rewritten to
  typed placeholders in-flight; storage stays raw. Rescue 2/2 on the hardest
  blocked threads, zero false-positive cost, glm failure rate 0% since deploy.
- **glm_local backstop** — locally-hosted unfiltered GLM (Ollama over tailnet,
  model ladder glm-4.7-flash → qwen3.8, structured-outputs JSON mode) in the
  chain after the primary: `glm → glm_local → heuristic`.
- **Weekly mining loop** — newly-blocked threads are auto-mined and
  verified-merged into the map (systemd timer on prod).

### 5.2 Anti-scrape toolkit (this workspace)
- **Live-validated embeds** — `embed.html` (171KB full payload) and
  `embed_minimal.html` (verified-only) both empirically trip 1301 when ingested.
- **Tiered artifacts** — `block_verified.txt` (every entry a proven trigger) vs
  `block_full.txt` (3,678-entry candidate pool, per-entry verified flags in
  `block_full_annotated.json`).
- **dataset_tarpit** (new project, ~/projects/MASTER_PROJECTS/dataset_tarpit) —
  a poisoning service: invalid lookups receive a freshly-composed PDF of
  plausible document prose (generated by local GLM-4.7-flash) woven around
  random term subsets; every fetch is unique, so scrapers ingest
  filter-triggering content that defeats dedup and contaminates training data.

### 5.3 Research infrastructure (reusable)
Probe framework (carrier method), bisection isolator, bulk verifiers with
resume/drift-abort, collocation controls, local-model lexicon generators
(truncation-safe, salvage parsing), public-list comparison tooling, and the
full experiment log (`results/PROGRESS.md`).

---

## 6. Numbers at a glance

| Metric | Value |
|---|---|
| API calls (controlled probes) | ~4,000 |
| Terms individually tested | 3,505 |
| **Verified blockers** | **560+** |
| Verified blockers in no public list | 530 |
| Production failure rate | 31% → **0%** |
| Junk-sink KPI (unclassified threads) | 27.0% → 21.7% (trending down) |
| Lexicon pool | 3,678 entries |
| Public lists compared | 20 (~250K entries) |

## 7. Sharing & publication notes
- Private repo: `qalarc/glm-word-filter` (findings, tooling, private vocab).
  Public release gated by `RELEASE.md` (history-rewrite required).
- This summary and the method are publishable as-is; the vocabulary itself
  ships only as a deliberate artifact release.
- Feed for the qalarc.com project entry and the chanalyse article cross-link.
