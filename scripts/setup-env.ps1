param(
    [switch]$Cpu
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root '.venv'
$python = Join-Path $venv 'Scripts\python.exe'

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

Write-Host ''
& $python -c "import torch, bs_roformer; print('torch', torch.__version__, '| cuda available:', torch.cuda.is_available())"
