# Claude Code: агент `shturman-ko-analyst`

Этот каталог в ветке `main` нужен только для запуска агента анализа чата КО из новой облачной
сессии Claude Code (по умолчанию она клонирует `main`). Код «Доски проектов» живёт в ветке
`claude/railway-deployment`; здесь нет серверных модулей, импортёра и секретов.

- `agents/shturman-ko-analyst.md` — инструкции агента (ежедневный режим: серверный курсор →
  новые сообщения → предложения в очередь подтверждения → курсор).
- `settings.json` + `hooks/ko-analyst-guard.js` — PreToolUse-хук проекта. Он вызывается для всех
  сессий репозитория, но правила применяет только к вызовам субагента `shturman-ko-analyst`
  (по полю `agent_type` во входе хука; основная сессия и другие агенты не затрагиваются).
  Хуки во frontmatter субагента в облачных сессиях не выполняются — проверено, поэтому хук здесь.
  Для агента блокирует Telegram Bot API, внешние LLM API, любые записи в «Доску
  проектов», кроме `POST /api/agent/ko/proposals` и `PUT /api/agent/ko/cursor`, Railway CLI,
  `git push`, `/data`, запись файлов вне `.ko-analyst/`.

Доступ к API: облачная среда «Сборка планов», Network Secret для
`projektmanagment-production.up.railway.app` (ключ `KO_ANALYST_API_KEY` подставляет прокси;
в репозитории его нет). Подробности и промпт задачи — `docs/KO_ANALYST_SCHEDULE.md` в ветке
`claude/railway-deployment`.

Изменения агента и хука делаются в `claude/railway-deployment` и переносятся сюда той же правкой.
