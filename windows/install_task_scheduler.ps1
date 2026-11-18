<#
.SYNOPSIS
  Installs plan_agent/windows_agent.py as a Scheduled Task that starts at boot, runs whether
  anyone is logged on or not, restarts itself if it crashes, and never shows a console window.

.DESCRIPTION
  Run this ONCE, as Administrator, on the always-on Windows laptop. It:
   1. Reads KEY=VALUE lines from -EnvFile (see windows_agent.env.example) and sets each as a
      persistent Machine-scope environment variable. Scheduled tasks inherit machine environment
      variables at launch, so windows_agent.py sees them with no .env loader needed on the Python
      side. Nothing from -EnvFile is written into this script, the task definition, or the repo.
   2. Prompts (interactively, via Get-Credential — never a script parameter) for the dedicated
      service account's password, so the task can run unattended under that account. NO PASSWORD
      IS EVER WRITTEN TO DISK BY THIS SCRIPT.
   3. Registers the task with: start at system startup, restart on failure (999 retries, 1 minute
      apart), no execution time limit (this must run forever), "run whether user is logged on or
      not" (no interactive session or open terminal required).
   4. Starts the task immediately, so a reboot is not required to begin working.

.PARAMETER EnvFile
  Path to a KEY=VALUE file with the settings in windows_agent.env.example. Required.

.PARAMETER ServiceAccount
  The dedicated, already-created Windows account to run the task as (e.g. ".\svc-shturman-agent").
  Use a LEAST-PRIVILEGE account created for this purpose, with just enough rights to read the
  workbook's share - not your own interactive login, not a domain admin. Required.

.PARAMETER PythonExe
  Full path to python.exe (ideally this project's own .venv). Default: .venv\Scripts\python.exe
  next to this script's project root.

.PARAMETER ProjectRoot
  Full path to the Projekt_managment checkout. Default: this script's grandparent folder.

.PARAMETER TaskName
  Scheduled task name. Default: "ShturmanWindowsAgent".

.EXAMPLE
  .\install_task_scheduler.ps1 -EnvFile C:\ShturmanAgent\agent.env -ServiceAccount ".\svc-shturman-agent"

.NOTES
  Verified: script logic and parameter handling only (this was written and reviewed, not executed,
  on a Linux sandbox with no Windows Task Scheduler available). Run it on the real laptop and
  confirm with the steps in docs/RAILWAY_DEPLOYMENT.md#verifying-the-windows-agent before relying
  on it. Needs to run as Administrator (setting a Machine-scope environment variable and
  registering a task that runs as another account both require it).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$EnvFile,
    [Parameter(Mandatory = $true)][string]$ServiceAccount,
    [string]$PythonExe,
    [string]$ProjectRoot,
    [string]$TaskName = "ShturmanWindowsAgent"
)

$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
    throw "Run this script from an elevated (Administrator) PowerShell prompt."
}
if (-not (Test-Path $EnvFile)) {
    throw "EnvFile not found: $EnvFile"
}
if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
if (-not $PythonExe) {
    $PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path $PythonExe)) {
    throw "Python not found at $PythonExe - pass -PythonExe, or create the project's .venv first."
}

Write-Host "Project root: $ProjectRoot"
Write-Host "Python:       $PythonExe"
Write-Host "Reading settings from: $EnvFile"

$required = @("PM_API_URL", "PM_AGENT_API_KEY", "WINDOWS_AGENT_API_KEY")
$seen = @{}
Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { return }
    $idx = $line.IndexOf("=")
    if ($idx -lt 1) { return }
    $name = $line.Substring(0, $idx).Trim()
    $value = $line.Substring($idx + 1).Trim()
    if ($value -eq "") { return }
    [Environment]::SetEnvironmentVariable($name, $value, "Machine")
    $seen[$name] = $true
    Write-Host "  set (machine) $name"
}
$missing = $required | Where-Object { -not $seen.ContainsKey($_) }
if ($missing) {
    throw "Missing required settings in ${EnvFile}: $($missing -join ', ')"
}
if (-not ($seen.ContainsKey("WINDOWS_AGENT_WORKBOOK_PATH") -or $seen.ContainsKey("WINDOWS_AGENT_WORKBOOK_FOLDER"))) {
    throw "Set WINDOWS_AGENT_WORKBOOK_PATH (or WINDOWS_AGENT_WORKBOOK_FOLDER + _PATTERN) in $EnvFile"
}

Write-Host ""
Write-Host "Enter the password for the dedicated service account ($ServiceAccount)."
Write-Host "It is used only to register the task below and is never written to disk."
$cred = Get-Credential -UserName $ServiceAccount -Message "Service account for $TaskName"

$action = New-ScheduledTaskAction -Execute $PythonExe `
    -Argument "-m plan_agent.windows_agent" `
    -WorkingDirectory $ProjectRoot

$trigger = New-ScheduledTaskTrigger -AtStartup

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Write-Host "Registering scheduled task '$TaskName'..."
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -User $cred.UserName -Password $cred.GetNetworkCredential().Password -RunLevel Limited `
    -Force | Out-Null

# "Run whether user is logged on or not" is the S4U/password logon type set above via -Password;
# confirm it, since some Register-ScheduledTask versions default to interactive-only otherwise.
$task = Get-ScheduledTask -TaskName $TaskName
$task.Principal.LogonType = "Password"
$task | Set-ScheduledTask | Out-Null

Write-Host "Starting the task now (no reboot required)..."
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ""
Write-Host "Task '$TaskName' registered and started. Last run result: $($info.LastTaskResult) (0 = still running/ok)"
Write-Host "Logs: see WINDOWS_AGENT_LOG_DIR in $EnvFile (default: <PLAN_AGENT_STATE_DIR>\logs\windows_agent.log)"
Write-Host "To stop:    Stop-ScheduledTask -TaskName '$TaskName'"
Write-Host "To remove:  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
