# ServiceNow triage - production pipeline

Production version of the triage model (`triage_holdout_model.ipynb`). For each new incident it predicts the CI, the assignment group, the priority and the subcategory, plus an urgency flag. Only confident predictions are acted on; everything else follows today's manual process.

```
ServiceNow export ─► clean ─► label hygiene ─► features ─► time split ─► train ─► honest report
                                                                              │
         ┌────────────────────────────────────────────────────────────────────┘
         ▼
  retrain on 100% ─► models/<version>/ ─► promote? ─► API (/predict) ◄── ServiceNow business rule
                                                          │
                                         logs/predictions.jsonl ─► weekly monitor ─► retrain
```

## Files

| File | Stage | What it does |
|---|---|---|
| `config.yaml` | all | Columns, cleaning rules, class cut-offs, confidence cut-offs, alert thresholds. Change behaviour here. |
| `src/triage/clean.py` | 1 | Strips HTML, reply chains and signatures. Masks emails, phones, IPs, PNRs, ticket and staff numbers. Normalises app names. Drops duplicates, cancelled tickets and test tickets. The same `clean_text()` runs in training and in the API. |
| `src/triage/labels.py` | 2 | Applies agreed team merges. Folds classes with too few tickets into `ROUTE_TO_HUMAN`. Marks P1/P2. |
| `src/triage/features.py` | 3 | One sklearn `Pipeline` per field: TF-IDF on the text, plus creation-time fields (contact type, caller, impact, hour, weekday). |
| `src/triage/model.py` | 5 | `TriageModel`: the field models, the urgency gate and the decision rules (cut-offs, never-auto list, fallback group, CI mismatch). |
| `src/triage/train.py` | 4-7 | Time split, then trains on the earliest 80% and scores on the latest 20% (the honest report). Then retrains on 100%, saves, and compares with production. |
| `src/triage/evaluate.py` | 6 | Accuracy against baseline, confidence tables, per-class recall, top mistakes, auto-routing and urgent metrics. |
| `src/triage/registry.py` | 7 | Versioned models with a sha256 check, plus promote and rollback. |
| `src/triage/serve.py` | 8 | FastAPI service: `/predict`, `/health`, `/admin/reload`. Needs an API key and never logs ticket text. |
| `src/triage/monitor.py` | 10 | Weekly check against resolved tickets. Exits with code 1 when accuracy drops, the model drifts or unknown teams or CIs appear. |
| `servicenow/business_rule_ml_triage.js` | 8-9 | Async rule on incident insert. Calls the API and handles shadow, suggest and auto modes. |
| `servicenow/ui_action_accept_suggestion.js` | 9 | One-click "accept suggestion" button for suggest mode. |
| `notebooks/runbook.ipynb` | - | The same steps, run interactively. It only calls `src/triage`, so it can't drift from production. |
| `Dockerfile` | 8 | Image for the API. |
| `tests/` | - | End-to-end test on synthetic tickets, plus `synthetic.py` to generate fake data. |

## 1. Set up

```bash
uv venv && uv pip install -e ".[dev]"
python -m pytest            # end-to-end test on synthetic data (about 10 s)
```

## 2. Export the data from ServiceNow

Export **resolved/closed** incidents to `data/raw_incident_reassign.csv` (list view, CSV, display values) with these columns:

`number, opened_at, short_description, description, contact_type, caller_id, sys_created_by, impact, cmdb_ci, assignment_group, category, subcategory, priority, state`

Why resolved or closed tickets: on a closed ticket, `assignment_group` holds the team that **finally** owned it. That is the right label. The first assignment is often a mistake, and a model trained on it learns the mistakes.

`data/`, `models/`, `logs/` and `reports/` are git-ignored because they hold incident text. Never commit them.

## 3. Train

```bash
python -m triage.clean --config config.yaml          # optional: inspect the cleaning (data/clean.csv)
python -m triage.train --config config.yaml --promote yes   # first time
```

Output:
- `models/<version>/model.joblib`: the model trained on **all** tickets. It is the one that gets deployed.
- `models/<version>/manifest.json`: the sha256, the data period and the **honest** scores.
- `reports/<version>/report.md`: the honest report (trained on 80%, scored on the unseen latest 20%). Quote these numbers, never in-sample ones.

Training fits the field models twice (80% and 100%), plus a 3-fold check for the urgency gate. On 38k tickets, expect about two to three times the notebook's 3 minutes.

## 4. Serve

```bash
export TRIAGE_API_KEY=$(openssl rand -hex 32)
uvicorn triage.serve:app --port 8080          # or: docker build -t servicenow-triage . && docker run ...

curl -s localhost:8080/predict -H "X-API-Key: $TRIAGE_API_KEY" -H 'Content-Type: application/json' -d '{
  "tickets": [{"number":"INC0012345","opened_at":"2026-10-12 09:00:00",
               "short_description":"ResConnect timeout","description":"booking screen frozen",
               "contact_type":"Phone","impact":"3 - Low","cmdb_ci":"S.A.MS-OUTLOOK"}]}'
```

Each result contains:
- `pred_<field>` and `conf_<field>` for every field.
- `auto_cmdb_ci` and `auto_assignment_group`: true only at or above the cut-off, and never for `ROUTE_TO_HUMAN` or the small SITA CIs.
- `routed_group`: the predicted team, or the fallback (ITSC) when the model isn't sure.
- `urgent_alert`, `urgency_score` and `ci_mismatch`.

## 5. Connect ServiceNow and go live in stages

The setup steps are in the header of `servicenow/business_rule_ml_triage.js`: the REST message, the system properties, the `u_pred_*` fields and the event. The property `x_triage.mode` controls the rollout:

| Mode | What happens | Move on when |
|---|---|---|
| `shadow` (2 to 4 weeks) | Predictions are only written to `u_pred_*`. Agents see nothing new. | `monitor.py` on these weeks matches the holdout report. |
| `suggest` | Agents see the suggestion and can click **Accept ML suggestion**. | The acceptance rate is high for confident tickets. |
| `auto` | Group routed when `auto_assignment_group` is true; CI filled when empty and `auto_cmdb_ci` is true; urgent alert raised. | - |
| `off` | Kill switch. | - |

The rule is async, so a slow or failed API never blocks ticket creation. It never overwrites a group a person has already changed, and it adds a work note whenever it routes a ticket.

## 6. Monitor and retrain (schedule these)

```cron
# Monday 06:10: score last week's resolved tickets against what the API predicted
10 6 * * 1  cd /opt/triage && python -m triage.monitor --actuals data/closed_last_week.csv || notify-team
# 1st of the month 02:20: retrain on the latest export; promote only if not worse on unseen tickets
20 2 1 * *  cd /opt/triage && python -m triage.train --promote auto && curl -X POST -H "X-API-Key: $KEY" localhost:8080/admin/reload
```

**Promotion rule.** The new model's 80% version and the production model are scored on the same tickets, which are newer than anything production was trained on. The new model replaces production only if it is no more than 1 point worse on `cmdb_ci` and `assignment_group` (`promotion:` in the config).

**Manual control:**
```bash
python -m triage.registry list          # versions, data period, honest scores (* = production)
python -m triage.registry promote v20261101-022000
python -m triage.registry rollback      # back to the previous production version
```

**Testing on tickets the model has never seen** (like section 6 of the notebook):
```bash
python -m triage.monitor --actuals data/tickets_after_12_aug.csv --rescore
```

## Decisions encoded in `config.yaml` (from `improvements.md`)

| Decision | Setting |
|---|---|
| `category` is not trained (below baseline) | `targets.category.enabled: false` |
| Teams and CIs with fewer than 25 training tickets go to `ROUTE_TO_HUMAN` | `labels.min_class_count` |
| Only CI and assignment group are ever written automatically | `auto_fields` |
| Act only at 0.9 confidence or higher | `cutoffs` |
| Small SITA CIs are never auto-filled | `never_auto.cmdb_ci` |
| Uncertain tickets go to ITSC | `routing.fallback_group` |
| Merging confusing team pairs (L1-Engineers / ITSC / BSO-ENG&ERP) | `labels.merge_map`, **empty until the teams agree** |

## Security

- `.joblib` files can run code when loaded. `registry.load` refuses a file whose sha256 doesn't match its manifest, or one saved with a different scikit-learn version. Keep `models/` writable by the training job only, and mount it read-only in the API.
- The API needs `X-API-Key`. Store the key in ServiceNow as a `password2` property.
- The prediction log holds ticket numbers, predictions and confidences, never ticket text.
- Text is masked before training, so emails, phone numbers, IPs, PNRs and long ID numbers are not stored in the model's vocabulary. People's names written in free text are **not** masked (signatures are cut, but names in the body remain). Treat the model file as internal data.
