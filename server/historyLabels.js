"use strict";

// Turns server/projectsModel.js's raw project_history rows into a readable journal: Russian field
// names, grouped-by-operation events with an expandable change list, and - the actual bug this
// fixes - never a raw JSON dump of a technical/diagnostic field (plan-import metadata like
// delivery_chance/probability_note/sales_channel/mapping_rule/unmapped_columns/
// actual_hours_source, all of which live inside the activity's own `details` object, or the
// project's `extra_attributes`). Nothing here reads or writes the database - it's a pure
// presentation layer over data that is never changed or deleted.
//
// Safety rule, not a field-by-field guess list: ANY change whose old or new value is a non-null
// object or array is automatically treated as technical (hidden from the normal journal, shown
// only when `admin` is requested) - so a diagnostic field this file doesn't explicitly know about
// still can never leak raw JSON into the normal view. A handful of fields are additionally forced
// technical even though their value might be a short string, because they are purely internal
// bookkeeping no one reads the journal to see (see TECHNICAL_FIELDS below).

const FIELD_LABELS = {
  // project-level ("updated:<field>")
  external_project_id: "Внешний идентификатор", record_category: "Категория записи",
  project_name: "Название проекта", manufacturer: "Марка", vehicle_model: "Модель",
  product_type: "Изделие", article_number: "Артикул", assigned_engineer: "Ответственный инженер",
  priority: "Приоритет", priority_original: "Приоритет (исходный)", planned_stage: "Этап по плану",
  work_status: "Статус работ", description: "Описание", planned_deadline: "Плановый срок",
  planned_start_date: "Плановая дата начала", actual_completion_date: "Дата завершения",
  comments: "Комментарии", source: "Источник данных", source_file: "Файл-источник",
  source_sheet: "Лист файла", source_row: "Строка файла", source_rows: "Строки файла",
  last_plan_month: "Последний месяц плана", extra_attributes: "Служебные атрибуты",
  dependencies: "Зависимости", stages: "Настройка этапов", next_action: "Следующее действие",
  // activity-level ("activity_updated:<key>.<field>")
  activity_key: "Код работы", plan_month: "Месяц плана", sequence: "Порядок", cycle: "Цикл",
  name: "Название работы", activity_type: "Тип работы", stage: "Этап работы", task_type: "Тип задачи",
  project_stage_original: "Этап (исходный)", task_group: "Группа задач", assignee: "Исполнитель",
  assignee_original: "Исполнитель (исходный)", planned_hours: "Плановые часы",
  actual_hours: "Фактические часы", hours_by_date: "Часы по дням", progress_status: "Статус выполнения",
  effort_ratio: "Отношение факт/план", items_ok: "Принято, шт.", items_nok: "Отклонено, шт.",
  result_raw: "Результат (из плана)", comment: "Комментарий", details: "Служебные детали импорта",
  normalized_status: "Статус (нормализованный)", status_evidence: "Основания статуса",
  manual_note: "Заметка", removed_from_source: "Исключена из текущего плана импорта"
};

const ACTION_LABELS = {
  created: "Проект создан",
  activity_added: "Добавлена работа плана",
  activity_removed_from_source: "Работа отсутствует в новом плане",
  file_uploaded: "Добавлен файл",
  image_uploaded: "Добавлена фотография",
  stage_auto_changed: "Этап изменён автоматически",
  stage_auto_assigned: "Этап определён автоматически"
};

// Forced technical regardless of value shape - internal bookkeeping, never business data a person
// reads the journal to see.
const TECHNICAL_FIELDS = new Set(["details", "extra_attributes", "status_evidence", "hours_by_date"]);
// Whole actions that are themselves only ever diagnostic/internal-automation trail, not a
// business-level "this changed" fact - hidden the same way (shown under admin raw mode only).
const TECHNICAL_ACTIONS = new Set(["workflow_decision", "stage_basis_changed", "agent_sync"]);

function isComplex(v) {
  return v !== null && typeof v === "object";
}

function stageLabel(stageLabels, key) {
  if (!key) return "не подтверждён";
  return (stageLabels && stageLabels[key]) || key;
}

/** A short, always-safe string for a scalar value - never JSON.stringify, never for an
 *  object/array (those are caught as "technical" before this is called). */
function formatScalar(v) {
  if (v === null || v === undefined || v === "") return "—";
  if (v === true) return "да";
  if (v === false) return "нет";
  return String(v);
}

/** One readable change line from a single project_history row, or null for a row that carries no
 *  useful "old -> new" fact on its own (its data feeds the event's header instead - stage_entered_at,
 *  agent_sync, created). `activityNames`: Map<activity_key, name> for a nicer label on
 *  activity_updated rows, built by the caller from the project's own `activities` list. */
function changeFromRow(row, stageLabels, activityNames) {
  const a = row.action || "";
  if (a === "created" || a === "stage_entered_at" || a === "agent_sync") return null;

  if (a === "stage_transition") {
    return { field: "stage_transition", label: "Этап", old: stageLabel(stageLabels, row.previous_value),
      new: stageLabel(stageLabels, row.new_value), technical: false };
  }
  if (a === "stage_auto_changed" || a === "stage_auto_assigned") {
    return { field: a, label: ACTION_LABELS[a], old: stageLabel(stageLabels, row.previous_value),
      new: stageLabel(stageLabels, row.new_value), technical: false };
  }
  if (a === "activity_added") {
    const nv = row.new_value || {};
    return { field: a, label: ACTION_LABELS[a], old: "—",
      new: [nv.name, nv.plan_month].filter(Boolean).join(" · ") || "—", technical: false };
  }
  if (a === "activity_removed_from_source") {
    const key = (row.new_value || {}).activity_key;
    return { field: a, label: ACTION_LABELS[a], old: "—", new: activityNames.get(key) || key || "—", technical: false };
  }
  if (a === "file_uploaded" || a === "image_uploaded") {
    const nv = row.new_value || {};
    return { field: a, label: ACTION_LABELS[a], old: "—", new: nv.caption || "без описания", technical: false };
  }
  if (a === "comment:stage_transition") {
    return { field: a, label: "Комментарий к переходу", old: "—", new: formatScalar(row.new_value), technical: false };
  }

  const m = a.match(/^activity_updated:(.+)\.([^.]+)$/);
  if (m) {
    const [, activityKey, field] = m;
    const technical = TECHNICAL_FIELDS.has(field) || isComplex(row.previous_value) || isComplex(row.new_value);
    const label = (FIELD_LABELS[field] || field) + (activityNames.get(activityKey) ? ` (${activityNames.get(activityKey)})` : "");
    return { field, activityKey, label, technical,
      old: technical ? null : formatScalar(row.previous_value), new: technical ? null : formatScalar(row.new_value) };
  }
  const u = a.match(/^updated:(.+)$/);
  if (u) {
    const field = u[1];
    const technical = TECHNICAL_FIELDS.has(field) || isComplex(row.previous_value) || isComplex(row.new_value);
    return { field, label: FIELD_LABELS[field] || field, technical,
      old: technical ? null : formatScalar(row.previous_value), new: technical ? null : formatScalar(row.new_value) };
  }
  // Any action this dictionary doesn't explicitly know - never guess at its value's shape; show it
  // only when it's provably a safe scalar, otherwise treat it as technical rather than risk a dump.
  const technical = TECHNICAL_ACTIONS.has(a) || isComplex(row.previous_value) || isComplex(row.new_value);
  return { field: a, label: ACTION_LABELS[a] || a, technical,
    old: technical ? null : formatScalar(row.previous_value), new: technical ? null : formatScalar(row.new_value) };
}

function eventTitle(rows, changes) {
  const created = rows.find((r) => r.action === "created");
  if (created) {
    const nv = created.new_value || {};
    return "Проект создан" + (nv.source_file ? ` · файл «${nv.source_file}»` : "");
  }
  const sync = rows.find((r) => r.action === "agent_sync");
  if (sync) {
    const nv = sync.new_value || {};
    const visible = changes.filter((c) => !c.technical).length;
    const actSummary = nv.activities;
    const bits = [`${visible} ${visible === 1 ? "поле" : "полей"} изменено`];
    if (actSummary && (actSummary.added || actSummary.updated || actSummary.removed_from_source)) {
      bits.push(`работы: +${actSummary.added || 0} / ~${actSummary.updated || 0} / -${actSummary.removed_from_source || 0}`);
    }
    return "Синхронизация плана" + (nv.source_file ? ` · файл «${nv.source_file}»` : "") + " · " + bits.join(" · ");
  }
  if (rows.length === 1) return changes[0] ? changes[0].label : (ACTION_LABELS[rows[0].action] || rows[0].action);
  return `Изменение · ${changes.filter((c) => !c.technical).length} из ${changes.length} полей показаны`;
}

/** rows: the project's raw `history` array (oldest-first, as server/projectsModel.js's rowToApi
 *  returns it) - never mutated. `admin`: include technical fields' real old/new values (still
 *  never the whole raw row, just the field's own values) and the full raw rows of every grouped
 *  event, for the diagnostic toggle. `stageLabels`: {key: label} for the board's configured
 *  stages. `activities`: the project's own `activities` list, used only to resolve an
 *  activity_key to its name for a nicer label.
 *
 *  Returns events newest-first: [{ id, at, source, actor, title, changes: [...], hidden_count,
 *  raw? }]. A row with no batch_id (written before this feature existed, or a standalone action
 *  like a comment/file upload) is its own one-row event, unchanged from how the journal always
 *  showed it. */
function presentHistoryEvents(rows, { admin = false, stageLabels = {}, activities = [] } = {}) {
  const activityNames = new Map(activities.map((a) => [a.activity_key, a.name]).filter(([k]) => k));
  const groups = [];
  const byBatch = new Map();
  (rows || []).forEach((row) => {
    if (row.batch_id) {
      if (!byBatch.has(row.batch_id)) {
        const g = { batchId: row.batch_id, rows: [] };
        byBatch.set(row.batch_id, g);
        groups.push(g);
      }
      byBatch.get(row.batch_id).rows.push(row);
    } else {
      groups.push({ batchId: null, rows: [row] });
    }
  });

  const events = groups.map((g) => {
    const changes = g.rows.map((r) => changeFromRow(r, stageLabels, activityNames)).filter(Boolean);
    const visible = changes.filter((c) => !c.technical);
    const hidden = changes.filter((c) => c.technical);
    const first = g.rows[0];
    return {
      id: g.batchId || first.id,
      at: g.rows[g.rows.length - 1].at,
      source: first.source,
      actor: first.actor,
      title: eventTitle(g.rows, changes),
      changes: admin ? changes : visible,
      hidden_count: hidden.length,
      raw: admin ? g.rows : undefined
    };
  });
  return events.sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : 0));
}

module.exports = { presentHistoryEvents, FIELD_LABELS, ACTION_LABELS, TECHNICAL_FIELDS, formatScalar };
