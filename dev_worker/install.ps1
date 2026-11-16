param([Parameter(Mandatory=$true)][string]$Config)
$ErrorActionPreference = 'Stop'
$Config = (Resolve-Path $Config).Path
# Preflight before registration. The owner sets DEV_EXECUTOR_KEY privately in
# their user environment; never put it in task arguments or in this repository.
& (Join-Path $PSScriptRoot 'run-worker.ps1') -Config $Config -Check
if ($LASTEXITCODE -ne 0) { throw 'Preflight failed' }
$python = (& py -3 -c 'import sys; print(sys.executable)').Trim()
$worker = Join-Path $PSScriptRoot 'worker.py'
$action = New-ScheduledTaskAction -Execute $python -Argument "`"$worker`" --config `"$Config`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
Register-ScheduledTask -TaskName 'ImperialDevelopmentWorker' -Action $action -Trigger $trigger -Settings $settings -Description 'Separate outbound Imperial development worker; no merge or deployment' -Force | Out-Null
Write-Host 'Registered for next logon. Not started. Diagnostic Local Node is unchanged.'
