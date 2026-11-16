param(
    [Parameter(Position = 0)]
    [string]$Song,
    [string]$Workspace,
    [string]$OutDir,
    [string]$CookiesFromBrowser,
    [switch]$NoSeparate
)

. (Join-Path $PSScriptRoot 'common.ps1')

$toolsDir = Join-Path $script:ElectoneRepoRoot 'tools'
$ytdlp = Join-Path $toolsDir 'yt-dlp.exe'

if (-not (Test-Path $ytdlp)) { throw "yt-dlp.exe not found: $ytdlp" }
if ($Workspace -and $OutDir) { throw 'Use either -Workspace or -OutDir, not both.' }

$interactive = [string]::IsNullOrWhiteSpace($Song)
if ($interactive) { $Song = Read-Host '曲名或视频 URL' }
if ([string]::IsNullOrWhiteSpace($Song)) { throw 'No song name or URL given.' }

$root = Get-ElectoneWorkspace -Override $Workspace

if ($OutDir) {
    $saveDir = Resolve-ElectonePath $OutDir
} else {
    if ($interactive -and -not $Workspace) {
        $defaultRoot = $root
        $answer = Read-Host "工作区目录 [$defaultRoot]（音频保存在其下的 audio 文件夹，回车使用默认）"
        if (-not [string]::IsNullOrWhiteSpace($answer)) {
            $newRoot = Resolve-ElectonePath ($answer.Trim().Trim('"'))
            if (-not [string]::Equals($newRoot, $defaultRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
                $remember = Read-Host '以后都用这个工作区? (y/N)'
                if ($remember -match '^(y|yes)$') {
                    Save-ElectoneWorkspace -Path $newRoot
                    Write-Host "已保存到 $(Get-ElectoneConfigPath)"
                }
            }
            $root = $newRoot
        }
    }
    $saveDir = Join-Path $root 'audio'
}

if (-not (Test-Path $saveDir)) { New-Item -ItemType Directory -Force -Path $saveDir | Out-Null }

if ($Song -match '^https?://') {
    $target = $Song
} else {
    $target = "ytsearch1:$Song"
}

$arguments = @(
    $target
    '--no-playlist'
    '--ffmpeg-location', $toolsDir
    '-x'
    '--audio-format', 'wav'
    '--postprocessor-args', 'ExtractAudio:-ac 2 -ar 44100'
    '-o', (Join-Path $saveDir '%(title).120s.%(ext)s')
    '--no-mtime'
    '--newline'
)

if (Get-Command node -ErrorAction SilentlyContinue) {
    $arguments += @('--js-runtimes', 'node')
}
if ($CookiesFromBrowser) {
    $arguments += @('--cookies-from-browser', $CookiesFromBrowser)
}

Write-Host "输入  : $Song"
Write-Host "工作区: $root"
Write-Host "输出  : $saveDir"
Write-Host ''

& $ytdlp @arguments
if ($LASTEXITCODE -ne 0) { throw "yt-dlp failed with exit code $LASTEXITCODE" }

$latest = Get-ChildItem $saveDir -Filter '*.wav' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($latest) {
    $renamed = Rename-ElectoneAudioFile -Path $latest.FullName
    if ($renamed -ne $latest.FullName) {
        Write-Host "Renamed: $([System.IO.Path]::GetFileName($renamed))"
    }
    Write-Host "Saved: $renamed"
    $latest = Get-Item -LiteralPath $renamed
}

if ($latest -and -not $NoSeparate) {
    Write-Host ''
    & (Join-Path $PSScriptRoot 'separate-stems.ps1') -AudioFile $latest.FullName -Workspace $root
}
