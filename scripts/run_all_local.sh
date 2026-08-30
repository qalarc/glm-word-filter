#!/usr/bin/env bash
# Driver: run the full local vocab generation pipeline with tee'd logging.
# Usage: bash scripts/run_all_local.sh
set -uo pipefail
cd "$(dirname "$0")/.."
LOG=results/logs/gen_vocab3.log
mkdir -p results/logs vocab

step() {
  echo "===== $1 =====" | tee -a "$LOG"
}

step "PART 1: gen_vocab_local.py"
python3 scripts/gen_vocab_local.py 2>&1 | tee -a "$LOG"
P1=${PIPESTATUS[0]}

step "PART 2: expand_triggers_local.py"
python3 scripts/expand_triggers_local.py 2>&1 | tee -a "$LOG"
P2=${PIPESTATUS[0]}

step "PART 3: build_pool.py"
python3 scripts/build_pool.py 2>&1 | tee -a "$LOG"
P3=${PIPESTATUS[0]}

step "SUMMARY"
echo "part1_exit=$P1 part2_exit=$P2 part3_exit=$P3" | tee -a "$LOG"
ls -la vocab/local_seeds.json vocab/trigger_expanded.json vocab/candidate_pool.json 2>&1 | tee -a "$LOG"
