param(
    [Parameter(Mandatory = $true)][string]$ServerUrl,
    [Parameter(Mandatory = $true)][string]$Code,
    [string]$RepositoryUrl = 'https://github.com/bendertherobot7771/codex-stats-bot'
)
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$serverUri = [Uri]$ServerUrl
if ($serverUri.Scheme -notin @('http', 'https') -or $serverUri.UserInfo -or $serverUri.Query -or $serverUri.Fragment) { throw 'Некорректный адрес сервера' }
if ($serverUri.Scheme -eq 'http') {
    $ip = $null
    if (-not [Net.IPAddress]::TryParse($serverUri.Host, [ref]$ip)) { throw 'Для внешнего сервера необходим HTTPS' }
    $b = $ip.GetAddressBytes()
    if ($b.Length -ne 4 -or -not ($b[0] -eq 10 -or ($b[0] -eq 192 -and $b[1] -eq 168) -or ($b[0] -eq 172 -and $b[1] -ge 16 -and $b[1] -le 31) -or $b[0] -eq 127)) { throw 'Открытый HTTP разрешён только в локальной сети' }
    Write-Warning 'Подключение по HTTP допустимо только в доверенной домашней сети.'
}
if ($RepositoryUrl.TrimEnd('/').Replace('.git','') -ne 'https://github.com/bendertherobot7771/codex-stats-bot') { throw 'Этот установщик доверяет только исходному репозиторию проекта' }
$config = Join-Path $env:APPDATA 'CodexStatsAgent/config.json'
if (Test-Path -LiteralPath $config) { throw 'Агент уже настроен. Его автообновление выполняется в простое; настройки не изменены.' }
$root = Join-Path $env:LOCALAPPDATA 'CodexStatsAgent'
$target = Join-Path $root 'codex-stats-agent.exe'
if (Get-Process 'codex-stats-agent' -ErrorAction SilentlyContinue) { throw 'Сначала дождитесь завершения работы установленного агента' }
$temporary = Join-Path $env:TEMP ('codex-stats-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $temporary | Out-Null
$release = Invoke-RestMethod 'https://api.github.com/repos/bendertherobot7771/codex-stats-bot/releases/latest'
if ($release.draft -or $release.prerelease -or $release.tag_name -notmatch '^v\d+\.\d+\.\d+$') { throw 'Нет стабильного релиза' }
$base = 'https://github.com/bendertherobot7771/codex-stats-bot/releases/download/' + $release.tag_name
$envelope = Invoke-RestMethod ($base + '/release.json')
$payloadBytes = [Convert]::FromBase64String($envelope.payload)
$signature = [Convert]::FromBase64String($envelope.signature)
$modulusHex = 'C7ADFC51A1D0B207EA8C166CF747C793C6104C48D29C2CDCC5F69A2CF30C35C3F10BA3C2CCB43BB8E1D2FC188DD3979143A862B764CFC361C28B2E900A508937FF249912B26573AD304B5D870A31C8427E92E119B81F32A3DAC4FB302465C809C3A78D519046EE45BBB754B23E0243621149A56D5B8C10187E5E60A92DEFDC85EAA1BD6989A79C46E6CEE8BFCCEB139EA83880DB85AF738B4CD8034928F8BB2B52257A95294BFC29E0C0C26B367F4ABFCC802F3B12BA21017A66D07DEC70050C59BDCE7A3D439E6E87642018DBF52E11DD66C011DC00E91901A9C39C3358AE4BD3AB050F25690F9638C851216545E0A7FE8B282D6421384BC2077383802A6C77638C65D44CE0287040CAFB0B6662AC3A19897C7204D2B75EA151048DC9DD30A0C6A1C53E2BD543B5347589EABAE09CDA914C89334AD25B3C2EBDED76E32C34570BD6F4102D2C7AD5E5FF393DC9D32139D072C4C42448535994E398F3AF1E3F3991B2A7E28BD0A7CD44E2DEA378844D2AE23C92E2FC29D086163E747D9E56E7B3'
$modulus = New-Object byte[] ($modulusHex.Length / 2)
for ($i=0; $i -lt $modulus.Length; $i++) { $modulus[$i] = [Convert]::ToByte($modulusHex.Substring(2*$i,2),16) }
$parameters = New-Object Security.Cryptography.RSAParameters
$parameters.Modulus = $modulus
$parameters.Exponent = [byte[]]@(1,0,1)
$rsa = [Security.Cryptography.RSA]::Create()
$rsa.ImportParameters($parameters)
if (-not $rsa.VerifyData($payloadBytes, $signature, [Security.Cryptography.HashAlgorithmName]::SHA256, [Security.Cryptography.RSASignaturePadding]::Pkcs1)) { throw 'Подпись релиза недействительна. Установка остановлена.' }
$rsa.Dispose()
$manifest = [Text.Encoding]::UTF8.GetString($payloadBytes) | ConvertFrom-Json
if ('v' + $manifest.version -ne $release.tag_name -or $manifest.protocol -ne 1 -or $manifest.schema -ne 1 -or $manifest.min_agent_protocol -gt 1 -or -not $manifest.rollback_safe) { throw 'Несовместимый релиз' }
$download = Join-Path $temporary 'codex-stats-agent.exe'
Invoke-WebRequest ($base + '/codex-stats-agent.exe') -OutFile $download -UseBasicParsing
$asset = $manifest.assets.'codex-stats-agent.exe'
if ((Get-Item -LiteralPath $download).Length -ne $asset.size -or (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash.ToLowerInvariant() -ne $asset.sha256) { throw 'Контрольная сумма не совпала' }
New-Item -ItemType Directory -Path $root -Force | Out-Null
Copy-Item -LiteralPath $download -Destination $target -Force
& $target enroll --server-url $ServerUrl.TrimEnd('/') --code $Code
if ($LASTEXITCODE -ne 0) { throw 'Не удалось подключиться. Получите новый код командой /addpc.' }
$startup = [Environment]::GetFolderPath('Startup')
$launcher = Join-Path $startup 'CodexStatsAgent.vbs'
$escapedTarget = $target.Replace('"', '""')
$vbs = 'CreateObject("Wscript.Shell").Run """' + $escapedTarget + '"" watch", 0, False'
Set-Content -LiteralPath $launcher -Value $vbs -Encoding ASCII
Start-Process -FilePath $target -ArgumentList 'watch' -WindowStyle Hidden
Write-Host 'Готово. Агент подключён, автозапуск и обновления в простое включены.'
Write-Host 'Проверить подключение можно в Telegram: /updates'
