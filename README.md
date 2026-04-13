# Leakguard Agent - Windows Installer

Instalador standalone para o Leakguard Agent, substituindo o deploy via Ansible
por pacotes EXE (Inno Setup) e MSI (WiX Toolset) para instalacoes Windows.

## Estrutura

```
leakguard-installer/
├── Build-Installers.ps1              # Script principal de build
├── assets/                           # Assets do wizard (logo)
├── bundle/                           # Arquivos empacotados no instalador
│   ├── config-template.json          # Template de config do agent
│   ├── requirements.txt              # Dependencias Python
│   ├── nssm.exe                      # NSSM service wrapper
│   ├── logo.png                      # Logo Leakguard
│   ├── ScreenshotAuditAgent.exe      # Binario pre-compilado (34 MB)
│   └── source/
│       └── mock_watermark.py         # Source Python do agent
├── scripts/
│   ├── Install-LeakguardAgent.ps1    # Logica core de instalacao
│   └── Uninstall-LeakguardAgent.ps1  # Logica de desinstalacao
├── inno-setup/
│   └── LeakguardAgent.iss            # Script Inno Setup -> .exe
├── wix/
│   └── LeakguardAgent.wxs            # Source WiX -> .msi
└── output/                           # Instaladores gerados
```

## Pre-requisitos para Build

| Ferramenta   | Versao | Como instalar                              |
|-------------|--------|--------------------------------------------|
| Inno Setup  | 6.x    | https://jrsoftware.org/isinfo.php          |
| WiX Toolset | 4.x    | `dotnet tool install --global wix`         |
| PowerShell  | 5.1+   | Incluso no Windows 10/11                   |

## Gerando os Instaladores

```powershell
.\Build-Installers.ps1 -Version "1.0.0"          # Ambos
.\Build-Installers.ps1 -Version "1.0.0" -SkipMsi # Apenas EXE
.\Build-Installers.ps1 -Version "1.0.0" -SkipExe # Apenas MSI
```

## Instalacao

### EXE (wizard interativo)
Duplo-clique no `.exe`. Pede: Bearer Token, Agent ID, API URL, hosts.

### MSI (silencioso / GPO / SCCM / Intune)
```powershell
msiexec /i LeakguardAgent-Setup-1.0.0.msi /qn `
    BEARER_TOKEN="tok_xxx" `
    AGENT_ID="PC-VENDAS-01" `
    API_BASE_URL="http://leakguard-api.homelab.local"
```

### PowerShell (direto)
```powershell
.\scripts\Install-LeakguardAgent.ps1 `
    -BearerToken "tok_xxx" `
    -AgentId "PC-01" `
    -ApiBaseUrl "http://leakguard-api.homelab.local"
```

## Desinstalacao

- **EXE**: Painel de Controle > Programas > Desinstalar
- **MSI**: `msiexec /x LeakguardAgent-Setup-1.0.0.msi /qn`
- **PowerShell**: `.\scripts\Uninstall-LeakguardAgent.ps1 -RemoveHostEntries`
