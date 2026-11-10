# data/: local operational data

At runtime this folder holds Штурман's operational data. **Everything here except this README, `.gitkeep` and `templates/` is ignored by Git.** Real projects, uploaded planning files and reports must never be committed.

| Path | Content |
|---|---|
| `shturman.json` | the data file: tasks, projects, activities, history, file metadata, review queue, import runs |
| `shturman.json.bak` | the previous version, kept on every write |
| `shturman.json.pre-migration-*.bak` | copy taken before a data-format upgrade |
| `sync-runs/` | full reports of planning-agent runs |
| `plan-imports/` | uploaded planning files and the agent's state (approved mapping, reports, review queue). Not served over HTTP |
| `.shturman.lock` | held by the running server: only one process may use this folder |
| `templates/shturman.empty.json` | committed empty template used to create `shturman.json` on first start |

**Backup:** stop the server and copy the whole folder. A copy of `shturman.json` taken while the server runs is also consistent, because each write replaces the file atomically. Keep `uploads/` together with it.

**Restore:** stop the server, put the copied files back and start it again.
