# Workflow automation: stage, status, priority, next action

The planning agent analyses every project and sends a **decision** with its evidence. The Штурман server **validates** it and applies only what the rules allow. Business rules live in one file, read by both: `config/workflow_automation.json`.

```text
Excel (read-only snapshot) → plan_agent: activity status → stage decision → priority → next action
                                   │  PATCH/POST /api/agent/projects  { …, "workflow": {…} }
                                   ▼
                       server/workflowModel.js: validate → apply (one transaction) → history, review items
```

Code: `plan_agent/automation.py` (decisions), `plan_agent/sync.py` (when a decision is sent), `server/workflowModel.js` (validation, authority, stage time, effective priority, attention indicators).

## 1. Activity status (normalised)

| Status | Evidence |
|---|---|
| `in_progress` | hours logged this month («Факт месяц» or day columns). Hours are effort, never completion |
| `planned` | hours planned this month, none logged |
| `completed` | a person marked the activity «done» in Штурман, or an **approved** indicator (`activity_status.completion_indicators`, empty by default) |
| `blocked` | a person marked it «blocked», or an approved indicator (`blocker_indicators`, empty) |
| `unknown` | neither planned nor logged hours, no mark |

Every activity carries `normalized_status` and `status_evidence` (`source`: hours / plan / manual / indicator / none, plus a detail). ОК and NOK are kept as original values and are **not** interpreted until the department confirms their meaning (then add an indicator, e.g. `{"field": "items_ok", "op": ">=", "value": 1}`).

## 2. Activity → stage

The department's validated task dictionary in `config/profiles/monthly_work_plan.yaml → activity_rules` maps each task to a type:

| Task | Type |
|---|---|
| Отсканировать | scanning |
| Разработать, Доработать | development |
| Заказать; group «Лабаратория, разбор опытников» | test_prototype |
| Примерить; group «Замеры» | measurement |
| КД, 1С; group «Выгрузка в PDM» | documentation |
| Перевод, Расширение | transfer_extension |
| stage «Тесты» | testing |
| change requests | engineering_change |

The last three are additional activity types, not Kanban stages. An unknown task matches no rule and sends the record to review.

## 3. Current stage

Current work is taken from the **latest cycle in which hours are logged**, so rework cycles do not mix. Rules based on planned work look at **all** open main-stage work of the month, across cycles. The rules are tried in order, and the first that applies decides:

| Rule | When | Mode | `stage_source` / `stage_confidence` |
|---|---|---|---|
| `active_single_stage` | hours logged on activities of exactly **one** stage | **assign** | `agent_rule` / `verified` |
| `active_multiple_stages` | hours logged on several stages; the most advanced one is proposed | propose | — |
| `completed_previous_stage` | all activities verified completed; the next stage is proposed | propose | — |
| `planned_single_stage` | nothing logged; **all** main-stage work of the month's plan belongs to one stage | **assign** (approved 2026-11-17) | `inferred_from_plan` / `inferred` |
| `planned_sequence_earliest` | nothing logged; several planned stages; the earliest is proposed | propose | — |

**Stage sources:**

| Source | Meaning | Changed by the automation? |
|---|---|---|
| `manual` | confirmed by a person | never |
| `agent_explicit` | an explicit, confirmed transition | never |
| `agent_rule` | assigned from logged work | yes, by later evidence |
| `inferred_from_plan` | assigned from the month plan only | yes, by later evidence |

`stage_confidence`: `confirmed`, `verified`, `inferred` or `null`. When hours are later logged on an inferred stage, its basis becomes `verified`. The history records `stage_basis_changed`; the stage and its timers are not touched.

* **assign**: the server sets the Kanban stage (`stage_source = agent_rule`). It re-checks that the rule is enabled with mode `assign`, so an agent cannot use an unapproved rule.
* **propose**: the stage is shown as a suggestion (`planned_stage`, «по плану: …» on the card). The project stays in «Этап не подтверждён», and a review item is created.

**Authority:**
- A stage confirmed by a person (drag, «Подтвердить», details panel) or by an explicit agent transition is **never changed** by the automation.
- A contradicting decision only creates the warning `workflow_manual_stage_conflict`.
- Stages that existed before this version count as confirmed by a person.

**Rework:**
- A stage assigned by the automation may move back (e.g. measurement → development) when new evidence says so (`allow_backward_transitions`).
- Every move is recorded in the history, in order (`stage_auto_assigned`, `stage_auto_changed`, `direction`).
- A move back also creates the warning `workflow_backward_transition`.
- Weaker evidence later (only planned work) never removes an assigned stage.

**Switches:** `auto_assign_stage: false` turns all automatic assignment off (proposals only). A rule is switched on by setting its `mode` to `assign`. No code change and no restart are needed; the server re-reads the file when it changes.

### November 2026 result (read-only validation)

**48 of 72** main projects are placed automatically:
- **11 `agent_rule`:** 10 «Оформление», 1 «Разработка».
- **37 `inferred_from_plan`:** 16 «Замер», 14 «Оформление», 7 «Сканирование».

Not placed:
- **22** projects whose month plan spans several stages (proposal and review);
- **1** with work logged on two stages, Lada Niva «Накладки на пороги» (proposal and review);
- **1** with no main-stage work, Lada HGH «Оснастка»: only tests are planned.

## 4. Stage time

| Field | Meaning |
|---|---|
| `stage_entered_at` | **confirmed** entry (a person's or an explicit transition). `null` for automatically assigned stages: the plan does not say when the project really entered the stage |
| `stage_detected_at` | when the system recognised the automatically assigned stage. Shown separately, never as the entry date |
| `stage_entry_confidence` | `confirmed` or `unknown` |
| `days_in_stage` | days since a confirmed entry; `null` if unknown («Длительность неизвестна») |
| `days_since_stage_detected` | days since the automatic assignment |

Ordinary updates never reset the timer, and repeated decisions for the same stage keep `stage_detected_at`. Only a real transition does.

**Stage limits:** `attention.stage_duration_limits`, e.g. `{"measurement": {"warning_days": 20, "critical_days": 40}}`. The default is empty, so nothing is flagged until limits are approved. Limits apply only to confirmed entry dates.

## 4a. Recalculation without a new Excel version

Durations (`days_in_stage`, `days_since_stage_detected`) and the attention indicators are computed **on every read** from the current time, so they are always current.

Decisions that depend on time or on marks made in Штурман are re-decided by a **recalculation**:
- system priority from approved rules, e.g. a deadline approaching;
- next action after a person marks an activity done or blocked;
- normalised activity statuses.

What a recalculation does:
- it uses the data already stored in Штурман (the activities of each project's latest plan month, manual marks, confirmed entry dates) with today's date (`PLAN_TIMEZONE`);
- it sends only what changed.

What it never does:
- read or re-import the Excel file;
- change a confirmed stage;
- reset a timer.

When it runs:
- **daily** at `PLAN_SCHEDULE_TIME` (09:00 Yekaterinburg), on **every** day including weekends and holidays (only the recalculation then, no file check), at most once per date;
- after a late server start (cheap, no file);
- on demand: `POST /api/plan-source/recalculate`, or `python -m plan_agent recalc`.

Every run is recorded in the run history (`trigger: "recalc"`, `phases.recalc`).

## 5. Priority

| Field | Source |
|---|---|
| `source_priority` (+ `source_priority_raw`) | the Excel value: 1, 1.1, 1.2, 1.3 → high; 2 → normal (Средний); 3 → low; 0 or empty → unspecified. The original is kept |
| `system_priority` | approved escalation rules in `priority.rules` (none by default). Supported types: `deadline_within_days`, `deadline_overdue`, `stage_days_exceeded` (confirmed entry date only) and `verified_blocker` |
| `effective_priority`, `priority_source` | **manual** (a person set it in Штурман) > **system** > **source** |
| `priority_reasons` | the explanation; only conditions supported by data |

A missing engineer or an unresolved stage never raises the priority; they are attention flags. The card keeps showing `priority`, which equals the effective priority, and a manual priority is never overwritten.

## 6. Next action

`next_action` = `{label, source, confidence, evidence, updated_at}`:
1. An activity of the current stage is in progress → «Продолжить: <task>» (`verified`, from logged hours).
2. All activities of the stage are verified completed → the next workflow step (`next_action.after_stage_completed`, e.g. «Заказать образец»).
3. Work is planned this month → «Начать: <task>» (`planned`).
4. Otherwise → «Следующий шаг требует уточнения» (`none`) and the warning `workflow_unknown_next_action`.

Generic task names («Прочее») are replaced by the task group («Выгрузка в PDM»). A next action written by a person (`PATCH /api/projects/:id {"next_action": "…"}`) is kept.

## 7. Attention indicators (`attention`)

- `missing_engineer`;
- `confirmed_blocker` (an activity a person marked «blocked»);
- `deadline_exceeded` (`planned_deadline` set and passed);
- `last_recorded_activity` (the last day with logged hours);
- `no_recent_activity` (only with `attention.no_recent_activity_days`; otherwise `{"configured": false}`);
- `stage_duration`.

An unchanged Excel file is not taken as proof that a project stopped.

## 8. Review queue

Items created by the automation are kept one per project and code. They are reopened if the problem returns and resolved automatically when it is gone, or when a person confirms the stage.

| Kind | Severity |
|---|---|
| `workflow_stage_undetermined`, `workflow_ambiguous_stage` | review |
| `workflow_manual_stage_conflict`, `workflow_backward_transition`, `workflow_unknown_next_action` | warning |
| `plan_record` (identity, unknown task, …: not synchronised) | review (blocking for that record) |

Stage items exist only for **main projects**. Additional work and backlog have no Kanban stage, so they get a next action but no stage review. Items created for them by an earlier build are resolved at server start (`resolution: not_a_kanban_record`); nothing is deleted.

`review_flags` in the project API also shows `manual_stage_differs` (severity info, computed and not queued). It appears when a person's confirmed stage differs from the agent's current decision. The stage is never changed because of it.

`GET /api/review-queue` returns the actionable items (blocking + review); `?severity=all` returns warnings too. Other projects are processed even when some need review.

## 9. When a decision is sent

The decision is part of the same API call as the project and activity data, so it is applied atomically. It is also sent when only the decision changed: for example, a person marked an activity done and the next run re-decides from that evidence. Re-processing an **older** month never sends a decision; it belongs to the latest plan month. The scheduled 09:00 run and «Обновить» use the same pipeline. An unchanged file (same SHA-256) is not analysed again, so time-based escalation rules are evaluated when the plan changes.
