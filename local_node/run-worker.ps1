<#
.SYNOPSIS
  Starts the Imperial Local Node worker with its DPAPI-protected key (Windows PowerShell 5.1).
  -Mode ""     run continuously (what the Scheduled Task uses)
  -Mode check  commissioning checks
  -Mode eval   synthetic guardrail test on the real model
  -Mode once   one heartbeat + at most one task
#>
param([ValidateSet("check", "eval", "once")][string]$Mode = "")

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
if (-not $root) { $root = Split-Path -Parent $MyInvocation.MyCommand.Path }
$configFile = Join-Path $root "config\config.json"
$keyFile = Join-Path $root "config\worker.key"
$python = (Get-Content (Join-Path $root "config\python-path.txt") -TotalCount 1).Trim()

$secure = Get-Content $keyFile -TotalCount 1 | ConvertTo-SecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    # Only this process and the worker it starts can see the key.
    $env:LOCAL_NODE_WORKER_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

$workerArgs = @((Join-Path $root "worker\worker.py"), "--config", $configFile)
if ($Mode) { $workerArgs += "--$Mode" }
try {
    & $python @workerArgs
    $code = $LASTEXITCODE
} finally {
    Remove-Item Env:\LOCAL_NODE_WORKER_KEY -ErrorAction SilentlyContinue
}
exit $code
