<#
.SYNOPSIS
  Installs plan_agent/windows_agent.py as a real Windows Service using NSSM, as an alternative to
  install_task_scheduler.ps1. A service is generally the more robust choice for a process that
  must run 24/7 with no one ever logging in: the Service Control Manager's own restart/recovery
  semantics are simpler to reason about than a scheduled task's, and `sc query` / `services.msc`
  give a very visible health check.

.DESCRIPTION
  Requires NSSM (https://nssm.cc/) already downloaded - this script does not and cannot fetch
  binaries. Pass its path with -NssmExe, or put nssm.exe on PATH first.

  Run this ONCE, as Administrator. It:
   1. Reads KEY=VALUE lines from -EnvFile and sets them as this service's own extra environment
      (NSSM's AppEnvironmentExtra) - not machine-wide, not written into this script or the repo.
   2. Prompts interactively (Get-Credential) for the dedicated service account's password; NSSM
      is handed it once, directly, to configure the service's logon - it is never written to disk
      by this script.
   3. Configures auto-start, restart-on-exit, and a sane restart delay.
   4. Starts the service.

.PARAMETER EnvFile
  Path to a KEY=VALUE file - see windows_agent.env.example.

.PARAMETER ServiceAccount
  The dedicated, already-created Windows account the service runs as (e.g. ".\svc-shturman-agent").

.PARAMETER NssmExe
  Full path to nssm.exe. Default: "nssm" (resolved on PATH).

.PARAMETER PythonExe / -ProjectRoot / -ServiceName
  Same meaning as in install_task_scheduler.ps1. ServiceName defaults to "ShturmanWindowsAgent".

.NOTES
  Verified: script logic only, reviewed but not run against a real NSSM install (written on a
  Linux sandbox with no Windows service manager available). Confirm on the real laptop.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$EnvFile,
    [Parameter(Mandatory = $true)][string]$ServiceAccount,
    [string]$NssmExe = "nssm",
    [string]$PythonExe,
    [string]$ProjectRoot,
    [string]$ServiceName = "ShturmanWindowsAgent"
)

$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
    throw "Run this script from an elevated (Administrator) PowerShell prompt."
}
if (-not (Get-Command $NssmExe -ErrorAction SilentlyContinue)) {
    throw "nssm.exe not found ('$NssmExe'). Download it from https://nssm.cc/ and pass -NssmExe <path>."
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

$required = @("PM_API_URL", "PM_AGENT_API_KEY", "WINDOWS_AGENT_API_KEY")
$envLines = @()
$seen = @{}
Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { return }
    $idx = $line.IndexOf("=")
    if ($idx -lt 1) { return }
    $name = $line.Substring(0, $idx).Trim()
    $value = $line.Substring($idx + 1).Trim()
    if ($value -eq "") { return }
    $envLines += "$name=$value"
    $seen[$name] = $true
}
$missing = $required | Where-Object { -not $seen.ContainsKey($_) }
if ($missing) {
    throw "Missing required settings in ${EnvFile}: $($missing -join ', ')"
}
if (-not ($seen.ContainsKey("WINDOWS_AGENT_WORKBOOK_PATH") -or $seen.ContainsKey("WINDOWS_AGENT_WORKBOOK_FOLDER"))) {
    throw "Set WINDOWS_AGENT_WORKBOOK_PATH (or WINDOWS_AGENT_WORKBOOK_FOLDER + _PATTERN) in $EnvFile"
}

if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Write-Host "Service '$ServiceName' already exists - removing it first to reinstall cleanly."
    & $NssmExe stop $ServiceName | Out-Null
    & $NssmExe remove $ServiceName confirm | Out-Null
}

Write-Host "Installing service '$ServiceName'..."
& $NssmExe install $ServiceName $PythonExe "-m plan_agent.windows_agent"
& $NssmExe set $ServiceName AppDirectory $ProjectRoot
& $NssmExe set $ServiceName AppEnvironmentExtra ($envLines -join "`r`n")
& $NssmExe set $ServiceName Start SERVICE_AUTO_START
& $NssmExe set $ServiceName AppExit Default Restart
& $NssmExe set $ServiceName AppRestartDelay 5000
& $NssmExe set $ServiceName AppStdout ""   # windows_agent.py does its own rotating file logging
& $NssmExe set $ServiceName AppStderr ""

Write-Host ""
Write-Host "Enter the password for the dedicated service account ($ServiceAccount)."
Write-Host "It is handed to NSSM directly and is never written to disk by this script."
$cred = Get-Credential -UserName $ServiceAccount -Message "Service account for $ServiceName"
& $NssmExe set $ServiceName ObjectName $cred.UserName $cred.GetNetworkCredential().Password

Write-Host "Starting service '$ServiceName'..."
& $NssmExe start $ServiceName
Start-Sleep -Seconds 2
& $NssmExe status $ServiceName

Write-Host ""
Write-Host "Logs: see WINDOWS_AGENT_LOG_DIR in $EnvFile (default: <PLAN_AGENT_STATE_DIR>\logs\windows_agent.log)"
Write-Host "To stop:    nssm stop $ServiceName"
Write-Host "To remove:  nssm stop $ServiceName ; nssm remove $ServiceName confirm"
