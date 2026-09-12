param(
    [string]$ProjectRoot = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)),
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path $ProjectRoot).Path
$Python = (Get-Command python.exe -ErrorAction Stop).Source
$Tasks = @("CCUSR Console", "CCUSR Scheduler")

if ($Remove) {
    foreach ($Task in $Tasks) {
        Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue
    }
    Write-Host "CC USR Windows tasks removed."
    exit 0
}

$RunRoot = Join-Path $ProjectRoot "runs\daemon"
New-Item -ItemType Directory -Force -Path $RunRoot | Out-Null
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$Definitions = @(
    @{ Name = "CCUSR Console"; Arguments = "`"$ProjectRoot\webapp\server.py`" --host 127.0.0.1 --port 4173 --db `"$ProjectRoot\production.sqlite3`" --no-open" },
    @{ Name = "CCUSR Scheduler"; Arguments = "`"$ProjectRoot\tools\pipeline_daemon.py`" --loop --concurrency 2 --codex-concurrency 1 --worker-image ccusr-claude-worker:local --worker-timeout 3600 --agent-timeout 3600 --poll-seconds 60 --failure-threshold 3 --min-free-gb 5 --log-retention-days 30 --max-log-gb 2" }
)

foreach ($Definition in $Definitions) {
    $Action = New-ScheduledTaskAction -Execute $Python -Argument $Definition.Arguments -WorkingDirectory $ProjectRoot
    Register-ScheduledTask -TaskName $Definition.Name -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Force | Out-Null
}

Start-ScheduledTask -TaskName "CCUSR Console"

Write-Host "CC USR Windows tasks installed for $ProjectRoot"
Write-Host "Console: http://127.0.0.1:4173/"
Write-Host "The scheduler preserves its database state; verify the target state before starting production."
