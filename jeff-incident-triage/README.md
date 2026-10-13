# Jeff incident triage

[Jeff](https://github.com/firelex/jeff) is a 0.8B decision model. You give it a ticket and a question with a list of options; it returns a calibrated probability per option in about 30 ms on a GPU. It is not fine-tuned as a whole: you train a small **LoRA adapter** on the Jeff v1.3 base, the same way Jeff's own `triage` adapter was trained (91.5% on its test set).

Here one adapter answers five questions per ticket: CI, assignment group, category, subcategory and priority. It is an alternative to the sklearn model in [`../servicenow-triage`](../servicenow-triage). Train both on the same tickets and keep the better one per field.

This folder reuses `servicenow-triage`'s text cleaning, label folding and `config.yaml`, so both models see the same cleaned tickets.

| File | What it does |
|---|---|
| `src/jeff_triage/rows.py` | Tickets → Jeff training rows, time split, `options.json` |
| `src/jeff_triage/client.py` | Asks a running `jeff-serve`: predictions, or accuracy on a held-out file |
| `scripts/train_jeff_adapter.sh` | Jeff's data checks, then LoRA training on the v1.3 base |
| `jeff_options.yaml` | Plain-words descriptions of your teams, CIs and priorities |
| `examples/my_tickets.json` | The `my_tickets` input format |

```
my_tickets / ServiceNow export ─► jeff_triage.rows ─► data/jeff/{train,development,calibration,test}.jsonl + options.json
                                                          │
                     jeff-kit checks ◄────────────────────┘  (valid rows, no leaks, shortcut report)
                           │
                     jeff-train --lora-rank 16 ─► adapters/servicenow ─► jeff-serve ◄── jeff_triage.client
```

## 1. Your tickets → Jeff rows

Each ticket becomes one row per field whose answer you know. A row is a ticket (the *state*), a question with its options, and the right option (the *label*). The same `clean_text()` as the sklearn model removes reply chains and masks emails, phone numbers, IPs, PNRs and staff numbers.

Your `my_tickets` format works as it is: save the list as JSON (see [`examples/my_tickets.json`](examples/my_tickets.json)). The `expected_*` keys are the labels and all of them are optional. `cmdb_ci` (the CI the caller picked) is ignored, as in the sklearn model. `opened_at` is read day-first (`15-09-2026` = 15 September).

```python
import json
json.dump(my_tickets, open("data/my_tickets.json", "w"), indent=2)
```

```bash
cd jeff-incident-triage
uv venv && uv pip install -e ".[dev]"       # also installs ../servicenow-triage
python -m jeff_triage.rows --data data/my_tickets.json --out data/jeff               # my_tickets format
python -m jeff_triage.rows --data data/raw_incident_reassign.csv --out data/jeff     # or the export (servicenow-triage README §2)
```

A row looks like this. The ticket's free text comes last, so Jeff can cache the rest of the prompt:

```json
{"id": "T0000000-assignment_group", "suite": "servicenow-triage", "family": "2026-W38",
 "state": {"contact type": "Phone", "impact": "3 - Low", "opened": "Tuesday 10:15",
           "summary": "unable to open outlook mailbox full", "details": "cannot send or receive email since this morning"},
 "question": {"type": "choice", "instructions": "Which support team should own and resolve this incident?",
              "criteria": {"IMG-SMC-TCC-ITSC": "IT service centre (first line): Outlook and email, passwords, ...",
                           "...": "...",
                           "ROUTE_TO_HUMAN": "None of the other options: a rarely seen or unclear case; leave it for a person to route"}},
 "label": "IMG-SMC-TCC-ITSC", "target": "IMG-SMC-TCC-ITSC", "source": {"field": "assignment_group", "ticket": "T0000000"}}
```

What the builder does:

| Step | How |
|---|---|
| Split | By time, in whole ISO weeks: train 70% (oldest), development 10%, calibration 10%, test 10% (newest). Each week is a Jeff *family*, so `leak-check` can confirm no week is in two files. |
| Options | Learned from the **train** split only. Classes with fewer tickets than `labels.min_class_count` become `ROUTE_TO_HUMAN`, as in the sklearn model. A class that only appears later is labelled `ROUTE_TO_HUMAN` in the later files. Options are listed alphabetically (at most 254 + `ROUTE_TO_HUMAN`). |
| Descriptions | Jeff decides from the words of each option, so a code like `IMG-SMC-TCC-ITSC` alone tells it little. Write what each team or CI handles in [`jeff_options.yaml`](jeff_options.yaml). Options you leave out get an automatic hint from their training tickets ("Tickets usually mention: outlook, mailbox, ..."). |
| `options.json` | The questions exactly as trained. Serving must ask the same questions with the same options. |

How much data: Jeff's own tests show 250 examples per question already gives about 76%, 1,000 about 81%, and the full 72,000 gives 91.5% (README, "Why a Jeff base?"). A handful of hand-written tickets is enough to check the format (`--min-count 1 --no-split`), not to train. Use the full export of resolved and closed tickets.

## 2. Check, train, install (NVIDIA GPU)

```bash
git clone https://github.com/firelex/jeff ../jeff   # next to this folder && (cd ../jeff && uv sync --extra lora --extra cuda)
scripts/train_jeff_adapter.sh ../jeff          # run from jeff-incident-triage/          # optional 2nd argument: LoRA learning rate
```

[`scripts/train_jeff_adapter.sh`](scripts/train_jeff_adapter.sh) runs these steps:

1. **`jeff-kit check-rows`** on every file.
2. **`jeff-kit leak-check`**. Service desks repeat themselves ("outlook not opening"), so some training tickets are copies of later ones. Those training tickets are dropped (never the test ones), then the check runs again.
3. **`jeff-kit shortcut-report`**, once per field, into `runs/<run>/shortcuts-<field>.md`. **Read these before you trust the score.** A flag is fine when it is the meaning of a label: P1 tickets mention outages. It is a problem when it is an accident of the data, such as one team's tickets all coming from one email template.
4. **Downloads the Jeff v1.3 base** (`mstrasser/jeff-base`, revision `v1.3`), then runs `jeff-train --lora-rank 16 --initial-checkpoint jeff-base-v1.3` for one epoch. Checkpoints are chosen on `development`, and the temperature that makes the probabilities honest is fitted on `calibration`.
5. **Copies the adapter** to `../jeff/adapters/servicenow`.

**Learning rate:** Jeff does not publish the rate used for its own adapters. Try `5e-5`, `1e-4` (the default) and `2e-4`, and keep the one with the lowest development loss in `runs/<run>/events.jsonl`. Never choose on test.

## 3. Serve and compare with the sklearn model

```bash
cd ../jeff && JEFF_CHECKPOINT=jeff-base-v1.3 JEFF_ADAPTERS=adapters JEFF_QUEUE_MS=500 PORT=8765 \
  uv run --extra lora jeff-serve

# accuracy on the newest 10% of tickets, plus coverage and accuracy at the config.yaml cut-offs
python -m jeff_triage.client --rows data/jeff/test.jsonl --model servicenow
# predictions for new tickets in the my_tickets format
python -m jeff_triage.client --tickets data/my_tickets.json --model servicenow
```

`jeff_triage.client.predict()` returns the same `pred_<field>` and `conf_<field>` keys as `/predict`. `conf_` is the probability of the chosen option, like sklearn's, so the 0.9 cut-offs mean the same for both models. (Jeff's own `confidence` field is rescaled for the number of options.) Set `JEFF_API_KEY` on the server and the client to require a key.

The sklearn report in `servicenow-triage/reports/<version>/report.md` scores the newest 20% of tickets; Jeff's test file is the newest 10%. To compare like with like, score the sklearn model on the same weeks: `python -m triage.monitor --actuals <those tickets> --rescore` (in `servicenow-triage/`). Jeff is worth switching to for a field only if it is better on the same tickets **and** covers more tickets at the 0.9 cut-off without lower accuracy there.

## Notes

- **Priority:** the impact the caller chose is part of the state. The P1-P5 descriptions in `jeff_options.yaml` must use the exact values of your export (`P3`, or `3 - Moderate`).
- **Category** is disabled in the sklearn model because it scored below its baseline. Jeff trains it anyway. Check its line in the comparison before you use it.
- **Licence and data:** the Jeff base and code are open. Training runs on your own GPU, so the tickets never leave it. `data/`, `runs/` and `adapters/` are git-ignored: never commit the row files, they contain ticket text.
