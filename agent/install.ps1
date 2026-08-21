[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ServerUrl,

    [Parameter(Mandatory = $true)]
    [string]$ApiKey,

    [Parameter(Mandatory = $true)]
    [string]$UserName,

    [string]$ExecutablePath,

    [string]$DownloadUrl = "https://github.com/bendertherobot7771/codex-stats-bot/releases/latest/download/codex-stats-agent.exe"
)

$ErrorActionPreference = "Stop"
$installDirectory = Join-Path $env:LOCALAPPDATA "CodexStatsAgent"
$configDirectory = Join-Path $env:APPDATA "CodexStatsAgent"
$target = Join-Path $installDirectory "codex-stats-agent.exe"
$startupDirectory = [Environment]::GetFolderPath("Startup")
$launcher = Join-Path $startupDirectory "CodexStatsAgent.vbs"

New-Item -ItemType Directory -Force -Path $installDirectory, $configDirectory | Out-Null

if ($ExecutablePath) {
    Copy-Item -LiteralPath $ExecutablePath -Destination $target -Force
} else {
    Invoke-WebRequest -Uri $DownloadUrl -OutFile $target -UseBasicParsing
}

& $target configure --server-url $ServerUrl --api-key $ApiKey --user-name $UserName
if ($LASTEXITCODE -ne 0) {
    throw "Не удалось настроить Codex Stats Agent"
}

$escapedTarget = $target.Replace('"', '""')
$vbs = 'CreateObject("Wscript.Shell").Run """' + $escapedTarget + '"" watch", 0, False'
Set-Content -LiteralPath $launcher -Value $vbs -Encoding ASCII

Start-Process -FilePath $target -ArgumentList "watch" -WindowStyle Hidden
Write-Host "Codex Stats Agent установлен и запущен."
Write-Host "Исполняемый файл: $target"
Write-Host "Конфигурация: $(Join-Path $configDirectory 'config.json')"

