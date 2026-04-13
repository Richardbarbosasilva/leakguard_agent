#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef OutputDir
  #define OutputDir "..\output"
#endif

#define AppName      "Leakguard Agent"
#define AppPublisher "IMAIS - DTI"
#define AppExeName   "ScreenshotAuditAgent.exe"
#define BundleDir    "..\bundle"
#define ScriptsDir   "..\scripts"

[Setup]
AppId={{B8A3F2D1-7E4C-4A9B-8F6D-2C1E5A3B7D9F}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\LeakguardAgent
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=LeakguardAgent-Setup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
PrivilegesRequired=admin
SetupIconFile=..\assets\logo.ico
UninstallDisplayIcon={app}\logo.ico
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
WizardImageFile=..\assets\logo_wizard.bmp
WizardSmallImageFile=..\assets\logo_small.bmp

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#BundleDir}\nssm.exe";                    DestDir: "{app}"; Flags: ignoreversion
Source: "{#BundleDir}\config-template.json";         DestDir: "{app}"; Flags: ignoreversion
Source: "{#BundleDir}\requirements.txt";             DestDir: "{app}"; Flags: ignoreversion
Source: "{#BundleDir}\logo.png";                     DestDir: "{app}"; Flags: ignoreversion
Source: "{#BundleDir}\logo.ico";                     DestDir: "{app}"; Flags: ignoreversion
Source: "{#BundleDir}\ScreenshotAuditAgent.exe";     DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#BundleDir}\source\*";                     DestDir: "{app}\source"; Flags: ignoreversion recursesubdirs skipifsourcedoesntexist
Source: "{#ScriptsDir}\Install-LeakguardAgent.ps1";  DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "{#ScriptsDir}\Uninstall-LeakguardAgent.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion

[Code]
var
  BearerTokenPage: TInputQueryWizardPage;
  AgentIdPage: TInputQueryWizardPage;
  ServerPage: TInputQueryWizardPage;

procedure InitializeWizard;
begin
  BearerTokenPage := CreateInputQueryPage(wpSelectDir,
    'Token de Autenticacao',
    'Informe o Bearer Token para a API do Leakguard',
    'Este token e fornecido pelo administrador do sistema.');
  BearerTokenPage.Add('Bearer Token:', False);

  AgentIdPage := CreateInputQueryPage(BearerTokenPage.ID,
    'Identificacao do Agent',
    'Informe o ID deste agent',
    'O padrao e o nome do computador. Pode ser alterado conforme necessidade.');
  AgentIdPage.Add('Agent ID:', False);
  AgentIdPage.Values[0] := GetComputerNameString;

  ServerPage := CreateInputQueryPage(AgentIdPage.ID,
    'Servidor Leakguard',
    'Informe o IP e a porta do servidor Leakguard',
    'O instalador resolve automaticamente os hostnames da API e do S3 para este IP.');
  ServerPage.Add('IP do Servidor (ex: 192.168.1.12):', False);
  ServerPage.Add('Porta da API (padrao: 8010):', False);
  ServerPage.Values[1] := '8010';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;

  if CurPageID = BearerTokenPage.ID then
  begin
    if Trim(BearerTokenPage.Values[0]) = '' then
    begin
      MsgBox('O Bearer Token e obrigatorio.', mbError, MB_OK);
      Result := False;
    end;
  end;

  if CurPageID = ServerPage.ID then
  begin
    if Trim(ServerPage.Values[0]) = '' then
    begin
      MsgBox('O IP do servidor e obrigatorio.', mbError, MB_OK);
      Result := False;
    end;
    if Trim(ServerPage.Values[1]) = '' then
    begin
      MsgBox('A porta da API e obrigatoria.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  AppDir: String;
  ServerIp: String;
  ApiPort: String;
  ApiBaseUrl: String;
  Params: String;
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    AppDir := ExpandConstant('{app}');
    ServerIp := Trim(ServerPage.Values[0]);
    ApiPort := Trim(ServerPage.Values[1]);
    ApiBaseUrl := 'http://' + ServerIp + ':' + ApiPort;

    Params := '-ExecutionPolicy Bypass -File "' + AppDir + '\scripts\Install-LeakguardAgent.ps1"';
    Params := Params + ' -BearerToken "' + BearerTokenPage.Values[0] + '"';
    Params := Params + ' -AgentId "' + AgentIdPage.Values[0] + '"';
    Params := Params + ' -ApiBaseUrl "' + ApiBaseUrl + '"';
    Params := Params + ' -ServerIp "' + ServerIp + '"';
    Params := Params + ' -NssmPath "' + AppDir + '\nssm.exe"';
    Params := Params + ' -ConfigTemplate "' + AppDir + '\config-template.json"';
    Params := Params + ' -LogoFile "' + AppDir + '\logo.png"';

    { Passa todos os caminhos - o PS1 prioriza PrebuiltExe se existir }
    Params := Params + ' -PrebuiltExe "' + AppDir + '\ScreenshotAuditAgent.exe"';
    Params := Params + ' -SourceDir "' + AppDir + '\source"';
    Params := Params + ' -RequirementsFile "' + AppDir + '\requirements.txt"';

    if not Exec('powershell.exe', Params, '', SW_SHOW, ewWaitUntilTerminated, ResultCode) then
      MsgBox('Falha ao executar o script de instalacao.', mbError, MB_OK)
    else if ResultCode <> 0 then
      MsgBox('O script retornou erro (codigo ' + IntToStr(ResultCode) + '). Verifique os logs em %ProgramData%\LeakguardAgent\logs.', mbError, MB_OK);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  AppDir: String;
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    AppDir := ExpandConstant('{app}');
    Exec('powershell.exe',
      '-ExecutionPolicy Bypass -File "' + AppDir + '\scripts\Uninstall-LeakguardAgent.ps1"',
      '', SW_SHOW, ewWaitUntilTerminated, ResultCode);
  end;
end;
