param(
    [Parameter(Position = 0)]
    [string]$AudioFile,
    [string]$Workspace,
    [string]$Model = 'roformer-model-bs-roformer-sw-by-jarredou',
    [switch]$Cpu
)

. (Join-Path $PSScriptRoot 'common.ps1')

$root = $script:ElectoneRepoRoot
$inferExe = Join-Path $root '.venv\Scripts\bs-roformer-infer.exe'

if (-not (Test-Path $inferExe)) {
    throw 'bs-roformer-infer is not installed. Run scripts\setup-env.ps1 first.'
}

$ws = Get-ElectoneWorkspace -Override $Workspace

if (-not $AudioFile) {
    $AudioFile = Get-ChildItem (Join-Path $ws 'audio') -Filter '*.wav' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
    if (-not $AudioFile) { throw "No wav file found in $(Join-Path $ws 'audio')." }
} else {
    $AudioFile = Resolve-ElectonePath $AudioFile
}
if (-not (Test-Path $AudioFile)) { throw "Audio file not found: $AudioFile" }

$AudioFile = Rename-ElectoneAudioFile -Path $AudioFile

$name = [System.IO.Path]::GetFileNameWithoutExtension($AudioFile)
$outDir = Join-Path $ws "stems\$name"
$modelDir = Join-Path $ws 'models'
$staging = Join-Path $ws "stems\.staging\$name"
New-Item -ItemType Directory -Force -Path $outDir, $modelDir, $staging | Out-Null
Copy-Item $AudioFile (Join-Path $staging ([System.IO.Path]::GetFileName($AudioFile))) -Force

$env:PATH = (Join-Path $root 'tools') + ';' + $env:PATH

if ($Cpu) { $device = 'cpu' } else { $device = 'cuda' }

Write-Host "输入  : $AudioFile"
Write-Host "模型  : $Model"
Write-Host "设备  : $device"
Write-Host "输出  : $outDir"
Write-Host ''

try {
    & $inferExe --input_folder $staging --store_dir $outDir --models_dir $modelDir --model $Model --device $device --output_format wav_float32
    $code = $LASTEXITCODE
} finally {
    Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue
}
if ($code -ne 0) { throw "bs-roformer-infer failed with exit code $code" }

foreach ($file in Get-ChildItem $outDir -Filter '*.wav') {
    $stem = $null
    if ($file.BaseName -match '_(vocals|drums|bass|guitar|piano|other|instrumental)$') {
        $stem = $Matches[1].ToLower()
    } elseif ($file.BaseName -match '\(([A-Za-z]+)\)$') {
        $stem = $Matches[1].ToLower()
    }
    if ($stem -and $file.BaseName -ne $stem) {
        Move-Item $file.FullName (Join-Path $outDir "$stem.wav") -Force
    }
}

$python = Join-Path $root '.venv\Scripts\python.exe'
$checker = Join-Path $PSScriptRoot 'check-stems.py'

Write-Host ''
if ((Test-Path $python) -and (Test-Path $checker)) {
    & $python $checker --stems $outDir --mix $AudioFile
} else {
    Write-Host '提示：未找到检查脚本或 Python，跳过质量检查。'
}
