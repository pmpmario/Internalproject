#!/usr/bin/env bash
# Train a Jeff LoRA adapter on the rows written by `python -m triage.jeff_rows`.
# Needs an NVIDIA GPU (training) and a checkout of https://github.com/firelex/jeff.
#
#   scripts/train_jeff_adapter.sh JEFF_DIR [LR]
#
# JEFF_DIR  the jeff checkout (uv sync --extra lora --extra cuda already run there)
# LR        LoRA learning rate (default 1e-4). Jeff does not publish the rate of its own
#           adapters: try 5e-5, 1e-4 and 2e-4 and keep the one with the lowest development
#           loss (runs/<run>/events.jsonl). Never choose on test.
#
# Writes $JEFF_DIR/checkpoints/servicenow-<date>/ (the "final" link is the adapter) and
# copies it to $JEFF_DIR/adapters/servicenow, the folder jeff-serve loads it from.
set -euo pipefail
if [[ "$#" -lt 1 ]]; then
  echo "Usage: scripts/train_jeff_adapter.sh JEFF_DIR [LR]" >&2
  exit 2
fi
jeff=$(cd "$1" && pwd) lr=${2:-1e-4}
triage=$(cd "$(dirname "$0")/.." && pwd)
triage_python=${TRIAGE_PYTHON:-python}   # the python with servicenow-triage installed
data=$triage/data/jeff
run=servicenow-$(date +%Y%m%d-%H%M)
for file in train development calibration test; do
  [[ -f "$data/$file.jsonl" ]] || { echo "Missing $data/$file.jsonl: run python -m triage.jeff_rows first" >&2; exit 1; }
done
cd "$jeff"

# 1. Check the rows: valid, no copies of evaluation tickets in train, and what a cheating model could use.
mkdir -p "runs/$run"
for file in train development calibration test; do uv run jeff-kit check-rows "$data/$file.jsonl"; done
leak_check() {
  uv run jeff-kit leak-check --train "$data/train.jsonl" --report "runs/$run/leaks.json" \
    --against "$data/test.jsonl" "$data/development.jsonl" "$data/calibration.jsonl"
}
if ! leak_check; then  # repeated tickets: drop the training copies, then check again
  (cd "$triage" && "$triage_python" -m triage.jeff_rows --out "$data" --drop-leaks "$jeff/runs/$run/leaks.json")
  leak_check
fi
# One shortcut report per question: the fields have different options, so mixing them only reports that.
for field in cmdb_ci assignment_group category subcategory priority; do
  mkdir -p "runs/$run/$field"
  for file in train test; do
    grep -F "\"field\": \"$field\"" "$data/$file.jsonl" > "runs/$run/$field/$file.jsonl" || true
  done
  [[ -s "runs/$run/$field/train.jsonl" && -s "runs/$run/$field/test.jsonl" ]] || continue
  uv run jeff-kit shortcut-report --train "runs/$run/$field/train.jsonl" --test "runs/$run/$field/test.jsonl" \
    --out "runs/$run/shortcuts-$field.md" --text-field details
done
echo "Read runs/$run/shortcuts-*.md before training; fix the data if a flag is not the meaning of a label."

# 2. The v1.3 base the adapter is trained on (and served with).
base=jeff-base-v1.3
[[ -f "$base/decision_config.json" ]] || uv run hf download mstrasser/jeff-base --revision v1.3 --local-dir "$base"
read -r model revision < <(uv run python -c "import json; c = json.load(open('$base/decision_config.json')); print(c['base_model'], c['revision'])")

# 3. LoRA training: one epoch, checkpoints chosen on development, temperature fitted on calibration.
export JEFF_EVENTS="runs/$run/events.jsonl" PYTHONUNBUFFERED=1
uv run jeff-train --train "$data/train.jsonl" --development "$data/development.jsonl" \
  --temperature "$data/calibration.jsonl" --run "runs/$run" --output "checkpoints/$run" \
  --base-model "$model" --revision "$revision" --initial-checkpoint "$base" \
  --lora-rank 16 --lr "$lr" --epochs 1 --seed 20260920 --batch-size 32 --effective-batch-size 256 \
  --token-budget 8192 --max-length 8192 --cpu-threads 16 --eval-every 20 --public-eval-every 1000000 --resume-every 50

# 4. Install it for jeff-serve: every folder under adapters/ is one adapter, named by the folder.
mkdir -p adapters
rm -rf adapters/servicenow
cp -rL "checkpoints/$run/final" adapters/servicenow
echo "Adapter ready: $jeff/adapters/servicenow"
echo "Serve:  JEFF_CHECKPOINT=$base JEFF_ADAPTERS=adapters PORT=8765 uv run --extra lora jeff-serve"
echo "Score:  python -m triage.jeff_client --rows $data/test.jsonl --model servicenow"
