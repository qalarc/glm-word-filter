# Verified blockers — pool verification

Generated: 2026-08-30 16:07:16  |  Source: `results/pool_verdicts_20260830_151927.jsonl`

## Main layer (carrier + term) — per category

| category | tested | blocked | net_err |
|---|---|---|---|
| ethnic_racial_en | 50 | 0 | 0 |
| gender_sexual_en | 30 | 0 | 0 |
| other_sensitive_en | 30 | 0 | 0 |
| political | 17 | 3 | 0 |
| political_cn | 130 | 19 | 0 |
| religious_en | 25 | 0 | 0 |
| sexual_explicit_en | 30 | 0 | 0 |
| slang_memes_en | 39 | 0 | 0 |
| unmapped | 32 | 0 | 0 |
| violence_gore_en | 25 | 0 | 0 |
| **TOTAL** | **408** | **22** | **0** |

## Context layer (innocuous templates x blocked/core terms)

| template | tested | blocked | net_err |
|---|---|---|---|
| T1 | 26 | 15 | 0 |
| T2 | 26 | 15 | 0 |
| T3 | 26 | 24 | 0 |

- verified_single (blocked in plain carrier): 24
- context_sensitive (carrier-pass but block in >=1 template): 0
- clean (carrier-pass, no template block): 386
- carrier-blocked terms that STILL block in >=1 template: 15

*(No term content appears in this file by design; see
`results/pool_verdicts_20260830_151927.jsonl` and `results/VERIFIED_BLOCKERS.json`.)*
