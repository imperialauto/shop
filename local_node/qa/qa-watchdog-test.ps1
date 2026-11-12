<#
  (PowerShell 5.1: always wrap WorkerProcs in @() before using .Count.)
  Imperial Local Node - QA watchdog test (Windows PowerShell 5.1). Synthetic QA only.
  Run AFTER re-running install.ps1. Shows the task now runs pythonw.exe with no window,
  then kills the worker the way a crash would and waits for the 5-minute watchdog to
  bring it back. Always leaves the worker running at the end. About 6-7 minutes.
#>
$ErrorActionPreference = "Continue"
$name = "Imperial Local Node"
function Say($m) { Write-Host ("[{0:HH:mm:ss}] {1}" -f (Get-Date), $m) -ForegroundColor Cyan }
function WorkerProcs {
    @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
      Where-Object { $_.CommandLine -like "*Imperial*LocalNode*worker.py*" })
}

Say "=== 1. Task definition ==="
$task = Get-ScheduledTask -TaskName $name
Say ("action: " + $task.Actions[0].Execute)
Say ("triggers: " + (($task.Triggers | ForEach-Object { $_.CimClass.CimClassName + $(if ($_.Repetition.Interval) { " every " + $_.Repetition.Interval } else { "" }) }) -join "; "))
Say ("state: " + $task.State)
$procs = @(WorkerProcs)
foreach ($p in $procs) { Say ("worker: " + $p.Name + " pid " + $p.ProcessId) }
if ($procs.Count -eq 0) { Say "no worker running - starting it"; Start-ScheduledTask -TaskName $name; Start-Sleep -Seconds 5; $procs = @(WorkerProcs) }

try {
    Say "=== 2. Killing the worker like a crash (task is NOT stopped) ==="
    foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force; Say ("killed pid " + $p.ProcessId) }
    $killed = Get-Date
    Say "=== 3. Waiting for the watchdog (up to 7 minutes) ==="
    $back = $null
    while (((Get-Date) - $killed).TotalMinutes -lt 7) {
        Start-Sleep -Seconds 15
        $now = @(WorkerProcs)
        if ($now.Count -gt 0) { $back = $now; break }
    }
    if ($back) {
        Say ("WATCHDOG OK: worker back after {0:N0} s as {1} pid {2}" -f ((Get-Date) - $killed).TotalSeconds, $back[0].Name, $back[0].ProcessId)
    } else {
        Say "WATCHDOG FAILED: no worker after 7 minutes"
    }
}
finally {
    if (@(WorkerProcs).Count -eq 0) { Say "restoring worker"; Start-ScheduledTask -TaskName $name; Start-Sleep -Seconds 5 }
    Say ("final: worker processes " + @(WorkerProcs).Count + "; task state " + (Get-ScheduledTask -TaskName $name).State)
    Say "DONE - copy everything in this window and paste it back to Claude."
}
