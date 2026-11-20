param(
    [switch]$Cpu
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root '.venv'
$python = Join-Path $venv 'Scripts\python.exe'

# Keep pip's download cache inside the project instead of the user profile.
if (-not $env:PIP_CACHE_DIR) {
    $env:PIP_CACHE_DIR = Join-Path $root 'workspace\cache\pip'
}
New-Item -ItemType Directory -Force -Path $env:PIP_CACHE_DIR | Out-Null

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'Git is required to install bs-roformer-infer.'
}

if (-not (Test-Path $python)) {
    Write-Host 'Creating virtual environment...'
    & python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create virtual environment (Python 3.10+ required).' }
}

Write-Host 'Upgrading pip...'
& $python -m pip install --upgrade pip --quiet

if ($Cpu -or -not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
    $torchIndex = 'https://download.pytorch.org/whl/cpu'
} else {
    $torchIndex = 'https://download.pytorch.org/whl/cu128'
}

Write-Host "Installing PyTorch from $torchIndex ..."
& $python -m pip install torch --index-url $torchIndex
if ($LASTEXITCODE -ne 0) { throw 'PyTorch install failed.' }

Write-Host 'Installing bs-roformer-infer...'
& $python -m pip install "bs-roformer-infer @ git+https://github.com/openmirlab/bs-roformer-infer.git"
if ($LASTEXITCODE -ne 0) { throw 'bs-roformer-infer install failed.' }

# Second-stage refinement (DrumSep and other specialist models) needs
# audio-separator. It pins a few older deps that would break bs-roformer, so it
# is installed without dependency resolution and the needed pieces are added
# explicitly, keeping bs-roformer's versions intact.
Write-Host 'Installing audio-separator (for specialist sub-stem models)...'
& $python -m pip install audio-separator --no-deps
if ($LASTEXITCODE -ne 0) { throw 'audio-separator install failed.' }

& $python -m pip install --quiet librosa einops julius ml_collections packaging pydub requests resampy six tqdm audioread onnxruntime-gpu samplerate audioop-lts
if ($LASTEXITCODE -ne 0) { Write-Host 'WARN: some audio-separator dependencies failed to install; refinement may be unavailable.' }

Write-Host ''
& $python -c "import torch, bs_roformer; print('torch', torch.__version__, '| cuda available:', torch.cuda.is_available())"
