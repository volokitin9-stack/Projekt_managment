# Running the Windows agent unattended

`plan_agent/windows_agent.py` is a normal Python process: it runs fine from a terminal (`python
-m plan_agent.windows_agent`) for a first test. For the always-on laptop, pick ONE of the two
options below to keep it running with no one logged in, surviving reboots and crashes.

Both need: Python with this repo's dependencies installed (`pip install -r requirements.txt`,
ideally into the project's own `.venv`), and a **dedicated, least-privilege Windows account**
created ahead of time (not your own login, not a domain admin) with just enough rights to read the
workbook's network share. Neither script ever writes a password to disk — both prompt for it
interactively (`Get-Credential`) and hand it straight to Task Scheduler / NSSM.

## Option A: Task Scheduler (`install_task_scheduler.ps1`)

No extra download. Recommended default.

```powershell
# As Administrator, from the windows/ folder:
Copy-Item windows_agent.env.example C:\ShturmanAgent\agent.env   # then edit it with real values
.\install_task_scheduler.ps1 -EnvFile C:\ShturmanAgent\agent.env -ServiceAccount ".\svc-shturman-agent"
```

This sets every variable from the env file as a **Machine**-scope environment variable (so it is
never embedded in the task itself), registers a task that starts at boot, runs whether anyone is
logged on or not, restarts up to 999 times at 1-minute intervals if the process dies, never shows
a window, and starts it immediately (no reboot needed to begin).

`windows_agent_task.xml.template` is the same task definition for `schtasks /create /xml` or Group
Policy deployment instead of the PowerShell script - read the note inside it about setting
environment variables yourself first (`setx ... /M`), since a raw task XML has no field for them.

## Option B: a Windows Service via NSSM (`install_nssm_service.ps1`)

Download [NSSM](https://nssm.cc/) first (this repo cannot bundle a binary). Use this if your team
already manages everything else as services, or wants `services.msc` / `sc query` visibility.

```powershell
.\install_nssm_service.ps1 -EnvFile C:\ShturmanAgent\agent.env -ServiceAccount ".\svc-shturman-agent" -NssmExe C:\tools\nssm.exe
```

Sets the environment variables as that service's own `AppEnvironmentExtra` (not machine-wide),
configures auto-start and restart-on-exit, and starts it.

## After installing (either option)

1. Check the log: `<PLAN_AGENT_STATE_DIR>\logs\windows_agent.log` (rotated, 5 files × 5 MB). It
   should show `Windows agent starting: url=... workbook=...` within a few seconds.
2. On the dashboard, **Инж. проекты → Импорт плана** (or the `/api/plan-source/status` API) should
   show the agent `online: true` within `WINDOWS_AGENT_POLL_SECONDS` plus a few seconds.
3. Click «Обновить» on the board and confirm a real result shows up (see
   `docs/RAILWAY_DEPLOYMENT.md#verifying-the-windows-agent` for the full checklist).
4. Reboot the laptop once and confirm the agent comes back online on its own, with no one logged
   in and no terminal open.

## Uninstall

- Task Scheduler: `Stop-ScheduledTask -TaskName ShturmanWindowsAgent; Unregister-ScheduledTask -TaskName ShturmanWindowsAgent -Confirm:$false`
- NSSM: `nssm stop ShturmanWindowsAgent; nssm remove ShturmanWindowsAgent confirm`

Either way, the machine-scope environment variables set by `install_task_scheduler.ps1` are not
removed automatically (NSSM's per-service ones are, with the service). Remove them yourself with
`[Environment]::SetEnvironmentVariable("WINDOWS_AGENT_API_KEY", $null, "Machine")` etc. if you no
longer want them on the machine.

## What was and wasn't verified here

This repository's automated tests (`tests/test_windows_agent.py`) run the real
`windows_agent.py` as a subprocess against a real Штурман server and a synthetic workbook, and
confirm the full analyze → approve → sync chain actually writes project data. That exercises the
script's logic (job handling, the approve-before-sync fix, crash-safe result submission, clean
shutdown) in full.

What that CANNOT exercise, because this was written in a Linux sandbox with no Windows machine or
Task Scheduler/NSSM available: the two PowerShell scripts and the XML template in this folder were
written and reviewed, not run. Verify on the real laptop: that the task/service is actually
created, starts at boot with no one logged in, restarts after `taskkill /F` on its process, and
that the dedicated account can actually reach the workbook's network share.
