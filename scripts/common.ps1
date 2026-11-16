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

function Save-ElectoneWorkspace {
    param([Parameter(Mandatory = $true)][string]$Path)

    $config = Get-ElectoneConfig
    if ($null -eq $config) { $config = New-Object psobject }

    $config | Add-Member -NotePropertyName workspace -NotePropertyValue $Path -Force
    $config | ConvertTo-Json -Depth 4 | Set-Content -Path (Get-ElectoneConfigPath) -Encoding UTF8
}
