# glm-word-filter

Empirical characterization of the content filter on a commercial Chinese LLM
API (Z.AI / GLM models — `HTTP 400 · code 1301` "unsafe or sensitive content"),
the verified-blocker dataset extracted from it, and the countermeasures built
on the findings.

**Companion project:** [dataset-tarpit](https://github.com/qalarc/dataset-tarpit)
— a serving layer that turns this dataset into unique poison PDFs served on
invalid lookups (anti-scraping / anti-training defence).

## Why

Our forum-analysis classifier ([chanalyse](https://github.com/fivelidz/chanalyse))
was losing ~31% of calls to undocumented filter rejections — on exactly the
politically-noisy content a moderation classifier exists to process. The
provider documents nothing about what trips the filter, so we took it apart.

## Findings (all empirically replicated, ~4,000 controlled probes)

1. **It is a string list, not a semantic classifier.** Removing one specific
   substring unblocks an otherwise-identical prompt; a single swap flips
   hard-blocked prompts to passing.
2. **It is narrowly political.** 550+ English slurs/profanity/religious/sexual
   terms: zero blocks. Every verified blocker is China-political.
3. **Some entries are phrase-level only** — individual words pass, the exact
   3-word collocation blocks.
4. **No combination layer.** 108 pairs + 30 triples of individually-clean
   terms across related categories: zero blocks.
5. **~2/3 of blockers behave as raw substring matchers** — and the dataset
   shows the consequence: of 560 verified blockers, only **162 are minimal
   roots**; the other **398 are extensions** that contain a root (71%). The
   filter's effective list is ~162 strings.
6. **The list drifts** over days — snapshots need refresh cadence.

## The dataset

**560 terms empirically verified** to trip the live filter individually
(3,505 tested), decomposed into 162 minimal roots + 398 substring-implied
extensions. Compared against 20 public Chinese filter wordlists (~250K
entries): their union covers only **5.4%** of our verified set — **530
verified blockers appear in no public list anywhere**. Closest public
analogues are consumer-chat moderation banks; classic censorship lists
(Citizen Lab's measured WeChat datasets) are poor predictors.

→ [`dataset/`](dataset/) — verified list, root decomposition, provenance and
method notes. The probe toolchain here reproduces and extends it against any
provider.

**Compared against every public list we could find** — 20 wordbanks, ~250K
entries (Tencent banks, chat-moderation packs, Citizen Lab measurements,
English profanity repos): their union covers only **5.4%** of this dataset,
and 530 of our verified blockers appear in no public list anywhere. Full
comparison table and credits: [`PUBLIC_LISTS.md`](PUBLIC_LISTS.md).

## Repo layout

- `dataset/` — the verified blocker list + root decomposition + provenance
- `scripts/` — research toolchain: carrier-sentence prober, bisection
  isolator, bulk verifiers (resume + drift-abort), local-model lexicon
  generators (truncation-safe JSON salvage), public-list comparator,
  acceptance tester, artifact builder
- `results/` — sanitized experiment summaries + full progress log
- `MASTER_PLAN.md` / `results/RESEARCH_SUMMARY.md` — method + write-ups

## Countermeasures built on it

- **Wire-only swap map** (production, live): trigger strings → typed
  placeholders in-flight; storage stays raw. Rescue 2/2 on the hardest
  blocked threads; classifier failure rate 31% → 0%.
- **Local backstop**: unfiltered self-hosted GLM in the classifier chain.
- **Weekly mining loop**: newly-blocked threads auto-mined and verified-merged.

## Ethics

- All probing on dedicated API keys against the provider's documented public
  API.
- The dataset is published for research and defensive use: content-moderation
  engineering and protecting content you own from non-consensual
  scraping/training — the same category as existing public censorship
  measurement research.
- It is provider-specific and time-sensitive: filters rotate. Refresh with
  the included tooling.

## License

Code: MIT (see `LICENSE`). Dataset: CC-BY-4.0 with a defensive-use notice —
see `dataset/DATASET.md`.
