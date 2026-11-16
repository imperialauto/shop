param([Parameter(Mandatory=$true)][string]$Config, [switch]$Check)
$ErrorActionPreference = 'Stop'
$worker = Join-Path $PSScriptRoot 'worker.py'
if ($Check) { py -3 $worker --config $Config --check }
else { py -3 $worker --config $Config }
exit $LASTEXITCODE
