[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ServerUrl,

    [Parameter(Mandatory = $true)]
    [string]$ApiKey,

    [Parameter(Mandatory = $true)]
    [string]$UserName,

    [string]$ExecutablePath,

    [string]$GitHubToken,

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
    $token = $GitHubToken
    if (-not $token) {
        $token = $env:GITHUB_TOKEN
    }
    if (-not $token -and (Get-Command git -ErrorAction SilentlyContinue)) {
        try {
            $credentialLines = "protocol=https`nhost=github.com`n`n" | git credential fill 2>$null
            foreach ($line in $credentialLines) {
                $parts = $line -split '=', 2
                if ($parts.Count -eq 2 -and $parts[0] -eq 'password') {
                    $token = $parts[1]
                }
            }
        } catch {
            $token = $null
        }
    }

    if ($token) {
        $apiHeaders = @{
            Authorization = "Bearer $token"
            Accept = "application/vnd.github+json"
            "X-GitHub-Api-Version" = "2022-11-28"
            "User-Agent" = "codex-stats-installer"
        }
        $release = Invoke-RestMethod `
            -Uri "https://api.github.com/repos/bendertherobot7771/codex-stats-bot/releases/latest" `
            -Headers $apiHeaders
        $asset = $release.assets | Where-Object name -eq "codex-stats-agent.exe" | Select-Object -First 1
        if (-not $asset) {
            throw "В последнем GitHub Release не найден codex-stats-agent.exe"
        }
        $downloadHeaders = @{
            Authorization = "Bearer $token"
            Accept = "application/octet-stream"
            "X-GitHub-Api-Version" = "2022-11-28"
            "User-Agent" = "codex-stats-installer"
        }
        Invoke-WebRequest -Uri $asset.url -Headers $downloadHeaders -OutFile $target -UseBasicParsing
        $token = $null
        $GitHubToken = $null
    } else {
        try {
            Invoke-WebRequest -Uri $DownloadUrl -OutFile $target -UseBasicParsing
        } catch {
            throw "Не удалось скачать exe. Для приватного репозитория войдите в Git через Git Credential Manager, задайте GITHUB_TOKEN только на время установки или передайте -ExecutablePath."
        }
    }
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
