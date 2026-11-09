# Connecting an external file-delivery agent

**Recommended for the integrated Штурман application:** deliver the file to the same endpoint the dashboard uses. This runs the dry run only:

```bash
curl -sS -F "file=@plan.xlsx" -F month=2026-11 http://<shturman>/api/plan-imports
```

Then approve it, either by a person in **Инж. проекты → Импорт плана** or by an authorized caller:

```bash
curl -sS -X POST -H "Content-Type: application/json" -d '{"confirm": true, "approved_by": "planning-bot"}' http://<shturman>/api/plan-imports/<import-id>/approve
```

Manual and automatic deliveries therefore share one validation and synchronization pipeline. The alternatives below run the agent outside the app and write to the same Штурман API.

Every channel stores the file in the content-addressed inbox and runs **the same pipeline**. File names are never relied on: the format is detected from the file contents, and the original name is kept only as metadata.

## 1. HTTP intake of the agent itself (agent running outside the app)

Set the intake key on the receiving side:

```bash
export PLAN_AGENT_INTAKE_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

```bash
python -m plan_agent serve --host 0.0.0.0 --port 8090
```

The sending agent posts the raw file bytes. Its key is a placeholder you replace with your own secret:

```bash
curl -sS -X POST "http://plan-agent:8090/files" -H "Authorization: Bearer $PLAN_AGENT_INTAKE_KEY" -H "X-Filename: plan_week_45.xlsx" -H "X-Sender: planning-agent" --data-binary @plan_week_45.xlsx
```

The response (JSON):

```json
{"run_id": "...", "status": "Completed", "counts": {"identified": 48, "new": 5, "updated": 12,
 "unchanged": 28, "review": 3, "duplicate_candidates": 1, "invalid_records": 0, "sync_errors": 0},
 "report_text": "ENGINEERING PLAN SYNCHRONIZATION ...", "analysis_markdown": null}
```

Possible `status` values:

* `Completed`
* `Completed with errors`
* `Approval required (dry run)`: initial file, or the structure changed
* `Dry run`: `?mode=dry_run` was requested
* `Dry run - dashboard unavailable`
* `Failed`: unsupported, empty or corrupt file, or no planning data

Other endpoints:

* `GET /files`: received files with their delivery metadata
* `POST /files/<sha256-prefix>/reprocess`: run a stored file again
* `GET /health`

Processing is serialized: one run at a time, in arrival order. Put the intake behind TLS, for example a reverse proxy, when it is reachable from other hosts.

## 2. Folder drop

```bash
python -m plan_agent watch /srv/plans/incoming --interval 30
```

A file is processed once its size has stopped changing. Afterwards it is moved to `processed/` or `failed/`, prefixed with the run id.

## 3. Scheduled processing

Run the folder pass once from cron or a systemd timer:

```cron
*/15 * * * * cd /opt/Projekt_managment && . ./.env && python -m plan_agent watch /srv/plans/incoming --once >> state/cron.log 2>&1
```

## 4. Manual upload / CLI

```bash
python -m plan_agent sync /path/to/file.xlsx
```

## 5. Reprocessing

```bash
python -m plan_agent reprocess <sha256-prefix>
```

`python -m plan_agent status` shows the approval state and recent runs.

## Behaviour the sender can rely on

* Sending the same file again is safe: matched projects are updated or left unchanged, never duplicated.
* Until a person approves the mapping for a given file structure, nothing is written.
* A partial or bad file never deletes or blanks dashboard data.
