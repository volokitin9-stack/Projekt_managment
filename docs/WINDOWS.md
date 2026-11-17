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
