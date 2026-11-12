<#
.SYNOPSIS
  Installs the Imperial Local Node worker on the shop workstation (Windows PowerShell 5.1).

.DESCRIPTION
  - Verifies Ollama is running, bound to 127.0.0.1 only, and has the model.
  - Verifies model storage (D:\Imperial\Models) and free space.
  - Copies worker.py into D:\Imperial\LocalNode\worker.
  - Stores the worker key encrypted with Windows DPAPI (only this Windows user can read it).
  - Writes config.json and registers a Scheduled Task "Imperial Local Node" that starts at
    logon and restarts on failure. No admin rights, no firewall rule, no listening port.
  - Runs the commissioning check (worker.py --check).

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -BackendUrl https://<your-qa-app>.up.railway.app
#>
param(
    [Parameter(Mandatory = $true)][string]$BackendUrl,
    [string]$WorkerId = ("shop-" + $env:COMPUTERNAME.ToLower()),
    [string]$InstallDir = "D:\Imperial\LocalNode",
    [string]$ModelsDir = "D:\Imperial\Models",
    [string]$SourceDir = "",
    [switch]$RebuildModel,
    [switch]$SkipTask
)

Set-StrictMode -Version 2
$ErrorActionPreference = "Stop"
$failures = 0
# Windows PowerShell 5.1 leaves $PSScriptRoot empty in param() defaults, so find
# this script's folder here instead.
if (-not $SourceDir) { $SourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path }

function Report([string]$Name, [bool]$Ok, [string]$Detail = "") {
    if ($Ok) { Write-Host "[PASS] $Name $Detail" -ForegroundColor Green }
    else { Write-Host "[FAIL] $Name $Detail" -ForegroundColor Red; $script:failures++ }
}

Write-Host "Imperial Local Node installer (PowerShell $($PSVersionTable.PSVersion))" -ForegroundColor Cyan

# --- 0. Inputs -------------------------------------------------------------------------
if ($BackendUrl -notmatch '^https://') { throw "BackendUrl must start with https:// (use the Railway QA URL)." }
$BackendUrl = $BackendUrl.TrimEnd('/')
if ($WorkerId -notmatch '^[A-Za-z0-9_-]{4,64}$') { throw "WorkerId may only contain letters, numbers, - and _ (4-64 chars)." }
foreach ($file in @("worker.py", "run-worker.ps1", "Modelfile")) {
    if (-not (Test-Path (Join-Path $SourceDir $file))) { throw "Missing $file next to install.ps1 (looked in $SourceDir)." }
}

# --- 1. Ollama: running, loopback only, models present -----------------------------------
try {
    $version = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/version" -TimeoutSec 5
    Report "Ollama API on 127.0.0.1:11434" $true ("version " + $version.version)
} catch {
    Report "Ollama API on 127.0.0.1:11434" $false "Start Ollama, then run this installer again."
}

$listeners = @(Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue)
$exposed = @($listeners | Where-Object { $_.LocalAddress -ne "127.0.0.1" -and $_.LocalAddress -ne "::1" })
Report "Ollama listens on loopback only" ($exposed.Count -eq 0) (($listeners | ForEach-Object { $_.LocalAddress }) -join ", ")
foreach ($scope in @("User", "Machine")) {
    $hostSetting = [Environment]::GetEnvironmentVariable("OLLAMA_HOST", $scope)
    if ($hostSetting -and $hostSetting -notmatch '^(http://)?(127\.0\.0\.1|localhost)(:\d+)?$') {
        Report "OLLAMA_HOST ($scope) is not exposed" $false "is '$hostSetting' - set it to 127.0.0.1 or remove it"
    }
}

$installed = @()
try {
    $installed = @((Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 10).models | ForEach-Object { $_.name })
} catch { }
Report "Base model qwen3:8b-q4_K_M installed" ($installed -contains "qwen3:8b-q4_K_M")
Report "Model imperial-diagnostics:latest installed" ($installed -contains "imperial-diagnostics:latest")

# --- 2. Storage ----------------------------------------------------------------------------
$modelSetting = [Environment]::GetEnvironmentVariable("OLLAMA_MODELS", "User")
if (-not $modelSetting) { $modelSetting = [Environment]::GetEnvironmentVariable("OLLAMA_MODELS", "Machine") }
$modelsMatch = ($modelSetting -and ($modelSetting.TrimEnd('\') -ieq $ModelsDir.TrimEnd('\')))
Report "OLLAMA_MODELS points to $ModelsDir" $modelsMatch ("currently '" + $modelSetting + "'")
Report "Model folder exists" (Test-Path $ModelsDir)
$drive = Get-PSDrive -Name ($InstallDir.Substring(0, 1)) -ErrorAction SilentlyContinue
if ($drive) {
    $freeGb = [math]::Round($drive.Free / 1GB, 1)
    Report "Free space on $($drive.Name):" ($freeGb -ge 20) "$freeGb GB free"
}

# --- 3. GPU (informational) ----------------------------------------------------------------
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
    $gpu = (& nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader) -join "; "
    Report "NVIDIA GPU visible" $true $gpu
} else {
    Report "NVIDIA GPU visible" $false "nvidia-smi not found"
}

# --- 4. Python -------------------------------------------------------------------------------
$python = $null
foreach ($candidate in @("py", "python")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    try {
        if ($candidate -eq "py") { $exe = (& py -3 -c "import sys; print(sys.executable)" 2>$null) }
        else { $exe = (& python -c "import sys; print(sys.executable)" 2>$null) }
        if ($exe -and ($exe -notmatch 'WindowsApps') -and (Test-Path $exe)) { $python = $exe.Trim(); break }
    } catch { }
}
if (-not $python) {
    Report "Python 3.9+ installed" $false "Install it with:  winget install -e --id Python.Python.3.12 --scope user   then open a NEW PowerShell window and run this installer again."
    throw "Python is required."
}
$pyVersion = (& $python -c "import sys; print('%d.%d' % sys.version_info[:2])").Trim()
Report "Python 3.9+ installed" ([version]$pyVersion -ge [version]"3.9") "$python ($pyVersion)"

if ($failures -gt 0) {
    Write-Host ""
    Write-Host "$failures check(s) failed above. Fix them and run the installer again." -ForegroundColor Yellow
    if (-not ($installed -contains "imperial-diagnostics:latest")) {
        Write-Host "To (re)build the model:  ollama create imperial-diagnostics -f `"$InstallDir\Modelfile`"" -ForegroundColor Yellow
    }
    exit 1
}

# --- 5. Files --------------------------------------------------------------------------------
$workerDir = Join-Path $InstallDir "worker"
$configDir = Join-Path $InstallDir "config"
$logDir = Join-Path $InstallDir "logs"
foreach ($dir in @($InstallDir, $workerDir, $configDir, $logDir)) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
}
Copy-Item (Join-Path $SourceDir "worker.py") (Join-Path $workerDir "worker.py") -Force
Copy-Item (Join-Path $SourceDir "run-worker.ps1") (Join-Path $InstallDir "run-worker.ps1") -Force
if (Test-Path (Join-Path $SourceDir "uninstall.ps1")) {
    Copy-Item (Join-Path $SourceDir "uninstall.ps1") (Join-Path $InstallDir "uninstall.ps1") -Force
}
Set-Content -Path (Join-Path $configDir "python-path.txt") -Value $python -Encoding ASCII

# Lock the config folder to this Windows user, SYSTEM and Administrators.
$me = "$env:USERDOMAIN\$env:USERNAME"
& icacls $configDir /inheritance:r /grant:r "${me}:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
Report "Config folder restricted to $me" ($LASTEXITCODE -eq 0)

# --- 6. Model rebuild (optional; fixes the P0301 'active' wording for manual ollama use) -----
if ($RebuildModel) {
    $current = Join-Path $InstallDir "Modelfile"
    if (Test-Path $current) {
        $backup = Join-Path $InstallDir ("Modelfile.backup-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
        Copy-Item $current $backup
        Write-Host "Previous Modelfile saved as $backup"
    }
    Copy-Item (Join-Path $SourceDir "Modelfile") $current -Force
    & ollama create imperial-diagnostics -f $current
    Report "Rebuilt imperial-diagnostics from updated Modelfile" ($LASTEXITCODE -eq 0)
}

# --- 7. Worker key (encrypted with DPAPI for this Windows user) -----------------------------
$keyFile = Join-Path $configDir "worker.key"
$replace = $true
if (Test-Path $keyFile) {
    $answer = Read-Host "A worker key is already stored. Replace it? (y/N)"
    $replace = ($answer -match '^[yY]')
}
if ($replace) {
    $secure = Read-Host "Paste LOCAL_NODE_WORKER_KEY from Railway QA (input hidden)" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $length = ([Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)).Length }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    if ($length -lt 32) { throw "That key is too short (needs 32+ characters)." }
    $secure | ConvertFrom-SecureString | Set-Content -Path $keyFile -Encoding ASCII
    Report "Worker key stored encrypted (DPAPI, user $me)" (Test-Path $keyFile)
}

# --- 8. Config ------------------------------------------------------------------------------
$config = [ordered]@{
    backendUrl = $BackendUrl
    workerId = $WorkerId
    ollamaUrl = "http://127.0.0.1:11434"
    pollSeconds = 10
    heartbeatSeconds = 60
    requestTimeoutSeconds = 30
    ollamaTimeoutSeconds = 600
    logDir = $logDir
}
$config | ConvertTo-Json | Set-Content -Path (Join-Path $configDir "config.json") -Encoding UTF8
Report "Config written" $true (Join-Path $configDir "config.json")

# --- 9. Commissioning check ----------------------------------------------------------------
Write-Host ""
Write-Host "Running commissioning check..." -ForegroundColor Cyan
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $InstallDir "run-worker.ps1") -Mode check
$checkOk = ($LASTEXITCODE -eq 0)
Report "Commissioning check" $checkOk

# --- 10. Scheduled task --------------------------------------------------------------------
if (-not $SkipTask) {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$InstallDir\run-worker.ps1`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $me
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName "Imperial Local Node" -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description "Imperial Operations local AI worker (outbound HTTPS only)" -Force | Out-Null
    Report "Scheduled Task 'Imperial Local Node' registered (starts at logon, restarts on failure)" $true
    if ($checkOk) {
        Start-ScheduledTask -TaskName "Imperial Local Node"
        Write-Host "Worker started. Log: $logDir\worker.log"
    } else {
        Write-Host "Worker NOT started because the commissioning check failed. Fix it, then: Start-ScheduledTask -TaskName 'Imperial Local Node'" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Next: run the model guardrail test:" -ForegroundColor Cyan
Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$InstallDir\run-worker.ps1`" -Mode eval"
if (-not $checkOk) { exit 1 }
