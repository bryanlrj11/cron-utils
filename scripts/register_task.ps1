# Registers the daily jobs.nba run with Windows Task Scheduler (stats.nba.com
# blocks cloud IPs, so this job runs from a home connection instead of Actions).
#
# One-time setup from the repo root:
#   python -m venv .venv
#   .venv\Scripts\pip install -r requirements-nba.txt
#   copy .env.example .env   (fill in COCKROACH_DATABASE_URL)
#   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
#
# -At is local time. 18:00 in Singapore is 05:00-06:00 ET, after every game of
# the previous US night is final. The logon trigger catches up missed days.
param([string]$At = "18:00")

$repo = Split-Path $PSScriptRoot -Parent
$action = New-ScheduledTaskAction -Execute (Join-Path $repo ".venv\Scripts\pythonw.exe") `
    -Argument "-m jobs.nba" -WorkingDirectory $repo
$triggers = @(
    (New-ScheduledTaskTrigger -Daily -At $At),
    (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME)
)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "cron-utils nba" -Action $action -Trigger $triggers `
    -Settings $settings -Description "Daily NBA schedule + box score collection (logs\nba.log)" -Force
