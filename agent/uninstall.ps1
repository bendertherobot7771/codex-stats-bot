$ErrorActionPreference = "Stop"
$installDirectory = Join-Path $env:LOCALAPPDATA "CodexStatsAgent"
$configDirectory = Join-Path $env:APPDATA "CodexStatsAgent"
if (Test-Path -LiteralPath (Join-Path $installDirectory 'current.json')) {
    throw 'Этот старый деинсталлятор предназначен для exe. Python-агент не остановлен и не удалён. Сначала выполните launch.py stop и удалите его задачу/ярлык автозапуска вручную.'
}
$launcher = Join-Path ([Environment]::GetFolderPath("Startup")) "CodexStatsAgent.vbs"

Get-Process -Name "codex-stats-agent" -ErrorAction SilentlyContinue | Stop-Process
Remove-Item -LiteralPath $launcher -Force -ErrorAction SilentlyContinue

if (Test-Path -LiteralPath $installDirectory) {
    Remove-Item -LiteralPath $installDirectory -Recurse -Force
}

Write-Host "Программа удалена. Локальная конфигурация и очередь сохранены в $configDirectory"

