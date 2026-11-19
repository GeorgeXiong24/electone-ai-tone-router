$script:ElectoneRepoRoot = Split-Path -Parent $PSScriptRoot

function Resolve-ElectonePath {
    param([string]$Path)
    return $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
}

function Get-ElectoneConfigPath {
    return (Join-Path $script:ElectoneRepoRoot 'config.json')
}

function Get-ElectoneConfig {
    $path = Get-ElectoneConfigPath
    if (-not (Test-Path $path)) { return $null }
    try {
        return (Get-Content -Raw -Encoding UTF8 -Path $path | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Get-ElectoneWorkspace {
    param([string]$Override)

    if ($Override) { return (Resolve-ElectonePath $Override) }
    if ($env:ELECTONE_WORKSPACE) { return (Resolve-ElectonePath $env:ELECTONE_WORKSPACE) }

    $config = Get-ElectoneConfig
    if ($config -and $config.workspace) { return (Resolve-ElectonePath $config.workspace) }

    return (Join-Path $script:ElectoneRepoRoot 'workspace')
}

function Get-ElectonePrimaryName {
    param([string]$Text)

    $t = $Text.Trim()
    if (-not $t) { return '' }

    $cjk = [regex]::Match($t, '[\u4e00-\u9fff\u3400-\u4dbf]+')
    if ($cjk.Success) { return $cjk.Value }

    $t = $t -replace '\s*[\(\[（【].*$', ''
    return $t.Trim()
}

function Get-ElectoneTrackName {
    param(
        [Parameter(Mandatory = $true)][string]$Title,
        [int]$MaxLength = 80
    )

    $t = $Title.Trim()
    $artist = ''
    $song = ''

    $segments = @($t -split '\s+-\s+' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($segments.Count -ge 2) {
        $artist = Get-ElectonePrimaryName $segments[0]
        $song = Get-ElectonePrimaryName ($segments[1] -replace '\s*[\(\[（【].*$', '')
    } else {
        $bracket = [regex]::Match($t, '[【\[（]([^】\]）]+)[】\]）]')
        if ($bracket.Success) {
            $prefix = $t.Substring(0, $bracket.Index)
            if (-not [string]::IsNullOrWhiteSpace($prefix)) {
                $artist = Get-ElectonePrimaryName $prefix
            }
            $song = Get-ElectonePrimaryName $bracket.Groups[1].Value
        } else {
            $song = Get-ElectonePrimaryName $t
        }
    }

    if ($artist -and $song -and $artist -ne $song) {
        $name = "$artist - $song"
    } elseif ($song) {
        $name = $song
    } else {
        $name = $artist
    }

    $name = $name -replace '[<>:"/\\|?*]', ''
    $name = ($name -replace '\s+', ' ').Trim().TrimEnd('.')
    if ($name.Length -gt $MaxLength) {
        $name = $name.Substring(0, $MaxLength).Trim().TrimEnd('.')
    }
    return $name
}

function Rename-ElectoneAudioFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    $file = Get-Item -LiteralPath $Path
    $clean = Get-ElectoneTrackName -Title $file.BaseName
    if (-not $clean -or $clean -eq $file.BaseName) { return $file.FullName }

    $target = Join-Path $file.DirectoryName "$clean$($file.Extension)"
    $i = 2
    while ((Test-Path -LiteralPath $target) -and
           -not [string]::Equals($target, $file.FullName, [System.StringComparison]::OrdinalIgnoreCase)) {
        $target = Join-Path $file.DirectoryName "$clean ($i)$($file.Extension)"
        $i++
    }

    if ([string]::Equals($target, $file.FullName, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $file.FullName
    }

    Move-Item -LiteralPath $file.FullName -Destination $target -Force
    return $target
}

function Save-ElectoneWorkspace {
    param([Parameter(Mandatory = $true)][string]$Path)

    $config = Get-ElectoneConfig
    if ($null -eq $config) { $config = New-Object psobject }

    $config | Add-Member -NotePropertyName workspace -NotePropertyValue $Path -Force
    $config | ConvertTo-Json -Depth 4 | Set-Content -Path (Get-ElectoneConfigPath) -Encoding UTF8
}

# --- cache redirection: keep every cache inside the project workspace ---------
# Tools (torch, HuggingFace, numba, pip, yt-dlp, ...) otherwise drop caches into
# the user profile on C:. These helpers point them at <workspace>/cache and are
# applied to every child process; scripts/_cache_env.py does the same for
# direct `python scripts/<script>.py` runs.

function Get-ElectoneCacheRoot {
    if (-not $env:ELECTONE_CACHE) {
        $env:ELECTONE_CACHE = Join-Path (Get-ElectoneWorkspace) 'cache'
    }
    if (-not (Test-Path $env:ELECTONE_CACHE)) {
        New-Item -ItemType Directory -Force -Path $env:ELECTONE_CACHE | Out-Null
    }
    return $env:ELECTONE_CACHE
}

function Set-ElectoneCacheEnv {
    $cache = Get-ElectoneCacheRoot
    if (-not $env:XDG_CACHE_HOME) { $env:XDG_CACHE_HOME = $cache }
    if (-not $env:TORCH_HOME) { $env:TORCH_HOME = Join-Path $cache 'torch' }
    if (-not $env:HF_HOME) { $env:HF_HOME = Join-Path $cache 'huggingface' }
    if (-not $env:NUMBA_CACHE_DIR) { $env:NUMBA_CACHE_DIR = Join-Path $cache 'numba' }
    if (-not $env:MPLCONFIGDIR) { $env:MPLCONFIGDIR = Join-Path $cache 'matplotlib' }
    if (-not $env:PIP_CACHE_DIR) { $env:PIP_CACHE_DIR = Join-Path $cache 'pip' }
}

Set-ElectoneCacheEnv
