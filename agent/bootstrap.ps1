param(
    [string]$ServerUrl,
    [string]$Code,
    [switch]$PrepareOnly,
    [string]$InstallRoot,
    [string]$ReleaseDirectory,
    [string]$PythonArchive,
    [string]$RepositoryUrl = 'https://github.com/bendertherobot7771/codex-stats-bot'
)
$ErrorActionPreference = 'Stop'
if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { throw 'Поддерживается Windows x64' }
if (-not $PrepareOnly -and (-not $ServerUrl -or -not $Code)) { throw 'Необходимы ServerUrl и Code из /addpc' }
if ($PrepareOnly -and -not $ServerUrl) { $ServerUrl = 'http://127.0.0.1:8765' }
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
if (-not $PrepareOnly -and (Test-Path -LiteralPath $config)) { throw 'Агент уже настроен. Нужна миграция в простое; настройки не изменены.' }
$root = Join-Path $env:LOCALAPPDATA 'CodexStatsAgent'
if ($InstallRoot) { $root = [IO.Path]::GetFullPath($InstallRoot) }
if ($PrepareOnly -and -not $InstallRoot) { throw 'Для проверки укажите отдельный InstallRoot' }
if (Test-Path -LiteralPath (Join-Path $root 'current.json')) { throw 'Каталог уже установлен; перезапись запрещена' }
if (-not $PrepareOnly -and (Get-Process 'codex-stats-agent' -ErrorAction SilentlyContinue)) { throw 'Сначала дождитесь завершения работы установленного агента' }
$temporary = Join-Path $env:TEMP ('codex-stats-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $temporary | Out-Null
if ($ReleaseDirectory -and -not $PrepareOnly) { throw 'Локальный пакет разрешён только для проверки/подготовки' }
if ($ReleaseDirectory) {
    $envelope = Get-Content -LiteralPath (Join-Path $ReleaseDirectory 'release.json') -Raw | ConvertFrom-Json
    $release = @{draft=$false; prerelease=$false; tag_name=('v' + (([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($envelope.payload)) | ConvertFrom-Json).version))}
} else {
    $release = Invoke-RestMethod 'https://api.github.com/repos/bendertherobot7771/codex-stats-bot/releases/latest'
}
if ($release.draft -or $release.prerelease -or $release.tag_name -notmatch '^v\d+\.\d+\.\d+$') { throw 'Нет стабильного релиза' }
$base = 'https://github.com/bendertherobot7771/codex-stats-bot/releases/download/' + $release.tag_name
if (-not $ReleaseDirectory) { $envelope = Invoke-RestMethod ($base + '/release.json') }
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
$download = Join-Path $temporary 'agent-source.zip'
if ($ReleaseDirectory) { Copy-Item -LiteralPath (Join-Path $ReleaseDirectory 'agent-source.zip') -Destination $download }
else { Invoke-WebRequest ($base + '/agent-source.zip') -OutFile $download -UseBasicParsing -TimeoutSec 180 }
$asset = $manifest.assets.'agent-source.zip'
if ((Get-Item -LiteralPath $download).Length -ne $asset.size -or (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash.ToLowerInvariant() -ne $asset.sha256) { throw 'Контрольная сумма не совпала' }
New-Item -ItemType Directory -Path $root -Force | Out-Null
$runtime = Join-Path $root 'runtime/3.14.7'
if (Test-Path -LiteralPath $runtime) { throw 'Каталог Python уже существует; автоматическая перезапись запрещена' }
$pythonZip = Join-Path $temporary 'python.zip'
if ($PythonArchive) { Copy-Item -LiteralPath $PythonArchive -Destination $pythonZip }
else { Invoke-WebRequest 'https://www.python.org/ftp/python/3.14.7/python-3.14.7-embed-amd64.zip' -OutFile $pythonZip -UseBasicParsing -TimeoutSec 600 }
if ((Get-FileHash -LiteralPath $pythonZip -Algorithm SHA256).Hash.ToLowerInvariant() -ne 'd297e5ff019966817ad8502465176139f2d3d840fa4ed84b13bed399a6ab1f15') { throw 'Контрольная сумма официального Python не совпала' }
Expand-Archive -LiteralPath $pythonZip -DestinationPath $runtime
$python = Join-Path $runtime 'python.exe'
$pythonw = Join-Path $runtime 'pythonw.exe'
foreach ($executable in @($python, $pythonw)) {
    $sign = Get-AuthenticodeSignature -LiteralPath $executable
    if ($sign.Status -ne 'Valid' -or $sign.SignerCertificate.Subject -notmatch 'O=Python Software Foundation') { throw 'Не удалось подтвердить издателя Python' }
}
$releaseDirectory = Join-Path $root ('releases/' + $manifest.version)
# Inspect ZIP names before Expand-Archive (no traversal, ADS, aliases or links).
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::OpenRead($download)
try {
    $seen = @{}; $total = 0
    foreach ($entry in $zip.Entries) {
        $name = $entry.FullName
        $total += $entry.Length
        if ($name -notmatch '^(agent|common)/[A-Za-z0-9_./-]+\.py$' -or $name -match '(^|/)\.|\.(/|$)|//|(^|/)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\.|/|$)' -or $seen.ContainsKey($name) -or (($entry.ExternalAttributes -shr 16) -band 61440) -eq 40960) { throw 'Небезопасный архив исходников' }
        $seen[$name] = $true
    }
    if ($total -gt 20000000 -or $seen.Count -gt 1000 -or -not $seen.ContainsKey('agent/launch.py')) { throw 'Неверный размер или состав архива' }
} finally { $zip.Dispose() }
Expand-Archive -LiteralPath $download -DestinationPath $releaseDirectory
$asset | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $releaseDirectory '.verified.json') -Encoding ASCII
$entrypoint = Join-Path $releaseDirectory 'agent/launch.py'
$actualVersion = & $python -B $entrypoint --version
if ($LASTEXITCODE -ne 0 -or $actualVersion.Trim() -ne $manifest.version) { throw 'Python-пакет не запускается; существующий агент не изменён' }
Copy-Item -LiteralPath $entrypoint -Destination (Join-Path $root 'launch.py')
Copy-Item -LiteralPath $releaseDirectory -Destination (Join-Path $root 'recovery') -Recurse
@{version=$manifest.version} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'current.json') -Encoding ASCII
$envelope | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'release.json') -Encoding ASCII
if ($PrepareOnly) { Write-Host 'Проверка завершена: код подготовлен; сборщик, регистрация и автозапуск не включались.'; return }
& $python -B (Join-Path $root 'launch.py') enroll --server-url $ServerUrl.TrimEnd('/') --code $Code
if ($LASTEXITCODE -ne 0) { throw 'Не удалось подключиться. Получите новый код командой /addpc.' }
$arguments = '-B "' + (Join-Path $root 'launch.py') + '" watch'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$taskName = 'CodexStatsAgent-' + $identity.User.Value
try {
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
    $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -ErrorAction Stop | Out-Null
} catch {
    # Some PCs deny task registration to ordinary users. A per-user shortcut requires no elevation.
    $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path ([Environment]::GetFolderPath('Startup')) 'CodexStatsAgent.lnk'))
    $shortcut.TargetPath = $pythonw; $shortcut.Arguments = $arguments; $shortcut.Save()
    Write-Warning 'Планировщик недоступен: создан ярлык автозапуска текущего пользователя.'
}
Start-Process -FilePath $pythonw -ArgumentList $arguments -WindowStyle Hidden
Write-Host 'Готово. Агент подключён, автозапуск и обновления в простое включены.'
Write-Host 'Проверить подключение можно в Telegram: /updates'
