<#
  (PowerShell 5.1: always wrap WorkerProcs in @() before using .Count.)
  Imperial Local Node - QA offline/restart test (Windows PowerShell 5.1). Synthetic QA only.
  1. Prints why the worker stopped (task state, worker processes, sleep/wake events).
  2. Starts the worker so queued QA reviews run.            (~2 min)
  3. Stops it for 5 minutes so the job page shows offline.  (~5 min)
  4. Always starts it again at the end, even if interrupted (Ctrl+C) or an error occurs.
  Total about 8 minutes. Leave the window open until it says DONE.
#>
$ErrorActionPreference = "Continue"
$task = "Imperial Local Node"
function Say($m) { Write-Host ("[{0:HH:mm:ss}] {1}" -f (Get-Date), $m) -ForegroundColor Cyan }
function WorkerProcs {
    @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
      Where-Object { $_.CommandLine -like "*Imperial*LocalNode*worker.py*" })
}
function StopWorker {
    Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    foreach ($p in @(WorkerProcs)) { Stop-Process -Id $p.ProcessId -Force; Say "stopped worker process $($p.ProcessId)" }
}
function StartWorker {
    foreach ($p in @(WorkerProcs)) { Stop-Process -Id $p.ProcessId -Force; Say "cleared leftover worker process $($p.ProcessId)" }
    Start-ScheduledTask -TaskName $task
    Start-Sleep -Seconds 5
    Say ("task state: " + (Get-ScheduledTask -TaskName $task).State + "; worker processes: " + @(WorkerProcs).Count)
}

Say "=== 1. Diagnostics ==="
$info = Get-ScheduledTaskInfo -TaskName $task
Say ("task state: " + (Get-ScheduledTask -TaskName $task).State + "; last run: " + $info.LastRunTime + "; last result: 0x" + ('{0:X}' -f $info.LastTaskResult))
$procs = @(WorkerProcs)
Say ("worker processes now: " + $procs.Count)
foreach ($p in $procs) { Say ("  pid " + $p.ProcessId + " started " + $p.CreationDate) }
Say "power events in the last 3 hours (sleep/wake/standby):"
Get-WinEvent -FilterHashtable @{LogName = "System"; StartTime = (Get-Date).AddHours(-3); Id = 1, 42, 107, 506, 507} -ErrorAction SilentlyContinue |
    Select-Object -First 12 | ForEach-Object { Write-Host ("  {0:HH:mm:ss}  id {1}  {2}" -f $_.TimeCreated, $_.Id, $_.ProviderName) }
Say "task scheduler history for this task (last 3 hours):"
Get-WinEvent -FilterHashtable @{LogName = "Microsoft-Windows-TaskScheduler/Operational"; StartTime = (Get-Date).AddHours(-3)} -ErrorAction SilentlyContinue |
    Where-Object { $_.Message -like "*Imperial Local Node*" } | Select-Object -First 12 |
    ForEach-Object { Write-Host ("  {0:HH:mm:ss}  id {1}  {2}" -f $_.TimeCreated, $_.Id, ($_.Message -split "`n")[0]) }
Say "last worker log lines:"
Get-Content "D:\Imperial\LocalNode\logs\worker.log" -Tail 5 | ForEach-Object { Write-Host "  $_" }

try {
    Say "=== 2. Starting worker (queued QA reviews should run) ==="
    StartWorker
    Start-Sleep -Seconds 120

    Say "=== 3. Stopping worker for 5 minutes (job page should show 'Workstation offline') ==="
    StopWorker
    Start-Sleep -Seconds 300
}
finally {
    Say "=== 4. Restoring worker ==="
    StartWorker
    Say "DONE - copy everything in this window and paste it back to Claude."
}
