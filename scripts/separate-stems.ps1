param(
    [Parameter(Position = 0)]
    [string]$AudioFile,
    [string]$Workspace,
    [string]$Model = 'roformer-model-bs-roformer-sw-by-jarredou',
    [switch]$Cpu,
    [switch]$Clean,
    [int]$Top = 0,
    [switch]$KeepAll
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
# The model downloader logs emoji; without UTF-8 the Windows console codec
# (GBK) raises UnicodeEncodeError and the download aborts.
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
# Wide models (e.g. the 53-stem mega) fragment the allocator badly on 8 GB
# laptop GPUs; expandable segments reuses freed blocks instead of OOM-ing.
$env:PYTORCH_CUDA_ALLOC_CONF = 'expandable_segments:True'

if ($Cpu) { $device = 'cpu' } else { $device = 'cuda' }

Write-Host "输入  : $AudioFile"
Write-Host "模型  : $Model"
Write-Host "设备  : $device"
Write-Host "输出  : $outDir"

$existing = @(Get-ChildItem $outDir -Filter '*.wav' -ErrorAction SilentlyContinue)
if ($existing.Count -gt 0 -and -not $Clean) {
    Write-Host "注意  : 输出目录已有 $($existing.Count) 个 wav，可能是上一次（不同模型）的结果。"
    Write-Host "        与本次结果混在一起会干扰检测；如需重跑请加 -Clean。"
    Write-Host ''
}
$runStarted = Get-Date

$python = Join-Path $root '.venv\Scripts\python.exe'
$progress = Join-Path $PSScriptRoot 'progress.py'
$checker = Join-Path $PSScriptRoot 'check-stems.py'
$aligner = Join-Path $PSScriptRoot 'align-model-config.py'

# Some registry models ship a config whose inference.chunk_size is not a
# multiple of stft_hop_length. The chunker then expects N samples while the
# model returns floor(N/hop)*hop and crashes in utils.demix_track. Align the
# config first (downloading the model once if needed) and pass it explicitly.
$inferArgs = @(
    '--input_folder', $staging
    '--store_dir', $outDir
    '--models_dir', $modelDir
    '--model', $Model
    '--device', $device
    '--output_format', 'wav_float32'
)

if ((Test-Path $python) -and (Test-Path $aligner)) {
    $fixed = & $python $aligner $Model --models-dir $modelDir 2>&1
    $fixedPath = ($fixed | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Last 1)
    if ($fixedPath) {
        $ckpt = Get-ChildItem (Join-Path $modelDir $Model) -Filter '*.ckpt' -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($ckpt) {
            Write-Host "配置  : chunk_size 已对齐 ($([System.IO.Path]::GetFileName($fixedPath)))"
            $inferArgs = @(
                '--input_folder', $staging
                '--store_dir', $outDir
                '--model_path', $ckpt.FullName
                '--config_path', $fixedPath.Trim()
                '--device', $device
                '--output_format', 'wav_float32'
            )
        }
    }
}

$progress = Join-Path $PSScriptRoot 'progress.py'

# Stage numbers differ depending on entry point: running download-audio.ps1
# makes download #1, so separation is #2 of 3. Invoked standalone, separation
# is the first stage of two.
$stageNo = 1
$stageTotal = 2
if ($env:ELECTONE_STAGE_OFFSET) { $stageNo = 2; $stageTotal = 3 }

try {
    if ((Test-Path $python) -and (Test-Path $progress)) {
        & $python $progress run --stage $stageNo --of $stageTotal --label '分离轨道 / separating stems' -- $inferExe @inferArgs
    } else {
        & $inferExe @inferArgs
    }
    $code = $LASTEXITCODE
} finally {
    Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue
}
if ($code -ne 0) { throw "bs-roformer-infer failed with exit code $code" }

# Only drop stale files once the new run actually produced output, so a failed
# or OOM-killed run cannot leave the song with an empty stem folder.
if ($Clean) {
    $fresh = @(Get-ChildItem $outDir -Filter '*.wav' | Where-Object { $_.LastWriteTime -ge $runStarted })
    if ($fresh.Count -gt 0) {
        Get-ChildItem $outDir -Filter '*.wav' |
            Where-Object { $_.LastWriteTime -lt $runStarted } |
            Remove-Item -Force
    } else {
        Write-Host "注意  : 本次未产生新文件，保留已有 $($existing.Count) 个 wav 不删除。"
    }
}

foreach ($file in Get-ChildItem $outDir -Filter '*.wav') {
    # The inference tool writes "<input file stem>_<source id>.wav".
    # Strip that prefix instead of matching a fixed list of names, so any
    # model works regardless of how many sources it emits.
    $stem = $null
    $prefix = "$name" + '_'
    if ($file.BaseName.StartsWith($prefix, [System.StringComparison]::Ordinal)) {
        $stem = $file.BaseName.Substring($prefix.Length)
    } elseif ($file.BaseName -match '\(([A-Za-z][A-Za-z0-9 _-]*)\)$') {
        $stem = $Matches[1]
    }
    if ($stem -and $file.BaseName -ne $stem) {
        Move-Item -LiteralPath $file.FullName -Destination (Join-Path $outDir "$stem.wav") -Force
    }
}

$python = Join-Path $root '.venv\Scripts\python.exe'
$checker = Join-Path $PSScriptRoot 'check-stems.py'

Write-Host ''
Write-Host ('=' * 64)
Write-Host ('[{0}/{1}] 质量检测 / quality check' -f $stageTotal, $stageTotal)
Write-Host ('=' * 64)
$produced = @(Get-ChildItem $outDir -Filter '*.wav' | Sort-Object Name)
Write-Host ''
Write-Host ("分离得到 {0} 个文件：" -f $produced.Count)
$produced | ForEach-Object { Write-Host "  $($_.BaseName)" }

# Quality check runs BEFORE pruning on purpose: pruning folds the discarded
# stems into `other`, which would make `other` resemble the mix and trip the
# "dumped" detector. The raw model output is what should be audited.
if ((Test-Path $python) -and (Test-Path $checker)) {
    & $python $checker --stems $outDir --mix $AudioFile
} else {
    Write-Host '提示：未找到检查脚本或 Python，跳过质量检查。'
}

# Then keep only the N most prominent stems. A 53-way model emits a long tail
# of near-empty leaf sources (triangle, wind-chimes, ...) that are noise for
# our purpose; what matters is the dozen or so audible parts.
$pruner = Join-Path $PSScriptRoot 'prune-stems.py'
if (-not $KeepAll -and $Top -gt 0) {
    $limit = $Top
} elseif (-not $KeepAll -and $produced.Count -gt 20) {
    $limit = 15
} else {
    $limit = 0
}
if ($limit -gt 0 -and (Test-Path $python) -and (Test-Path $pruner)) {
    Write-Host ''
    & $python $pruner --stems $outDir --top $limit
} elseif ($limit -gt 0) {
    Write-Host "提示：未找到 prune-stems.py，保留全部 $($produced.Count) 个轨道。"
}
