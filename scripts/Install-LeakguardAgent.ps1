#Requires -RunAsAdministrator
param(
    [Parameter(Mandatory=$true)]
    [string]$BearerToken,

    [string]$AgentId = $env:COMPUTERNAME,

    [Parameter(Mandatory=$true)]
    [string]$ApiBaseUrl,

    [int]$ApiPort = 8010,

    [string]$ServerIp = "",

    [string]$HostMappings = "",

    [string]$StorageHost = "",
    [string]$StorageIp   = "",
    [string]$ConsoleHost  = "",
    [string]$ConsoleIp    = "",
    [string]$ApiHost      = "",
    [string]$ApiIp        = "",

    [string]$SourceDir        = "",
    [string]$RequirementsFile = "",
    [string]$NssmPath         = "",
    [string]$PrebuiltExe      = "",
    [string]$ConfigTemplate   = "",
    [string]$LogoFile         = "",

    [string]$InstallDir      = "$env:ProgramFiles\LeakguardAgent",
    [string]$DataDir         = "$env:ProgramData\LeakguardAgent",
    [string]$ServiceName     = "LeakguardAgent"
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function Write-Step  { param([string]$msg) Write-Host "[*] $msg" -ForegroundColor Cyan }
function Write-Ok    { param([string]$msg) Write-Host "[+] $msg" -ForegroundColor Green }
function Write-Warn  { param([string]$msg) Write-Host "[!] $msg" -ForegroundColor Yellow }

function Resolve-BundledPath {
    param([string]$Explicit, [string]$BundleName)
    if ($Explicit -and (Test-Path $Explicit)) { return $Explicit }
    $bundled = Join-Path $PSScriptRoot "..\bundle\$BundleName"
    if (Test-Path $bundled) { return (Resolve-Path $bundled).Path }
    return $null
}

# ---------------------------------------------------------------------------
# 1. Pre-requisitos: VC++ Redistributable
# ---------------------------------------------------------------------------
Write-Step "Verificando pre-requisitos..."

$vcInstalled = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64" -ErrorAction SilentlyContinue
if (-not $vcInstalled) {
    Write-Step "Instalando Visual C++ Redistributable..."
    $vcDest = "$env:TEMP\vc_redist.x64.exe"
    $vcUrl  = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $wc = New-Object System.Net.WebClient
        $wc.DownloadFile($vcUrl, $vcDest)
        $proc = Start-Process -FilePath $vcDest -ArgumentList "/install","/quiet","/norestart" -Wait -PassThru -NoNewWindow
        if ($proc.ExitCode -eq 0 -or $proc.ExitCode -eq 3010) {
            Write-Ok "VC++ Redistributable instalado"
        } else {
            Write-Warn "VC++ retornou codigo $($proc.ExitCode) - o agent pode falhar"
        }
        Remove-Item $vcDest -Force -ErrorAction SilentlyContinue
    } catch {
        Write-Warn "Falha ao instalar VC++ automaticamente: $_"
        Write-Warn "Instale manualmente: https://aka.ms/vs/17/release/vc_redist.x64.exe"
    }
} else {
    Write-Ok "VC++ Redistributable ja instalado: $($vcInstalled.Version)"
}

# ---------------------------------------------------------------------------
# 2. Resolve paths (explicit param > bundle/ defaults)
# ---------------------------------------------------------------------------
Write-Step "Resolvendo caminhos..."

$resolvedNssm       = Resolve-BundledPath $NssmPath        "nssm.exe"
$resolvedSource     = Resolve-BundledPath $SourceDir        "source"
$resolvedReqs       = Resolve-BundledPath $RequirementsFile "requirements.txt"
$resolvedConfig     = Resolve-BundledPath $ConfigTemplate   "config-template.json"
$resolvedLogo       = Resolve-BundledPath $LogoFile         "logo.png"
$resolvedPrebuilt   = Resolve-BundledPath $PrebuiltExe      "ScreenshotAuditAgent.exe"

if (-not $resolvedNssm)   { throw "NSSM nao encontrado. Forneça -NssmPath ou coloque em bundle/nssm.exe" }
if (-not $resolvedConfig) { throw "Config template nao encontrado. Forneça -ConfigTemplate ou coloque em bundle/config-template.json" }

$usePrebuilt = $false
if ($resolvedPrebuilt) {
    $usePrebuilt = $true
    Write-Ok "Binario pre-compilado encontrado: $resolvedPrebuilt"
} elseif ($resolvedSource) {
    Write-Step "Usando source Python (compilacao no alvo)"
} else {
    throw "Nenhum binario ou source encontrado. Forneça -PrebuiltExe, -SourceDir, ou popule bundle/"
}

# ---------------------------------------------------------------------------
# 3. Create directory structure
# ---------------------------------------------------------------------------
Write-Step "Criando diretorios..."

$dirs = @($InstallDir, $DataDir, "$DataDir\logs", "$DataDir\data",
          "$DataDir\spool", "$DataDir\spool\Screenshots", "$DataDir\tmp", "$DataDir\assets")
foreach ($d in $dirs) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
Write-Ok "Diretorios criados"

# ---------------------------------------------------------------------------
# 4. Copy NSSM
# ---------------------------------------------------------------------------
Write-Step "Verificando NSSM..."
$nssmDest = "$InstallDir\nssm.exe"
if ($resolvedNssm -ne $nssmDest) {
    Copy-Item -Path $resolvedNssm -Destination $nssmDest -Force
    Write-Ok "NSSM copiado para $nssmDest"
} else {
    Write-Ok "NSSM ja esta no destino: $nssmDest"
}

# ---------------------------------------------------------------------------
# 5. Install/compile agent binary
# ---------------------------------------------------------------------------
$agentExe = "$InstallDir\ScreenshotAuditAgent.exe"

if ($usePrebuilt) {
    Write-Step "Verificando binario pre-compilado..."
    if ($resolvedPrebuilt -ne $agentExe) {
        Copy-Item -Path $resolvedPrebuilt -Destination $agentExe -Force
        Write-Ok "Binario copiado de $resolvedPrebuilt"
    } else {
        Write-Ok "Binario ja esta no destino: $agentExe"
    }
} else {
    Write-Step "Compilando agent com PyInstaller..."

    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) { throw "Python nao encontrado no PATH. Instale Python 3.10+ ou use binario pre-compilado." }

    $venvDir = "$InstallDir\.venv"
    & python -m venv $venvDir
    & "$venvDir\Scripts\pip.exe" install --quiet --upgrade pip

    if ($resolvedReqs) {
        & "$venvDir\Scripts\pip.exe" install --quiet -r $resolvedReqs
    }
    & "$venvDir\Scripts\pip.exe" install --quiet pyinstaller

    $mainPy = Get-ChildItem -Path $resolvedSource -Filter "*.py" -Recurse |
              Where-Object { $_.Name -match "mock_watermark|main|agent" } |
              Select-Object -First 1

    if (-not $mainPy) { throw "Nenhum arquivo Python principal encontrado em $resolvedSource" }

    & "$venvDir\Scripts\pyinstaller.exe" --onefile --name ScreenshotAuditAgent `
        --distpath $InstallDir $mainPy.FullName

    if (-not (Test-Path $agentExe)) { throw "Falha ao compilar o agent" }
    Write-Ok "Agent compilado: $agentExe"
}

# ---------------------------------------------------------------------------
# 6. Copy logo
# ---------------------------------------------------------------------------
if ($resolvedLogo) {
    Write-Step "Copiando logo..."
    Copy-Item -Path $resolvedLogo -Destination "$DataDir\assets\logo.png" -Force
    Write-Ok "Logo copiada"
}

# ---------------------------------------------------------------------------
# 7. Generate config from template
# ---------------------------------------------------------------------------
Write-Step "Gerando configuracao..."

$configJson = Get-Content -Raw $resolvedConfig | ConvertFrom-Json

$configJson.api.bearer_token = $BearerToken
$configJson.api.agent_id     = $AgentId

# Se ServerIp foi fornecido, manter os hostnames do template (resolvidos via hosts file)
if ($ServerIp) {
    $tplApiUri = $null
    try { $tplApiUri = [System.Uri]$configJson.api.base_url } catch {}
    if ($tplApiUri) {
        $configJson.api.base_url = "http://$($tplApiUri.Host):$ApiPort"
    }
} else {
    $testUri = $null
    try { $testUri = [System.Uri]$ApiBaseUrl } catch {}
    if ($testUri -and $testUri.Port -gt 0 -and $testUri.Port -ne 80) {
        $configJson.api.base_url = $ApiBaseUrl
    } else {
        $configJson.api.base_url = "${ApiBaseUrl}:${ApiPort}"
    }
}

$configJson.paths.spool_dir = "$DataDir\spool"
$configJson.paths.tmp_dir   = "$DataDir\tmp"
$configJson.paths.db_path   = "$DataDir\data\queue.db"
$configJson.paths.log_path  = "$DataDir\logs\agent.log"
$configJson.watermark.logo_path = "$DataDir\assets\logo.png"

$configPath = "$DataDir\config.json"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$jsonContent = $configJson | ConvertTo-Json -Depth 10
[System.IO.File]::WriteAllText($configPath, $jsonContent, $utf8NoBom)
Write-Ok "Config salva em $configPath (UTF-8 sem BOM)"

# ---------------------------------------------------------------------------
# 8. Hosts file entries
# ---------------------------------------------------------------------------
Write-Step "Configurando resolucao de nomes..."
$hostsFile = "$env:SystemRoot\System32\drivers\etc\hosts"
$currentHosts = Get-Content $hostsFile -ErrorAction SilentlyContinue
$hostsEntries = @()

# Ler hostnames do config template (fonte verdadeira dos hostnames)
$tplJson = Get-Content -Raw $resolvedConfig | ConvertFrom-Json
$tplApiHost = $null
$tplS3Host  = $null
try {
    $u = [System.Uri]$tplJson.api.base_url
    if ($u.Host -notmatch '^\d+\.\d+\.\d+\.\d+$') { $tplApiHost = $u.Host }
} catch {}
try {
    $u = [System.Uri]$tplJson.minio.endpoint_url
    if ($u.Host -notmatch '^\d+\.\d+\.\d+\.\d+$') { $tplS3Host = $u.Host }
} catch {}

if ($ServerIp) {
    # Modo ServerIp: mapear todos os hostnames do template para o IP fornecido
    Write-Ok "Modo ServerIp: hostnames apontados para $ServerIp"
    if ($tplApiHost) { $hostsEntries += "$ServerIp`t$tplApiHost" }
    if ($tplS3Host)  { $hostsEntries += "$ServerIp`t$tplS3Host" }
} else {
    # Modo manual: HostMappings ou auto-detect via DNS
    if ($HostMappings) {
        foreach ($pair in ($HostMappings -split ',')) {
            $parts = $pair.Trim() -split '='
            if ($parts.Count -eq 2) {
                $hostsEntries += "$($parts[1].Trim())`t$($parts[0].Trim())"
            }
        }
    }

    if ($StorageHost -and $StorageIp) { $hostsEntries += "$StorageIp`t$StorageHost" }
    if ($ConsoleHost -and $ConsoleIp) { $hostsEntries += "$ConsoleIp`t$ConsoleHost" }
    if ($ApiHost -and $ApiIp)         { $hostsEntries += "$ApiIp`t$ApiHost" }

    # Auto-detect via DNS
    foreach ($h in @($tplApiHost, $tplS3Host)) {
        if (-not $h) { continue }
        $alreadyMapped = $hostsEntries | Where-Object { $_ -match [regex]::Escape($h) }
        $alreadyInFile = $currentHosts | Where-Object { $_ -match [regex]::Escape($h) }
        if ($alreadyMapped -or $alreadyInFile) { continue }
        $resolved = $null
        try { $resolved = [System.Net.Dns]::GetHostAddresses($h) | Select-Object -First 1 } catch {}
        if ($resolved) {
            $hostsEntries += "$($resolved.IPAddressToString)`t$h"
            Write-Ok "  Auto-detectado: $h -> $($resolved.IPAddressToString)"
        } else {
            Write-Warn "  Nao foi possivel resolver '$h'. Use -ServerIp para mapear automaticamente."
        }
    }
}

# Aplicar entradas
if ($hostsEntries.Count -gt 0) {
    foreach ($entry in $hostsEntries) {
        $entryHost = ($entry -split "`t")[1]
        $entryIp   = ($entry -split "`t")[0]
        $existingLine = $currentHosts | Where-Object { $_ -match "^\s*[\d\.]+\s+$([regex]::Escape($entryHost))" }
        if ($existingLine) {
            $existingIp = ($existingLine.Trim() -split '\s+')[0]
            if ($existingIp -ne $entryIp) {
                $newHosts = $currentHosts -replace [regex]::Escape($existingLine), $entry
                $utf8NoBom2 = New-Object System.Text.UTF8Encoding($false)
                [System.IO.File]::WriteAllLines($hostsFile, $newHosts, $utf8NoBom2)
                $currentHosts = $newHosts
                Write-Ok "  Hosts atualizado: $entryHost -> $entryIp (era $existingIp)"
            } else {
                Write-Ok "  $entryHost ja aponta para $entryIp, pulando"
            }
        } else {
            Add-Content -Path $hostsFile -Value $entry
            Write-Ok "  Hosts adicionado: $entry"
        }
    }
} else {
    Write-Ok "Nenhum mapeamento necessario"
}

# ---------------------------------------------------------------------------
# 9. Firewall: WinRM + Agent
# ---------------------------------------------------------------------------
Write-Step "Configurando firewall..."

$rules = @(
    @{ Name = "WinRM-HTTP-Leakguard";  Port = 5985; Desc = "WinRM HTTP (Leakguard)" },
    @{ Name = "LeakguardAgent-ICMP";    Port = $null; Desc = "ICMP Echo (Leakguard)" }
)

# WinRM inbound
$existingWinRM = Get-NetFirewallRule -Name "WinRM-HTTP-Leakguard" -ErrorAction SilentlyContinue
if (-not $existingWinRM) {
    New-NetFirewallRule -Name "WinRM-HTTP-Leakguard" -DisplayName "WinRM HTTP (Leakguard)" `
        -Direction Inbound -Protocol TCP -LocalPort 5985 -Action Allow `
        -Profile Domain,Private -Enabled True | Out-Null
    Write-Ok "Firewall: regra WinRM criada (TCP 5985, Domain+Private)"
} else {
    Write-Ok "Firewall: regra WinRM ja existe"
}

# Enable built-in WinRM rules
Enable-NetFirewallRule -Name "WINRM-HTTP-In-TCP" -ErrorAction SilentlyContinue
Enable-NetFirewallRule -Name "WINRM-HTTP-In-TCP-PUBLIC" -ErrorAction SilentlyContinue

# ICMP (ping) inbound
$existingICMP = Get-NetFirewallRule -Name "LeakguardAgent-ICMP" -ErrorAction SilentlyContinue
if (-not $existingICMP) {
    New-NetFirewallRule -Name "LeakguardAgent-ICMP" -DisplayName "ICMP Echo (Leakguard)" `
        -Direction Inbound -Protocol ICMPv4 -IcmpType 8 -Action Allow `
        -Profile Domain,Private -Enabled True | Out-Null
    Write-Ok "Firewall: regra ICMP criada (Domain+Private)"
} else {
    Write-Ok "Firewall: regra ICMP ja existe"
}

# Ensure WinRM service is running and auto-start
$winrmSvc = Get-Service WinRM -ErrorAction SilentlyContinue
if ($winrmSvc) {
    if ($winrmSvc.StartType -ne 'Automatic') {
        Set-Service WinRM -StartupType Automatic
    }
    if ($winrmSvc.Status -ne 'Running') {
        Start-Service WinRM
    }
    Write-Ok "WinRM: servico ativo e automatico"
}

# ---------------------------------------------------------------------------
# 10. Register Windows service via NSSM
# ---------------------------------------------------------------------------
Write-Step "Registrando servico '$ServiceName'..."

$nssmBin = "$InstallDir\nssm.exe"

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Warn "Servico existente encontrado, removendo..."
    $null = & $nssmBin stop $ServiceName 2>&1
    $null = & $nssmBin remove $ServiceName confirm 2>&1
    Start-Sleep -Seconds 2
}

$null = & $nssmBin install $ServiceName $agentExe 2>&1
$null = & $nssmBin set $ServiceName AppDirectory $InstallDir 2>&1
$null = & $nssmBin set $ServiceName AppParameters "--config `"$configPath`"" 2>&1
$null = & $nssmBin set $ServiceName Start SERVICE_AUTO_START 2>&1
$null = & $nssmBin set $ServiceName AppStdout "$DataDir\logs\service-stdout.log" 2>&1
$null = & $nssmBin set $ServiceName AppStderr "$DataDir\logs\service-stderr.log" 2>&1
$null = & $nssmBin set $ServiceName AppRotateFiles 1 2>&1
$null = & $nssmBin set $ServiceName AppRotateBytes 5242880 2>&1
$null = & $nssmBin set $ServiceName Description "Leakguard Screenshot Audit Agent" 2>&1

Start-Service $ServiceName
Write-Ok "Servico '$ServiceName' iniciado"

# ---------------------------------------------------------------------------
# 11. Configure ShareX
# ---------------------------------------------------------------------------
Write-Step "Configurando ShareX..."

$spoolDir = "$DataDir\spool"

# Registry: redirect PersonalPath + disable built-in upload/update
$regPath = "HKLM:\SOFTWARE\ShareX"
if (-not (Test-Path $regPath)) { New-Item -Path $regPath -Force | Out-Null }
Set-ItemProperty -Path $regPath -Name "PersonalPath"       -Value $spoolDir -Type String
Set-ItemProperty -Path $regPath -Name "DisableUpload"      -Value 1         -Type DWord
Set-ItemProperty -Path $regPath -Name "DisableUpdateCheck"  -Value 1         -Type DWord
Write-Ok "Registry ShareX: PersonalPath=$spoolDir, Upload=off, Update=off"

# Autostart shortcut
$sharexExe = @(
    "$env:ProgramFiles\ShareX\ShareX.exe",
    "${env:ProgramFiles(x86)}\ShareX\ShareX.exe",
    "$env:LOCALAPPDATA\Programs\ShareX\ShareX.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($sharexExe) {
    $startupDir = "$env:ProgramData\Microsoft\Windows\Start Menu\Programs\Startup"
    New-Item -ItemType Directory -Force -Path $startupDir | Out-Null
    $wsh = New-Object -ComObject WScript.Shell
    $lnk = $wsh.CreateShortcut("$startupDir\ShareX.lnk")
    $lnk.TargetPath       = $sharexExe
    $lnk.WorkingDirectory  = Split-Path $sharexExe -Parent
    $lnk.IconLocation      = "$sharexExe,0"
    $lnk.Save()
    Write-Ok "ShareX autostart: $startupDir\ShareX.lnk"

    # Remove Lightshot entries
    $lightshotNames = @('Lightshot', 'LightShot')
    $runKeys = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run'
    )
    foreach ($rk in $runKeys) {
        foreach ($name in $lightshotNames) {
            Remove-ItemProperty -Path $rk -Name $name -ErrorAction SilentlyContinue
        }
    }
    $lightshotLnk = "$startupDir\Lightshot.lnk"
    if (Test-Path $lightshotLnk) { Remove-Item $lightshotLnk -Force }

    # Restart ShareX to pick up new PersonalPath
    $proc = Get-Process -Name ShareX -ErrorAction SilentlyContinue
    if ($proc) {
        $proc | Stop-Process -Force
        Start-Sleep -Seconds 1
        Start-Process -FilePath $sharexExe
        Write-Ok "ShareX reiniciado com novo PersonalPath"
    } else {
        Write-Ok "ShareX nao estava rodando. Sera aplicado no proximo login."
    }
} else {
    Write-Warn "ShareX nao encontrado. Instale o ShareX e reinicie o computador."
}

# ---------------------------------------------------------------------------
# 12. Summary
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  Leakguard Agent instalado com sucesso!" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host "  Servico  : $ServiceName"
Write-Host "  Binario  : $agentExe"
Write-Host "  Config   : $configPath"
Write-Host "  Spool    : $DataDir\spool\"
Write-Host "  Logs     : $DataDir\logs\"
Write-Host "  Agent ID : $AgentId"
Write-Host "  WinRM    : Habilitado (TCP 5985)"
Write-Host "  ShareX   : PersonalPath -> $DataDir\spool\"
Write-Host ""

exit 0
