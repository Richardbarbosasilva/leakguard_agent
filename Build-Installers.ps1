param(
    [Parameter(Mandatory=$true)]
    [string]$Version,

    [switch]$SkipExe,
    [switch]$SkipMsi,

    [string]$InnoSetupPath = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    [string]$OutputDir     = "$PSScriptRoot\output"
)

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Leakguard Agent - Build Installers" -ForegroundColor Cyan
Write-Host "  Version: $Version" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Validate bundle contents
$bundleDir = "$PSScriptRoot\bundle"
$requiredFiles = @("nssm.exe", "config-template.json", "requirements.txt")

foreach ($f in $requiredFiles) {
    $path = Join-Path $bundleDir $f
    if (-not (Test-Path $path)) {
        Write-Host "[!] Arquivo obrigatorio ausente: bundle\$f" -ForegroundColor Red
        exit 1
    }
}

$hasExe    = Test-Path "$bundleDir\ScreenshotAuditAgent.exe"
$hasSource = (Get-ChildItem "$bundleDir\source\*.py" -ErrorAction SilentlyContinue).Count -gt 0

if (-not $hasExe -and -not $hasSource) {
    Write-Host "[!] Nenhum binario ou source encontrado em bundle/" -ForegroundColor Red
    Write-Host "    Coloque ScreenshotAuditAgent.exe ou arquivos .py em bundle/source/" -ForegroundColor Red
    exit 1
}

# Create output directory
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

# ---------------------------------------------------------------------------
# Build EXE (Inno Setup)
# ---------------------------------------------------------------------------
if (-not $SkipExe) {
    Write-Host "[*] Gerando instalador EXE (Inno Setup)..." -ForegroundColor Cyan

    if (-not (Test-Path $InnoSetupPath)) {
        Write-Host "[!] Inno Setup nao encontrado em: $InnoSetupPath" -ForegroundColor Red
        Write-Host "    Instale em https://jrsoftware.org/isinfo.php ou use -SkipExe" -ForegroundColor Yellow
    } else {
        $issFile = "$PSScriptRoot\inno-setup\LeakguardAgent.iss"
        & $InnoSetupPath /DAppVersion=$Version /DOutputDir=$OutputDir $issFile

        if ($LASTEXITCODE -eq 0) {
            Write-Host "[+] EXE gerado: $OutputDir\LeakguardAgent-Setup-$Version.exe" -ForegroundColor Green
        } else {
            Write-Host "[!] Falha ao gerar EXE (exit code: $LASTEXITCODE)" -ForegroundColor Red
        }
    }
}

# ---------------------------------------------------------------------------
# Build MSI (WiX Toolset 4)
# ---------------------------------------------------------------------------
if (-not $SkipMsi) {
    Write-Host "[*] Gerando instalador MSI (WiX Toolset)..." -ForegroundColor Cyan

    $wix = Get-Command wix -ErrorAction SilentlyContinue
    if (-not $wix) {
        Write-Host "[!] WiX Toolset nao encontrado." -ForegroundColor Red
        Write-Host "    Instale com: dotnet tool install --global wix" -ForegroundColor Yellow
        Write-Host "    Ou use -SkipMsi" -ForegroundColor Yellow
    } else {
        $wxsFile = "$PSScriptRoot\wix\LeakguardAgent.wxs"
        $msiOutput = "$OutputDir\LeakguardAgent-Setup-$Version.msi"

        wix build $wxsFile `
            -d "ProductVersion=$Version" `
            -d "BundleDir=$bundleDir" `
            -d "ScriptsDir=$PSScriptRoot\scripts" `
            -o $msiOutput

        if ($LASTEXITCODE -eq 0) {
            Write-Host "[+] MSI gerado: $msiOutput" -ForegroundColor Green
        } else {
            Write-Host "[!] Falha ao gerar MSI (exit code: $LASTEXITCODE)" -ForegroundColor Red
        }
    }
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  Build concluido!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Get-ChildItem $OutputDir -Filter "LeakguardAgent-*$Version*" | ForEach-Object {
    Write-Host "  $($_.Name)  ($([math]::Round($_.Length / 1MB, 1)) MB)"
}
Write-Host ""
