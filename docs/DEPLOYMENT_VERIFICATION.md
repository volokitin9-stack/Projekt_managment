# Deployment verification (before the first synchronization)

The first synchronization must not go to a temporary or disposable database. The agent therefore refuses a real sync until `verify-deployment` has reported **READY** for exactly the API URL in `PM_API_URL`.

Run the first check. It takes a snapshot of project IDs and creation times:

```bash
python -m plan_agent verify-deployment
```

Then restart the dashboard application (for example, restart its service or container) and run the check again:

```bash
python -m plan_agent verify-deployment --after-restart
```

Nothing is written to the dashboard. Agent authentication is tested by sending an **empty** create request. The Штурман API authenticates it and then rejects it with `400 Validation failed`; `401` means the key is wrong and `501` means the server has no key configured.

| Check | Passes when |
|---|---|
| configuration | `PM_API_URL` is set |
| dashboard page | the dashboard page is served (HTTP 200 with a title) |
| backend API | `GET /api/health` reports ok |
| agent authentication | the empty probe is authenticated (Штурман) or `/api/agent/schema` responds (reference API) |
| database contents | the project list is readable; seeded sample projects are reported |
| persistent database | every project seen before the restart is still there afterwards, unchanged |
| production host | the API is not on a local address |
| agent payload compatibility | the API can store categories, activities and original values without loss |

## API compatibility

The integrated Штурман app advertises its planning support in `GET /api/agent/schema`: activities, create-only writes, review queue, sync runs and field sources. The compatibility check passes only when all of these are present.

A Штурман build **without** this integration (the original `main` branch) fails the check. In such a build:
* there are no categories or activities;
* unknown fields are silently dropped;
* a missing priority or stage is displayed as "normal" or "scanning".
