<#
.SYNOPSIS
  Stops and removes the Imperial Local Node Scheduled Task (Windows PowerShell 5.1).
  Leaves Ollama, models and logs alone. -RemoveKey also deletes the stored worker key.
#>
param([string]$InstallDir = "D:\Imperial\LocalNode", [switch]$RemoveKey)

$ErrorActionPreference = "Stop"
$task = Get-ScheduledTask -TaskName "Imperial Local Node" -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName "Imperial Local Node" -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName "Imperial Local Node" -Confirm:$false
    Write-Host "Scheduled Task removed."
} else {
    Write-Host "Scheduled Task was not installed."
}
Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -like "*Imperial*LocalNode*worker.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host "Stopped worker process $($_.ProcessId)." }
if ($RemoveKey) {
    $keyFile = Join-Path $InstallDir "config\worker.key"
    if (Test-Path $keyFile) { Remove-Item $keyFile -Force; Write-Host "Stored worker key deleted." }
}
Write-Host "Done. Also delete or rotate LOCAL_NODE_WORKER_KEY in Railway if this workstation is retired."
