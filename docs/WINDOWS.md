# Running Штурман on Windows

## Python for the planning agent

The server starts the planning agent (`python -m plan_agent`) with the interpreter chosen by `server/pythonResolver.js`:

1. `PLAN_PYTHON_EXECUTABLE`, used exactly as given (surrounding quotes are removed). A bare name like `python` is looked up on PATH; the Microsoft Store folder `…\AppData\Local\Microsoft\WindowsApps` is skipped.
2. The project's own virtual environment: `<project>\.venv\Scripts\python.exe`. The path is taken relative to the project folder, not the current directory.
3. A verified system Python (the `py` launcher, `python.exe` on PATH), never the Store alias.

Every agent run uses this interpreter: «Обновить», «Проверить обновления», «Пробный анализ», «Синхронизировать» / «Утвердить синхронизацию» and the 09:00 schedule. The agent is started with an argument list (`shell: false`), so paths with spaces or Cyrillic need no extra quoting.

### `.env`

`.env` is read from the project folder, wherever `npm start` is run from. Backslashes are never escape sequences, so write the path as is:

```text
PLAN_PYTHON_EXECUTABLE=C:\Users\user\Projekt_managment\.venv\Scripts\python.exe
```

**Delete the line `PLAN_AGENT_PYTHON=python3`** if your `.env` was copied from an older `.env.example`. It is now ignored, and a warning is shown. It was the cause of the error «Python was not found; run without arguments to install from the Microsoft Store»: on Windows, `python3` is the Store alias, and `.venv\Scripts` contains `python.exe` but no `python3.exe`, so adding it to PATH did not help.

## Verification on the Windows PC (real, not mocked)

Run these in `cmd`, in the project folder.

1. Update the code:
   ```bat
   cd /d C:\Users\user\Projekt_managment
   ```
   ```bat
   git pull origin claude/ecstatic-pascal-7feq2f
   ```
2. Edit `.env`: set `PLAN_PYTHON_EXECUTABLE` as above, and remove `PLAN_AGENT_PYTHON` if present.
3. Check the interpreter without starting the server:
   ```bat
   npm run check-python
   ```
   Expected: `Python агента  C:\Users\user\Projekt_managment\.venv\Scripts\python.exe  [PLAN_PYTHON_EXECUTABLE]`, then `Интерпретатор OK … 3.13.12`, `Библиотеки OK`, `Агент plan_agent OK` and `РЕЗУЛЬТАТ  Python-агент запускается: OK`. Any `ОШИБКА` line names the path and the error code.
4. Start the server:
   ```bat
   npm start
   ```
   The console shows `Python для агента планирования: C:\Users\user\Projekt_managment\.venv\Scripts\python.exe (PLAN_PYTHON_EXECUTABLE)`.
5. In the browser, go to **Инж. проекты → Импорт плана**. The row «Python агента» shows the same path.
6. Click «Пробный анализ». The result is a dry run with counts, not «Python was not found».
7. Click «Проверить обновления», then «Обновить» on the board. The button reports «Изменений нет», «Обновлено» or «Требуется проверка», but not «Ошибка обновления».
8. If a first approval is pending, click «Утвердить синхронизацию». The status becomes «Актуально» and the projects appear on the board.
9. Optional automated tests on the PC:
   ```bat
   .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
   ```
   ```bat
   npm test -- tests/test_python_resolver.py
   ```
   On Windows, this also runs `test_windows_project_venv_runs_the_agent` against the real `.venv`.

If step 3 fails, the output shows which paths were tried and why. Send that output; it contains no keys or passwords.

## Text encoding (Russian data on Windows)

All text files of the planning agent are UTF-8:
- metadata `state/inbox/<sha256>/meta.json`;
- reports `.json`, `.txt` and `_analysis.md`;
- `review_queue.json`, `sync_history.jsonl`;
- the mapping YAML files and the deployment status.

They are written atomically (temporary file, then replace), and the server starts the agent with `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8`.

**Earlier versions** wrote some of these files in the Windows code page (cp1251) and could fail with `UnicodeEncodeError` on symbols such as «→». After the switch to UTF-8 mode, reading such a file failed with `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xd1`.

**Existing metadata files** are migrated automatically on first read, only for the known metadata files (`meta.json` and the deployment status):
1. strict UTF-8 is tried first, then strict cp1251;
2. the result must be valid JSON with the expected structure;
3. the original is kept as `meta.json.cp1251-<time>.bak`;
4. the file is rewritten as UTF-8, atomically;
5. the migration is recorded in `state/encoding_migrations.jsonl`.

A file that is neither valid UTF-8 nor valid cp1251 JSON is **not changed**. The run stops with `metadata file cannot be read: <path>: …`. Repair that file, or move it away and run again; the workbook itself is not affected.

Report files written by older versions are not rewritten. They are only displayed, so Cyrillic in them may look garbled; new reports are UTF-8.
