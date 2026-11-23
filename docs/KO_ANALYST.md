# Агент `shturman-ko-analyst` — анализ чата КО средствами Claude Code

Статус: **этап 2 — интеграция с «Штурманом» реализована и покрыта тестами, на Railway не развёрнута.**
Автоматические запуски не настроены.

## Принцип

```text
Telegram (чат КО) ──webhook──► Railway «Штурман»  ── хранит сообщения и фото (только чтение чата)
                                     │  GET /api/agent/ko/*   (отдельный ключ, только чтение)
                                     ▼
                     Claude Code: subagent shturman-ko-analyst  ── анализ в рамках подписки,
                                     │                              без внешних LLM API
                                     │  POST /api/agent/ko/proposals (единственная запись)
                                     ▼
                     review_queue (kind "ko_proposal") ──► человек в дашборде: «Применить» / «Отклонить»
```

Сервер остаётся детерминированным: он хранит, отдаёт и проверяет. Понимание текста и фото
выполняет Claude Code. Решения применяет только человек.

## Что входит в репозиторий сейчас

| Файл | Назначение |
|---|---|
| `.claude/agents/shturman-ko-analyst.md` | определение субагента: роль, запреты, порядок работы, формат предложений |
| `.claude/hooks/ko-analyst-guard.js` | PreToolUse-хук субагента: технически блокирует Telegram Bot API, внешние LLM API, запись в «Штурман» кроме очереди предложений, Railway CLI, `git push`, `/data`, `shturman.json`, запись файлов вне `.ko-analyst/` |
| `tools/ko_analyst/ledger.js` | детерминированная часть: проверка предложений, стабильный `dedupe_key`, идемпотентное слияние, курсор, ссылки на сообщения |
| `tests/test_ko_analyst.py`, `tests/js/ko_analyst_unit.js` | тесты хука, журнала и определения агента |

Код сервера, Telegram webhook, Excel-агент (`plan_agent/`) и данные на Volume не изменены.

## Как запустить сейчас (офлайн-режим)

1. Положить снимок в `.ko-analyst/inbox/`: `messages.json` (экспорт Telegram Desktop «JSON»
   из чата КО или нормализованный массив — формат в определении агента), папку `photos/`,
   `projects.json` (выгрузка проектов из дашборда).
2. В Claude Code: `@agent-shturman-ko-analyst разбери .ko-analyst/inbox` или
   `claude --agent shturman-ko-analyst`.
3. Результат: `.ko-analyst/out/proposals.new.json` и отчёт. Ничего никуда не отправляется.

Субагенты из `.claude/agents/` подхватываются при старте сессии — в уже открытой сессии,
где файл был создан, может понадобиться новая сессия.

## Найденные ограничения текущего Railway-кода (почему нужна доработка)

1. **Сырые сообщения КО не хранятся целиком.** `telegram_log` содержит текст, но без
   `message_id`, автора, даты, `reply_to`/`media_group_id`; журнал обрезается до 2000 записей.
   Без `message_id` нельзя дать ссылку на исходное сообщение.
2. **Фото.** Сохраняется только при совпадении проекта или в очередь `telegram_photo`;
   из альбома — только последнее сообщение каждого апдейта, это нормально, но связь фото ↔ сообщение
   хранится только в payload очереди.
3. **Нет авторизованного чтения для агента.** `/api/telegram/log`, `/api/review-queue`,
   `/uploads/*` закрыты сессией дашборда (cookie), а не ключом агента.
4. **`AGENT_API_KEY` слишком мощный** для аналитика: им можно делать `PATCH` проектов
   и `POST /stage-transitions` — то есть менять статусы без подтверждения. Аналитику нужен
   отдельный ключ с правами «чтение + очередь предложений».
5. **Платный API в канале КО.** `server/telegram/engineeringChannel.js` вызывает Anthropic
   Messages API, если задан `ANTHROPIC_API_KEY`. Чтобы не использовать платный API и не получать
   двойные предложения, в канале КО этот вызов нужно отключать флагом (см. п. «д»).

## Реализация обмена с Railway (этап 2)

### а) Архив сообщений КО — коллекция `ko_messages`

Запись **в начале** `engineeringChannel.handle()`, до любого анализа, append-only:

```json
{ "seq": 1931, "chat_id": "-100…", "message_id": 4821, "date": "2026-11-20T09:14:00Z",
  "from": { "id": "555", "name": "Иванов", "username": "ivanov" },
  "text": "…", "reply_to_message_id": 4815, "media_group_id": null,
  "photos": [{ "file_id": "ko_…", "storage_key": "ko/…jpg", "width": 1280, "height": 960 }],
  "update_id": 99121, "archived_at": "…" }
```

- Идемпотентность: уникальность по `chat_id:message_id` (повторная доставка Telegram — no-op,
  дополнительно к существующему `telegram_updates`).
- `seq` — монотонный счётчик для курсора агента.
- Фото: сохранять наибольший размер каждого фото через уже существующий `botApi.downloadFile`
  (это `getFile` — чтение, не отправка; хард-гвард `botApi.js` остаётся в силе).
- Хранить в отдельном файле `ko-messages.jsonl` рядом с `shturman.json` или в новой коллекции
  `store.js` (новая коллекция добавляется без смены формата, как `telegram_log`).
- Сам webhook (маршрут, секрет, `setWebhook`, `allowed_updates`) **не меняется**.

### б) Отдельный ключ `KO_ANALYST_API_KEY` и middleware `requireKoAnalystAuth`

- Отдельный секрет (по аналогии с `WINDOWS_AGENT_API_KEY`), отзывается независимо.
- Разрешает **только** маршруты `/api/agent/ko/*`; на все прочие `/api/agent/*` → 403.
- Ограничение частоты запросов (как в `requireWindowsAgentAuth`), журнал обращений.
- Пока ключ не задан — 501 (fail closed, как остальные ключи).

### в) Эндпоинты

| Метод | Путь | Что делает |
|---|---|---|
| GET | `/api/agent/ko/messages?after=<seq>&limit<=500` | сообщения архива по возрастанию `seq`, `next_after` |
| GET | `/api/agent/ko/files/:file_id` | байты фото из архива (только файлы, упомянутые в `ko_messages`) |
| GET | `/api/agent/ko/projects` | проекты: id, external id, название, марка, модель, изделие, артикул, `status`, `planned_stage`, ответственный, срок, активности |
| GET | `/api/agent/ko/proposals?status=open` | ранее поданные предложения (для дедупликации в облачном запуске) |
| POST | `/api/agent/ko/proposals` | пакет до 100 предложений → `review_queue` kind `ko_proposal`, upsert по `dedupe_key` |

Проверки сервера для `POST /proposals`: белый список `kind`; каждое `evidence` ссылается на
существующую запись `ko_messages` (иначе 400 — агент не может «придумать» источник); лимит размера;
`requires_approval` принудительно `true`. Эндпоинт **никогда** не трогает `projects`, `tasks`,
`project_files`.

### г) Очередь на подтверждение

- В дашборде раздел «На проверке» показывает `ko_proposal`: суть, обоснование, цитаты со ссылками
  на сообщения, фото, уверенность.
- Кнопка «Применить» (сессия + CSRF, как «Подключить webhook») выполняет изменение существующими
  механизмами: `POST /api/projects/:id/stage` для этапа, `PATCH /api/projects/:id` для
  ответственного/срока (через `safeUpdate` с защитой ручных правок), привязка фото — через
  `attachProjectFile`, задача — `docStore("tasks").create`. В историю проекта пишется
  `source: "ko_analyst"`, кто подтвердил и ссылки на сообщения.
- «Отклонить» — `resolve(id, "rejected")`; повторная подача того же `dedupe_key` не открывает
  отклонённое предложение заново (поведение `upsertIn` для решённых человеком).

### д) Флаг `TELEGRAM_ENGINEERING_AI_ENABLED=false`

Отключает вызов Anthropic API только в канале КО: сообщение архивируется (п. «а»), фото
сохраняются, в `review_queue` ничего не пишется — анализ делает `shturman-ko-analyst`.
Личный канал не затрагивается.

### е) Сеть и секрет для облачной сессии Claude Code

- В облачной среде «Сборка планов» сейчас Railway-хост **заблокирован** (проверено: `CONNECT 403`
  от прокси). Нужно добавить ровно один хост `projektmanagment-production.up.railway.app`
  в разрешённые.
- `KO_ANALYST_API_KEY` — сохранить как **network secret** среды (прокси подставляет заголовок
  `Authorization` сам; ключ не попадает в промпт, переменные окружения и логи). В команде агента
  достаточно фиктивного `$KO_ANALYST_API_KEY`.
- `api.telegram.org` в эту среду не добавлять.

### Где что лежит в коде

| Пункт | Файлы |
|---|---|
| а) архив | `server/ko/archive.js`; вызов в начале `server/telegram/engineeringChannel.js#handle`; коллекции `ko_messages`, `ko_files`, объект `ko_archive` в `server/store.js` |
| б) ключ | `server/auth.js#requireKoAnalystAuth` |
| в) API агента | `server/routes/koAgent.js` (`/api/agent/ko/*`, смонтирован до `/api/agent`) |
| г) подтверждение | `server/ko/proposals.js` (submit/apply/reject), `server/routes/koDashboard.js` (`/api/ko/*`), вкладка «Предложения агента КО» в `public/index.html` |
| д) флаг | `engineeringChannel.aiEnabled()` |
| общие правила | `server/ko/rules.js` (их же использует `tools/ko_analyst/ledger.js`) |

Не менялись: `server/routes/telegramWebhook.js` (маршрут, секрет, `allowed_updates`), регистрация
webhook, `plan_agent/`, `server/planSource/*`, `server/agentJobs.js`, существующие данные.

### Как «Применить» повторяет проверки Канбана

| Тип | Что вызывается | Те же проверки, что у |
|---|---|---|
| `stage_change` | `projectsModel.moveStage(source:"manual", expectedVersion)` + проверка, что этап не изменился с момента предложения | `POST /api/projects/:id/stage` |
| `assignee_change`, `deadline_change` | `validateProjectInput(allowed: MANUAL_EDITABLE)` + `safeUpdate(source:"manual", expectedVersion)` | `PATCH /api/projects/:id` |
| `work_done`, `problem` | добавление комментария через `safeUpdate` | `POST /api/projects/:id/comments` |
| `photo_link` | `storage.validateFile` + `attachProjectFile` (дедупликация по чату/сообщению) | загрузка фото |
| `new_task` | `docStore("tasks").create` во «Входящие» | `POST /api/tasks` |
| `project_match`, `needs_clarification` | ничего не меняет, только «Принять к сведению» | — |

Поле изменения фиксировано типом (`rules.FIELD_BY_KIND`), агент не может назвать другое поле.
Применить может только человек: сессия дашборда + CSRF; ключ агента на `/api/ko/*` не принимается.
Операция синхронная — двойной клик не применит дважды; применённое/отклонённое предложение закрыто
навсегда. В историю проекта пишется переход/изменение от имени человека (`source: manual`) и запись
`ko_proposal_applied` со ссылками на сообщения.

## Типы сообщений в архиве

`content_type` различает `text, photo, video, document, voice, audio, sticker, animation, service,
other`. Для вложений в `media[]` сохраняются метаданные Telegram: `file_id` (только на сервере,
в API не отдаётся), `file_unique_id`, MIME, размер, имя файла, длительность, размеры, для стикера —
emoji и набор. Скачиваются только изображения (фото и изображения, отправленные файлом) — в
`KO_FILES_DIR`, как раньше. Видео, голосовые, аудио, прочие документы, стикеры и GIF —
`download: "metadata_only"`, без обращений к Telegram. Служебные события — `service.event` +
безопасные детали (id участников без имён, новый chat_id при миграции, id закреплённого сообщения).
Для ответа на сообщение, которого нет в архиве, сохраняется `reply_context` (автор, дата, тип,
до 500 символов текста). Записи, сохранённые до этого изменения, не переписываются.

## Идемпотентность — три уровня

1. Railway: `telegram_updates` (update_id) + уникальность `ko_messages` по `chat_id:message_id`.
2. Агент: курсор `last_seq` + локальный журнал `ledger.js` (стабильный `dedupe_key`: для смены
   этапа/ответственного/срока — проект + поле + новое значение; для фактов — проект + исходные
   сообщения; для задач — проект + нормализованный заголовок; для фото — проект + file_id).
3. Сервер: `review_queue` upsert по `dedupe_key` — повтор обновляет одно предложение (добавляет
   доказательства), а не создаёт второе. В облаке локальный журнал теряется вместе с контейнером,
   поэтому источник истины — п. 3 и `GET /proposals`.

## Плановый запуск (на будущее, сейчас не включён)

Облачные Scheduled Tasks доступны на аккаунте (в нём уже есть три задачи, все выключены).
Минимальный интервал — 1 час, расход идёт из лимитов подписки. Когда п. «а»–«е» будут готовы,
можно создать задачу, например будни в 10:17, 13:17, 16:17 МСК, в среде с доступом только к
Railway-хосту, с промптом «запусти shturman-ko-analyst в онлайн-режиме, отправь предложения».
