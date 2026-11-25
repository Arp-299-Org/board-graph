[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Live", "Offline")]
    [string]$Mode,

    [Parameter(Mandatory = $true)]
    [string]$Project,

    [Parameter(Mandatory = $true)]
    [string]$Script,

    [string[]]$ScriptArgs = @(),

    [int]$TimeoutSeconds = 180,

    [switch]$AllowLocalWindowsOffline,

    [switch]$AllowHistoricalV2,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemainingArgs = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$toolRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$projectRoot = if ($env:BOARD_GRAPH_PROJECT) {
    (Resolve-Path -LiteralPath $env:BOARD_GRAPH_PROJECT).Path
} else {
    $toolRoot
}
$externalMode = -not $projectRoot.Equals($toolRoot, [StringComparison]::OrdinalIgnoreCase)
$preflight = Join-Path $PSScriptRoot "kicad_preflight.ps1"
$scriptPath = if ([IO.Path]::IsPathRooted($Script)) {
    $Script
} else {
    $projectCandidate = Join-Path $projectRoot $Script
    if ($externalMode -and (Test-Path -LiteralPath $projectCandidate -PathType Leaf)) {
        $projectCandidate
    } else {
        Join-Path $toolRoot $Script
    }
}
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw "KiCad script does not exist: $scriptPath"
}
$scriptPath = (Resolve-Path -LiteralPath $scriptPath).Path
$toolPrefix = $toolRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
$projectPrefix = $projectRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
$inTool = $scriptPath.StartsWith($toolPrefix, [StringComparison]::OrdinalIgnoreCase)
$inProject = $scriptPath.StartsWith($projectPrefix, [StringComparison]::OrdinalIgnoreCase)
if (-not ($inTool -or $inProject)) {
    throw "Refusing to run a script outside the repository: $scriptPath"
}
if ([IO.Path]::GetExtension($scriptPath) -ne ".py") {
    throw "The guarded runner accepts Python scripts only: $scriptPath"
}

# Two registries: the board graph tool's own scripts (kicad-script-modes.json, part of the tool) and the
# project's (kicad-script-modes.project.json, optional). A script is run only when exactly one of them names it.
$registryPath = Join-Path $PSScriptRoot "kicad-script-modes.json"
$projectRegistryPath = Join-Path (Join-Path $projectRoot "scripts") "kicad-script-modes.project.json"
if (-not (Test-Path -LiteralPath $registryPath -PathType Leaf)) {
    throw "KiCad script-mode registry is missing: $registryPath"
}
$candidates = @()
if ($inTool) {
    $relTool = [IO.Path]::GetRelativePath($toolRoot, $scriptPath).Replace('\', '/')
    $candidates += [PSCustomObject]@{ Path = $registryPath; Rel = $relTool }
}
if ($inProject -and (Test-Path -LiteralPath $projectRegistryPath -PathType Leaf)) {
    $relProject = [IO.Path]::GetRelativePath($projectRoot, $scriptPath).Replace('\', '/')
    $candidates += [PSCustomObject]@{ Path = $projectRegistryPath; Rel = $relProject }
}
$entry = $null
$scriptRelative = if ($inTool) {
    [IO.Path]::GetRelativePath($toolRoot, $scriptPath).Replace('\', '/')
} else {
    [IO.Path]::GetRelativePath($projectRoot, $scriptPath).Replace('\', '/')
}
foreach ($candidate in $candidates) {
    $registry = Get-Content -LiteralPath $candidate.Path -Raw | ConvertFrom-Json
    if ($registry.schema_version -ne 1) {
        throw "Unsupported KiCad script-mode registry schema in $($candidate.Path): $($registry.schema_version)"
    }
    $found = $registry.scripts.PSObject.Properties[$candidate.Rel]
    if ($null -ne $found) {
        if ($null -ne $entry) {
            throw "KiCad script is named in both script-mode registries: $scriptRelative"
        }
        $entry = $found
    }
}
if ($null -eq $entry) {
    throw "KiCad script is not reviewed in scripts/kicad-script-modes.json or scripts/kicad-script-modes.project.json: $scriptRelative"
}
$allowedModes = @($entry.Value)
if ($Mode -notin $allowedModes) {
    throw "'$scriptPath' allows mode(s) $($allowedModes -join ', '), not $Mode."
}

& $preflight -Project $Project -Mode $Mode -AllowHistoricalV2:$AllowHistoricalV2

$python = if ($Mode -eq "Live") {
    if ($env:KICAD_IPC_PYTHON) {
        $env:KICAD_IPC_PYTHON
    } elseif ($IsWindows) {
        $toolVenv = Join-Path $toolRoot ".venv-kicad-ipc\Scripts\python.exe"
        if (Test-Path -LiteralPath $toolVenv -PathType Leaf) { $toolVenv } else { Join-Path $projectRoot ".venv-kicad-ipc\Scripts\python.exe" }
    } else {
        $toolVenv = Join-Path $toolRoot ".venv-kicad-ipc/bin/python"
        if (Test-Path -LiteralPath $toolVenv -PathType Leaf) { $toolVenv } else { Join-Path $projectRoot ".venv-kicad-ipc/bin/python" }
    }
} else {
    # tools/kicad/kicad_install.py is the Python home of these paths; change both together.
    if ($env:KICAD_PYTHON) {
        $env:KICAD_PYTHON
    } elseif ($IsMacOS) {
        "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3"
    } elseif ($IsLinux) {
        "/usr/bin/python3"
    } else {
        "C:\Program Files\KiCad\10.0\bin\python.exe"
    }
}

$siteFile = if ($env:BOARD_GRAPH_SITE) { $env:BOARD_GRAPH_SITE } else { Join-Path $projectRoot "tools\board_graph_site.json" }
if ($IsWindows -and $Mode -eq "Offline" -and -not $AllowLocalWindowsOffline -and (Test-Path -LiteralPath $siteFile -PathType Leaf)) {
    try {
        $siteData = Get-Content -LiteralPath $siteFile -Raw | ConvertFrom-Json
    } catch {
        $siteData = $null
    }
    if ($null -ne $siteData -and -not ($siteData.PSObject.Properties["one_machine"] -and $siteData.one_machine -eq $true)) {
        $km = $siteData.PSObject.Properties["kicad_machine"]
        if ($null -ne $km -and $null -ne $km.Value -and $km.Value.ssh_host) {
            throw "Refusing local Windows Offline KiCad Python while a remote KiCad machine ($($km.Value.ssh_host)) is configured in $siteFile. Run 'board-graph track-craft' or execute on the KiCad machine (pass -AllowLocalWindowsOffline only to override explicitly)."
        }
    }
}

$extraArgs = @($RemainingArgs)
if ($extraArgs.Count -gt 0 -and $extraArgs[0] -eq "--") {
    if ($extraArgs.Count -gt 1) {
        $extraArgs = @($extraArgs[1..($extraArgs.Count - 1)])
    } else {
        $extraArgs = @()
    }
}
$forwardedArgs = @($ScriptArgs) + $extraArgs
foreach ($tok in $forwardedArgs) {
    if (($tok -match '^".*",".*"' -or $tok -match ',--(?:json|output|geometry|witnesses)(?:=|$)') -and -not (Test-Path -LiteralPath $tok)) {
        throw "Refusing comma-mangled -ScriptArgs token: $tok. Pass arguments after '--' instead, e.g.: board-graph kicad-run -Mode Offline -Project $Project -Script $Script -- <arg1> <arg2>"
    }
}

Write-Output "runner_mode=$Mode"
Write-Output "runner_python=$python"
Write-Output "runner_script=$scriptPath"
$psi = [System.Diagnostics.ProcessStartInfo]::new()
$psi.FileName = $python
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.ArgumentList.Add($scriptPath)
foreach ($argItem in $forwardedArgs) {
    $psi.ArgumentList.Add([string]$argItem)
}
$proc = [System.Diagnostics.Process]::Start($psi)
$stdoutTask = $proc.StandardOutput.ReadToEndAsync()
$stderrTask = $proc.StandardError.ReadToEndAsync()
if ($TimeoutSeconds -gt 0) {
    $finished = $proc.WaitForExit($TimeoutSeconds * 1000)
} else {
    $proc.WaitForExit()
    $finished = $true
}
if (-not $finished) {
    try { $proc.Kill($true) } catch {}
    try { $proc.WaitForExit(5000) | Out-Null } catch {}
    $partialOut = $stdoutTask.GetAwaiter().GetResult()
    $partialErr = $stderrTask.GetAwaiter().GetResult()
    if ($partialOut) { [Console]::Out.Write($partialOut) }
    if ($partialErr) { [Console]::Error.Write($partialErr) }
    [Console]::Error.WriteLine("ERROR: kicad_run timed out after ${TimeoutSeconds}s running $scriptPath")
    exit 124
}
$proc.WaitForExit()
$outText = $stdoutTask.GetAwaiter().GetResult()
$errText = $stderrTask.GetAwaiter().GetResult()
if ($outText) { [Console]::Out.Write($outText) }
if ($errText) { [Console]::Error.Write($errText) }
exit $proc.ExitCode
