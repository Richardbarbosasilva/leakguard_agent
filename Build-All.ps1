<#
.SYNOPSIS
    Compila o agent a partir do source, atualiza o bundle e gera o instalador Inno Setup.
#>

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "============================================" -ForegroundColor Cyan
Write-Host " Leakguard Agent - Build Completo"           -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

# ---------------------------------------------------------------------------
# 1. Compilar o agent com PyInstaller
# ---------------------------------------------------------------------------
Write-Host "`n[1/3] Compilando ScreenshotAuditAgent.exe..." -ForegroundColor Yellow

$bundleDir  = Join-Path $root "bundle"
$sourceFile = Join-Path $bundleDir "source\mock_watermark.py"
$reqsFile   = Join-Path $bundleDir "requirements.txt"
$buildDir   = Join-Path $root "_build"

if (-not (Test-Path $sourceFile)) {
    throw "Source nao encontrado: $sourceFile"
}

# Garante que PyInstaller esta instalado
$pip = (Get-Command pip -ErrorAction SilentlyContinue)
if (-not $pip) { throw "pip nao encontrado no PATH. Instale Python 3.10+." }

pip install pyinstaller --quiet --upgrade
if (Test-Path $reqsFile) {
    pip install -r $reqsFile --quiet
}

# Limpa build anterior
if (Test-Path $buildDir) { Remove-Item $buildDir -Recurse -Force }

pyinstaller --noconfirm --clean --onefile `
    --name ScreenshotAuditAgent `
    --distpath "$bundleDir" `
    --workpath "$buildDir\work" `
    --specpath "$buildDir\spec" `
    $sourceFile

if (-not (Test-Path "$bundleDir\ScreenshotAuditAgent.exe")) {
    throw "Falha ao compilar o agent."
}

$exeSize = [math]::Round((Get-Item "$bundleDir\ScreenshotAuditAgent.exe").Length / 1MB, 2)
Write-Host "   OK: ScreenshotAuditAgent.exe ($exeSize MB)" -ForegroundColor Green

# ---------------------------------------------------------------------------
# 2. Compilar o instalador Inno Setup
# ---------------------------------------------------------------------------
Write-Host "`n[2/3] Compilando instalador Inno Setup..." -ForegroundColor Yellow

$issFile = Join-Path $root "inno-setup\LeakguardAgent.iss"
$iscc = $null
foreach ($loc in @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
)) {
    if (Test-Path $loc) { $iscc = $loc; break }
}
if (-not $iscc) { throw "Inno Setup (ISCC.exe) nao encontrado." }

& $iscc $issFile
if ($LASTEXITCODE -ne 0) { throw "ISCC retornou erro." }

$outputDir = Join-Path $root "output"
$exeSetup = Get-ChildItem $outputDir -Filter "LeakguardAgent-Setup-*.exe" | Sort-Object LastWriteTime -Descending | Select-Object -First 1

Write-Host "   OK: $($exeSetup.Name) ($([math]::Round($exeSetup.Length / 1MB, 2)) MB)" -ForegroundColor Green

# ---------------------------------------------------------------------------
# 3. Limpeza
# ---------------------------------------------------------------------------
Write-Host "`n[3/3] Limpando arquivos temporarios..." -ForegroundColor Yellow
if (Test-Path $buildDir) { Remove-Item $buildDir -Recurse -Force }
Write-Host "   OK: Build limpo." -ForegroundColor Green

# ---------------------------------------------------------------------------
Write-Host "`n============================================" -ForegroundColor Green
Write-Host " Build concluido!"                              -ForegroundColor Green
Write-Host " Instalador: $($exeSetup.FullName)"            -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
