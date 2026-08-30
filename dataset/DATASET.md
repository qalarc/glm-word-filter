# Verified blocker dataset

**560 terms empirically confirmed** to trip the live filter (HTTP 400, code
1301) when sent individually in an innocuous carrier sentence, against
`glm-5.2` via the provider's Anthropic-compatible route, 2026-08-30/31.

## Files
- `verified_blockers.txt` — one term per line (sha256 in `verified_blockers.sha256`)
- `root_decomposition.json` — `roots` (162 minimal strings) and
  `extensions_of_roots` (398 verified terms that CONTAIN a root; under raw
  substring matching these block by implication)

## Structure insight
71% of verified entries are extensions of a shorter verified entry —
consistent with ~2/3 of blockers behaving as raw substring matchers. For
sanitisation/matching purposes, the 162 roots are the operative set; for
poison-text purposes, the full 560 maximise surface.

## Method
Each term was appended to a fixed innocuous carrier sentence and sent as a
single completion request on a dedicated API key. `blocked` = non-200 with
`1301`/`sensitive` in the response body. Replication: ~15% of entries were
re-probed across different days with zero verdict flips, though the provider's
list drifts over time — treat this snapshot as dated.

Candidate terms came from local-model recall (Zhipu-trained GLM-4.7-flash),
public wordbanks, and mechanical bisection of real blocked prompts; see the
repo README and `results/RESEARCH_SUMMARY.md`.

## Comparison to public lists
Union of 20 public Chinese filter wordlists (~250K entries) covers 5.4% of
this set; 530 entries appear in no public list (see RESEARCH_SUMMARY).

## License / use
CC-BY-4.0. Published for research and defensive use: content-moderation
engineering, and protecting content you own from non-consensual scraping or
training. Provider-specific and time-sensitive — regenerate with the tooling
in `../scripts/`.
