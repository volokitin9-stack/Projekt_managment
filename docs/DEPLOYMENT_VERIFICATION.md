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

## Gaps in the Штурман agent API (branch `main`)

These were measured on a local test copy using synthetic probe records. They block a lossless sync today:

* There are no record categories, so additional work and backlog items would appear as Kanban projects.
* There are no activities, so per-task assignee, hours, hours per date, progress and source rows would be lost.
* Unknown fields (`extra_attributes`, `record_category`, `last_plan_month`, `source_row`) are silently dropped.
* Priorities are `critical/high/normal/low`, and a missing priority is **displayed as "normal"**.
* The stage is stored in `status` with the key `prototype`, and a missing stage is **displayed as "scanning"**.
* Creating a project requires manufacturer, model and product, so additional work without a vehicle is rejected.
* A fresh database seeds 14 sample projects with placeholder engineers.

The reference backend in `backend/` implements the full contract. Porting its additions into Штурман (additive, with the UI unchanged except for separate lists for additional work and backlog) removes these gaps.
