# Public filter lists — and how this dataset differs

Our verified dataset was built independently, but we compared it against every
public Chinese content-filter wordlist we could find. This file credits those
projects and states precisely where the overlap ends. All numbers below are
measured (see `results/RESEARCH_SUMMARY.md` and `scripts/compare_public.py`).

## The public lists we compared against

| List | Entries (norm.) | Covers of our 560 | What % of the list we confirmed |
|---|---|---|---|
| [cjh0613/tencent-sensitive-words](https://github.com/cjh0613/tencent-sensitive-words) | 41,268 | 28 (5.0%) | 0.07% |
| [kaixindelele/ChatSensitiveWords](https://github.com/kaixindelele/ChatSensitiveWords) | 41,376 | 28 (5.0%) | 0.07% |
| [xwg666/Sensitive-words](https://github.com/xwg666/Sensitive-words) | 8,909 | 26 (4.8%) | 0.29% |
| [mimikin/AI-Sensitive-Word-Bank](https://github.com/mimikin/AI-Sensitive-Word-Bank) | 11,765 | 22 (3.9%) | 0.18% |
| [houbb/sensitive-word](https://github.com/houbb/sensitive-word) (dict 2024) | 65,141 | 17 (3.0%) | 0.03% |
| [HaHaWTH/AdvancedSensitiveWords](https://github.com/HaHaWTH/AdvancedSensitiveWords) | 56,656 | 16 (3.0%) | 0.03% |
| [qloog/sensitive_words](https://github.com/qloog/sensitive_words) (Baidu) | 2,467 | 12 (2.3%) | 0.49% |
| [57ing/Sensitive-word](https://github.com/57ing/Sensitive-word) | 13,993 | 8 (1.6%) | 0.06% |
| [Citizen Lab WeChat keywords](https://github.com/citizenlab/tiktok-report-data) (2020) | 5,058 | 6 (1.1%) | 0.04% |
| [Citizen Lab Douyin restricted-search](https://github.com/citizenlab/tiktok-report-data) | 354 | 2 (0.4%) | 0.00% |
| [fwwdn/sensitive-stop-words](https://github.com/fwwdn/sensitive-stop-words), [selfcs/stop-and-sensitive-words](https://github.com/selfcs/stop-and-sensitive-words) | 303–434 | ≤0.7% | 0% |
| [gfwlist](https://github.com/gfwlist/gfwlist) (domains — control) | 4,336 | 0 | 0% |
| English profanity banks: [LDNOOBW](https://github.com/LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words), [google-profanity-words](https://github.com/coffee-and-fun/google-profanity-words), [zacanger/profane-words](https://github.com/zacanger/profane-words) | 30–2,725 | **0** | 0% |

**Union of all 20 lists: 30/560 (5.4%). 530 of our verified blockers appear in
no public list anywhere.**

Credit where due: these banks were useful as candidate pools and comparison
baselines, and the Tencent/Chat banks are the closest public analogues to this
filter's flavour (they cover ~25% of the Chinese-language slice of our set).
They are also, by our measurements, **~250,000 entries of mostly non-blocking
material** for this provider — which is exactly the difference that matters.

## How this dataset is different

1. **Every entry is a live-measured verdict, not curation.** Public lists are
   aggregated hearsay — assembled from forum posts, vendor SDKs and each
   other, with no per-entry testing. We probed every one of our 3,505
   candidates against the real API, one term at a time, and kept only the 560
   that verifiably block. When we checked the big public banks against the
   live filter, between 99.5% and 99.97% of their entries did nothing.
2. **Provider-specific ground truth.** A list that says "sensitive" means
   nothing without saying *to whom*. This dataset states its target (Z.AI /
   GLM-5.2, Anthropic-compatible route, 2026-08), so it's reproducible and
   falsifiable — and its drift over time is measurable with the included
   tooling.
3. **Structural metadata, not just strings.** Each entry carries behavioural
   data: single-term vs phrase-level blocking, context-sensitive vs raw
   substring matching, and the root/extension decomposition (162 minimal
   roots; 398 verified extensions blocked by implication). Public lists are
   flat string bags.
4. **Complementary, not competing.** The public banks model a *different*
   thing: broad-spectrum coverage across many Chinese platforms (Tencent
   pipelines, ByteDune-style moderation, forum-era filters). For
   broad-spectrum anti-scrape payloads, mixing public banks with our verified
   set is the right play; for provider-exact work (sanitisation, rescue,
   evasion research), verified data is the only kind that works.

## Reproducing the comparison

`scripts/compare_public.py` (in this repo's private research workspace /
methodology documented in `results/RESEARCH_SUMMARY.md`) downloads the lists,
normalises them (utf-8/gb18030, dedupe), and measures substring + exact
overlap against the verified set. Counts only — no list contents are
republished here beyond our own verified entries.
