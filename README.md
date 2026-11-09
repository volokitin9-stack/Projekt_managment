# Projekt_managment: Engineering Plan Agent

An agent that reads the engineering department's planning files (Excel, CSV, PDF or Word), works out their structure on its own, and keeps the Projekt_managment dashboard up to date through the backend API, never through Git commits. It preserves manual dashboard data and records history for every change.

> **Repository state when this was built:** the GitHub repository was **empty**: no dashboard, no backend, no database schema. Since there was no existing API to verify against, this repository now contains:
>
> * `plan_agent/`: the file-processing and synchronization agent (the main deliverable);
> * `backend/`: a small **reference backend** (REST API + SQLite) implementing the agent endpoints from the specification (`GET /api/projects`, `POST /api/agent/projects`, `PATCH /api/agent/projects/:id`) plus history and sync-run storage. It is the data layer the dashboard UI should read from. It is *not* a second dashboard.
>
> The agent talks to the backend only through HTTP (`plan_agent/client.py`). If the dashboard team ships a different backend, it only needs the contract in [docs/API.md](docs/API.md).

## How it works

```text
manual upload / folder drop / HTTP POST from another agent / schedule / reprocess
                                   │
                         inbox (content-addressed; file name is only metadata)
                                   │
 parse (xlsx/xls/csv/pdf/docx) → detect structure → extract projects → map workflow → validate
                                   │
              approved mapping & same structure fingerprint?  ── no ──► dry run + proposal + analysis report
                                   │ yes
            GET dashboard projects → match (id → article → make+model+product) → field ownership policy
                                   │
            POST/PATCH per project (one transaction each, history rows)  → report + sync history
```

| Module | Responsibility |
|---|---|
| `parsers/` | Format detection by content; merged cells, outline groups, hidden rows, cell notes; PDF/Word tables |
| `schema_detection.py` | Header row(s) anywhere in the sheet, multi-row headers, column roles + confidence + evidence, section rows, wide vs. activity layout |
| `workflow.py` | Stage/status/priority mapping and derivation of current stage and status, **without fabricating facts** |
| `extraction.py` | Rows → project records; hierarchy from section rows; merging across sheets |
| `validation.py` | Record-level checks; problems become issues, never exceptions |
| `matching.py` | Duplicate prevention: exact identifiers first, fuzzy matching only to suggest candidates |
| `ownership.py` | Configurable field ownership; empty values never overwrite; manual edits are reported |
| `sync.py`, `client.py` | Safe execution: retries, auth handling, optimistic concurrency, verification of retried writes |
| `mapping_config.py` | Proposed/approved mapping, structure fingerprint |
| `report.py` | Sync report and initial-analysis report |
| `service.py`, `intake.py`, `cli.py` | One pipeline for all delivery channels |

## Quick start

Install the dependencies:

```bash
pip install -r requirements.txt
```

Start the backend. The agent key is a shared secret: generate it once and use the same value for the backend and the agent.

```bash
export PM_AGENT_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

```bash
python -m backend.server --db data/projekt.db --port 8080 &
```

Point the agent at the backend:

```bash
export PM_API_URL=http://127.0.0.1:8080
```

Run the initial analysis. It is always a dry run and writes nothing to the dashboard.

```bash
python -m plan_agent analyze path/to/engineering_plan.xlsx
```

Review `state/reports/<run>_analysis.md` and `state/mapping.proposed.yaml`, edit the mapping if needed, then approve it:

```bash
python -m plan_agent approve --by "Your Name"
```

Synchronize. This and every later file with the same structure syncs automatically:

```bash
python -m plan_agent sync path/to/engineering_plan.xlsx
```

Exit codes for `analyze` and `sync`: `0` completed, `3` approval required (dry run), `4` completed with errors, `1` failed.

## Processing the real engineering plan

The department's real file has not been supplied yet, so **no final column mapping exists**. The vocabulary in `config/vocabulary.yaml` contains hints only. When the file arrives:

1. `python -m plan_agent analyze <file>` produces the initial-analysis report. It covers detected sheets, header rows, the meaning of every column with its evidence, how projects are identified, how statuses and stages were interpreted, which fields are unreliable, and a table of proposed changes.
2. Correct anything wrong in `state/mapping.proposed.yaml`, such as a column role or a status value mapping, and run `approve`.
3. Run `sync`.

## Monthly work-plan workbooks

The department's plan, a workbook with one worksheet per month, is read by the explicit profile `config/profiles/monthly_work_plan.yaml` (see [docs/CONFIGURATION.md](docs/CONFIGURATION.md#workbook-profiles-configprofiles)). Only the target month's worksheet is imported:

```bash
python -m plan_agent analyze plan.xlsx --month 2026-11
```

* A project is Марка + Модель + Продукт/Задача, made of the month's task rows.
* Rows hidden by an AutoFilter are still read.
* The sheet becomes three dashboard categories: main engineering projects, additional work (ДОП РАБОТЫ) and task backlog (БАНК ЗАДАЧ). Totals, the resource table and the dropdown lists are accounted for but not imported.
* Every task row is an activity with its own assignee, hours, progress and source reference. Activities are stored per month, so December adds to November's history instead of replacing it.
* Summary or pivot sheets of the month are reported but never imported.
* Two plan sheets for the same month stop the run for verification.

## Safety guarantees

* The first file, and any file whose detected structure changed, is processed as a **dry run** that needs approval.
* Stage or status values that were never approved send only the affected projects to review.
* A missing fact stays `null`. A passed planned date never marks anything complete.
* An empty file value never clears a dashboard value. Projects missing from a newer file are never deleted. Photos, attachments, comments and history are never sent.
* A field edited manually in the dashboard is not overwritten by default; the conflict is reported. This is configurable.
* A changed identity of a matched project (for example, the model of ENG‑003 suddenly differs because a merged cell shifted) goes to review.
* One bad row never stops the file. Each project is written atomically, and retried writes are verified so they cannot create duplicates.
* Report counts come from actual API results.

## Documentation

* [docs/CONFIGURATION.md](docs/CONFIGURATION.md): environment variables, vocabulary, ownership policy, mapping approval
* [docs/API.md](docs/API.md): backend endpoints and data model
* [docs/EXTERNAL_AGENT.md](docs/EXTERNAL_AGENT.md): connecting an external file-delivery agent, folder drops, schedules

## Tests

```bash
pip install -r requirements-dev.txt
```

```bash
python -m pytest -q
```

The tests use **synthetic** representative plans generated by `tests/fixtures/make_fixtures.py`. These include title rows, a two-row merged header, bold manufacturer section rows, vertically merged cells, Russian vocabulary, an unknown stage, an invalid date, a contradictory status, a totals row, an irrelevant lookup sheet, and an activity-per-row CSV. They also run the agent end to end against the real backend over HTTP. These files are not the department's plan.
