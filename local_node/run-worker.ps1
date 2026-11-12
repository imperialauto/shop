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

# The worker decrypts its own key (DPAPI, this Windows user) - the same path the
# Scheduled Task uses, so -Mode check also proves the background start will work.
$workerArgs = @((Join-Path $root "worker\worker.py"), "--config", $configFile, "--key-file", $keyFile)
if ($Mode) { $workerArgs += "--$Mode" }
& $python @workerArgs
exit $LASTEXITCODE
