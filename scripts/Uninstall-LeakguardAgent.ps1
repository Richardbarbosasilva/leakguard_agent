#Requires -RunAsAdministrator
param(
    [string]$ServiceName = "LeakguardAgent",
    [string]$InstallDir  = "$env:ProgramFiles\LeakguardAgent",
    [string]$DataDir     = "$env:ProgramData\LeakguardAgent",
    [switch]$KeepData,
    [switch]$RemoveHostEntries
)

$ErrorActionPreference = "Stop"

function Write-Step  { param([string]$msg) Write-Host "[*] $msg" -ForegroundColor Cyan }
function Write-Ok    { param([string]$msg) Write-Host "[+] $msg" -ForegroundColor Green }
function Write-Warn  { param([string]$msg) Write-Host "[!] $msg" -ForegroundColor Yellow }

# ---------------------------------------------------------------------------
# 1. Stop and remove service
# ---------------------------------------------------------------------------
$nssmBin = "$InstallDir\nssm.exe"
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue

if ($svc) {
    Write-Step "Parando servico '$ServiceName'..."
    if ($svc.Status -eq "Running") {
        Stop-Service $ServiceName -Force
        Start-Sleep -Seconds 2
    }

    if (Test-Path $nssmBin) {
        Write-Step "Removendo servico via NSSM..."
        & $nssmBin remove $ServiceName confirm 2>$null
    } else {
        Write-Step "Removendo servico via sc.exe..."
        & sc.exe delete $ServiceName 2>$null
    }
    Write-Ok "Servico removido"
} else {
    Write-Warn "Servico '$ServiceName' nao encontrado"
}

# ---------------------------------------------------------------------------
# 2. Remove install directory
# ---------------------------------------------------------------------------
if (Test-Path $InstallDir) {
    Write-Step "Removendo diretorio de instalacao: $InstallDir"
    Remove-Item -Path $InstallDir -Recurse -Force
    Write-Ok "Diretorio removido"
}

# ---------------------------------------------------------------------------
# 3. Remove data directory (unless -KeepData)
# ---------------------------------------------------------------------------
if (-not $KeepData) {
    if (Test-Path $DataDir) {
        Write-Step "Removendo dados: $DataDir"
        Remove-Item -Path $DataDir -Recurse -Force
        Write-Ok "Dados removidos"
    }
} else {
    Write-Warn "Dados preservados em $DataDir (flag -KeepData)"
}

# ---------------------------------------------------------------------------
# 4. Remove hosts entries (if -RemoveHostEntries)
# ---------------------------------------------------------------------------
if ($RemoveHostEntries) {
    Write-Step "Limpando entradas do hosts..."
    $hostsFile = "$env:SystemRoot\System32\drivers\etc\hosts"
    $markers = @("homelab.local", "leakguard")
    $lines = Get-Content $hostsFile
    $cleaned = $lines | Where-Object {
        $line = $_
        -not ($markers | Where-Object { $line -match $_ })
    }
    $cleaned | Set-Content $hostsFile -Encoding ASCII
    Write-Ok "Entradas do hosts removidas"
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  Leakguard Agent desinstalado" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
if ($KeepData) {
    Write-Host "  Dados preservados em: $DataDir"
}
Write-Host ""
