param(
    [Parameter(Mandatory=$true)][string]$Config,
    [ValidatePattern('^3\.\d+$')][string]$PythonVersion = '3.12',
    [switch]$Check
)
$ErrorActionPreference = 'Stop'
$worker = Join-Path $PSScriptRoot 'worker.py'
if ($Check) { py "-$PythonVersion" $worker --config $Config --check }
else { py "-$PythonVersion" $worker --config $Config }
exit $LASTEXITCODE
