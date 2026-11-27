[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Project,

    [Parameter(Mandatory = $true)]
    [ValidateSet("Live", "Offline")]
    [string]$Mode,

    [switch]$AllowHistoricalV2,

    [switch]$SchematicOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Stop-Preflight {
    param([string]$Status, [string]$Message)
    Write-Output "status=$Status"
    throw $Message
}

# Every program this preflight starts goes through Invoke-Console: no window on Windows.
. (Join-Path $PSScriptRoot "invoke_console.ps1")

function Resolve-ProjectIdentity {
    param([string]$RepositoryRoot, [string]$RequestedProject)

    $candidate = if ([IO.Path]::IsPathRooted($RequestedProject)) {
        $RequestedProject
    } else {
        Join-Path $RepositoryRoot $RequestedProject
    }
    if (-not (Test-Path -LiteralPath $candidate)) {
        Stop-Preflight "project_missing" "Project does not exist: $candidate"
    }

    $resolved = (Resolve-Path -LiteralPath $candidate).Path
    if (Test-Path -LiteralPath $resolved -PathType Container) {
        $projectDirectory = $resolved
        $projectFiles = @(Get-ChildItem -LiteralPath $projectDirectory -Filter "*.kicad_pro" -File)
        if ($projectFiles.Count -ne 1) {
            Stop-Preflight "project_ambiguous" "Expected exactly one .kicad_pro under '$projectDirectory'; found $($projectFiles.Count)."
        }
        $projectFile = $projectFiles[0].FullName
    } elseif ([IO.Path]::GetExtension($resolved) -eq ".kicad_pro") {
        $projectFile = $resolved
        $projectDirectory = Split-Path -Parent $resolved
    } else {
        Stop-Preflight "project_invalid" "Project must be a directory or .kicad_pro file: $resolved"
    }

    $baseName = [IO.Path]::GetFileNameWithoutExtension($projectFile)
    $boardFile = Join-Path $projectDirectory "$baseName.kicad_pcb"
    $schematicFile = Join-Path $projectDirectory "$baseName.kicad_sch"
    $requiredFiles = @($schematicFile)
    if (-not $SchematicOnly) {
        $requiredFiles += $boardFile
    }
    foreach ($required in $requiredFiles) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            Stop-Preflight "source_missing" "Required KiCad source is missing: $required"
        }
    }

    [PSCustomObject]@{
        Directory = $projectDirectory
        ProjectFile = $projectFile
        BoardFile = $boardFile
        SchematicFile = $schematicFile
        BaseName = $baseName
    }
}

$toolRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$repoRoot = if ($env:BOARD_GRAPH_PROJECT) {
    (Resolve-Path -LiteralPath $env:BOARD_GRAPH_PROJECT).Path
} else {
    $toolRoot
}
if ($SchematicOnly -and $Mode -ne "Offline") {
    Stop-Preflight "invalid_mode" "SchematicOnly requires Offline mode."
}
$identity = Resolve-ProjectIdentity -RepositoryRoot $repoRoot -RequestedProject $Project
$repoPrefix = $repoRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
if (-not $identity.Directory.StartsWith($repoPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    Stop-Preflight "project_outside_repository" "Refusing a project outside the repository: $($identity.Directory)"
}

$relativeProject = [IO.Path]::GetRelativePath($repoRoot, $identity.Directory)
if ($relativeProject -eq "v2-kicad" -and -not $AllowHistoricalV2) {
    Stop-Preflight "historical_project_blocked" "v2-kicad is historical/reference. Use board, or pass -AllowHistoricalV2 only when the user explicitly puts v2 in scope."
} elseif ($relativeProject -eq "v2-kicad") {
    Write-Output "warning=historical v2-kicad access explicitly enabled"
}

# KiCad bundles its own Python, a different one on each platform; scripts run on
# whichever Python the installed KiCad ships.
# tools/kicad/kicad_install.py is the Python home of these paths; change both together.
$kicadPython = if ($env:KICAD_PYTHON) {
    $env:KICAD_PYTHON
} elseif ($IsMacOS) {
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3"
} elseif ($IsLinux) {
    "/usr/bin/python3"
} else {
    "C:\Program Files\KiCad\10.0\bin\python.exe"
}
if (-not (Test-Path -LiteralPath $kicadPython -PathType Leaf)) {
    Stop-Preflight "offline_python_missing" "KiCad Python not found at '$kicadPython'. Set KICAD_PYTHON explicitly."
}

$offlineProbe = Invoke-Console $kicadPython @("-c", "import pcbnew,sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}|{pcbnew.GetBuildVersion()}')")
if ($offlineProbe.ExitCode -ne 0) {
    Stop-Preflight "offline_python_invalid" "KiCad Python could not import pcbnew: $($offlineProbe.Lines -join ' ')"
}
$offlineIdentity = $offlineProbe.Stdout.Trim()
$offlineParts = $offlineIdentity.Split('|')
# The whole version, so a release candidate or development build (10.0.7-rc1) is not taken for the stable release.
# Linux distro packages can append packaging metadata (e.g. 10.0.8-10.0.8+dfsg-1~ubuntu24.04.1).
$prereleasePattern = '(?i)(?:^|[._-])(rc|alpha|beta|dev|pre)(?:\d*|[._+-]|$)'
if ($IsLinux) {
    if ($offlineParts.Count -ne 2 -or $offlineParts[1] -notmatch '^(\d+\.\d+\.\d+)(?:[+-].*)?$' -or $offlineParts[1] -match $prereleasePattern) {
        Stop-Preflight "offline_version_unreadable" "Expected KiCad's Python and a stable pcbnew release (X.Y.Z); got '$offlineIdentity'."
    }
} else {
    if ($offlineParts.Count -ne 2 -or $offlineParts[1] -notmatch '^(\d+\.\d+\.\d+)$') {
        Stop-Preflight "offline_version_unreadable" "Expected KiCad's Python and a stable pcbnew release (X.Y.Z); got '$offlineIdentity'."
    }
}
$offlineBaseVersion = $Matches[1]
$kicadVersion = [version]$offlineBaseVersion

# Keep KiCad on the latest stable release. Raise the minimum whenever a new
# 10.0.x ships. Another series changes the project's file format, so moving to
# KiCad 11 is a one-time migration, not an update.
$minimumKiCad = [version]"10.0.8"
if ($kicadVersion.Major -ne $minimumKiCad.Major -or $kicadVersion.Minor -ne $minimumKiCad.Minor) {
    Stop-Preflight "kicad_series_mismatch" "KiCad $kicadVersion is not a $($minimumKiCad.Major).$($minimumKiCad.Minor) release; moving the project to another series changes its file format and is the owner's migration decision."
}
if ($kicadVersion -lt $minimumKiCad) {
    Stop-Preflight "kicad_outdated" "KiCad $kicadVersion is older than $minimumKiCad, the oldest this repository accepts: KiCad stays on the latest stable release (before 10.0.7 a zone refill can short other-net pads, KiCad issue 25670). Update KiCad."
}

Write-Output "project=$($identity.ProjectFile)"
if (-not $SchematicOnly) {
    Write-Output "board=$($identity.BoardFile)"
}
Write-Output "schematic=$($identity.SchematicFile)"
Write-Output "offline_python=$kicadPython"
Write-Output "offline_identity=python-$($offlineParts[0]) pcbnew-$($offlineParts[1])"

$gitStatus = Invoke-Console "git" @("-C", $repoRoot, "status", "--short", "--", $relativeProject)
$projectChanges = @($gitStatus.Stdout -split "\r?\n" | Where-Object { $_ -ne "" })
if ($projectChanges.Count -eq 0) {
    Write-Output "project_git_state=clean"
} else {
    Write-Output "project_git_state=dirty count=$($projectChanges.Count)"
    foreach ($change in $projectChanges) {
        Write-Output "project_change=$change"
    }
}

if ($Mode -eq "Offline") {
    $locks = @(Get-ChildItem -LiteralPath $identity.Directory -Filter "~*.kicad_*.lck" -File -ErrorAction SilentlyContinue)
    if ($locks.Count -gt 0) {
        Write-Output "warning=KiCad lock files are present; offline inspection is allowed, but do not mutate files open in KiCad"
    }
    Write-Output "mode=Offline"
    Write-Output "status=ready"
    return
}

$livePython = if ($env:KICAD_IPC_PYTHON) {
    $env:KICAD_IPC_PYTHON
} elseif ($IsWindows) {
    $toolVenv = Join-Path $toolRoot ".venv-kicad-ipc\Scripts\python.exe"
    if (Test-Path -LiteralPath $toolVenv -PathType Leaf) { $toolVenv } else { Join-Path $repoRoot ".venv-kicad-ipc\Scripts\python.exe" }
} else {
    $toolVenv = Join-Path $toolRoot ".venv-kicad-ipc/bin/python"
    if (Test-Path -LiteralPath $toolVenv -PathType Leaf) { $toolVenv } else { Join-Path $repoRoot ".venv-kicad-ipc/bin/python" }
}
if (-not (Test-Path -LiteralPath $livePython -PathType Leaf)) {
    Write-Output "live_python=$livePython"
    Write-Output "recovery=.\scripts\setup_kicad_ipc.ps1 -Confirm"
    Stop-Preflight "live_environment_missing" "The pinned live KiCad IPC environment is absent. Setup is a dependency/network mutation and must be explicitly approved before running the recovery command."
}

$liveProbe = 'import importlib.metadata as m,sys; print("|".join([f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}", m.version("kicad-python"), m.version("protobuf"), m.version("pynng")]))'
$liveRun = Invoke-Console $livePython @("-c", $liveProbe)
if ($liveRun.ExitCode -ne 0) {
    Stop-Preflight "live_environment_invalid" "The live IPC environment cannot import its pinned packages: $($liveRun.Lines -join ' ')"
}
$liveIdentity = $liveRun.Stdout.Trim()
$liveParts = $liveIdentity.Split('|')
# setup_kicad_ipc.ps1 builds the environment from KiCad's own Python, so it must
# be the installed KiCad's Python. A KiCad update that changes that Python
# needs the environment rebuilt.
$expectedLive = @($offlineParts[0], "0.7.1", "5.29.6", "0.9.0")
if ($liveParts.Count -ne 4 -or (Compare-Object $expectedLive $liveParts)) {
    Stop-Preflight "live_version_mismatch" "Required Python/kicad-python/protobuf/pynng versions are '$($expectedLive -join '|')'; got '$liveIdentity'."
}

if ($IsWindows) {
    # KiCad 10 hosts the PCB Editor inside kicad.exe when it is opened from the
    # Project Manager (window title "<project> — PCB Editor"); a stand-alone launch
    # is a pcbnew.exe process. Count both, so the guard is about how many editors
    # are open and not about how the user chose to open one.
    $pcbnewProcesses = @(Get-Process -Name "pcbnew" -ErrorAction SilentlyContinue)
    $pcbnewProcesses += @(Get-Process -Name "kicad" -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowTitle -match 'PCB Editor' })
    $editorRule = "exactly one PCB Editor process"
} else {
    # macOS gives Get-Process no window titles, so it cannot tell whether the
    # KiCad app has a PCB Editor open. Require exactly one KiCad app (the KiCad
    # app or a stand-alone PCB Editor) instead. That is stricter than the Windows
    # count, and the IPC check below confirms which board that app has open.
    $pcbnewProcesses = @(Get-Process -Name "pcbnew", "kicad" -ErrorAction SilentlyContinue)
    $editorRule = "exactly one running KiCad app (KiCad, or a stand-alone PCB Editor)"
}
if ($pcbnewProcesses.Count -ne 1) {
    Stop-Preflight "pcbnew_instance_count" "Live mode requires $editorRule; found $($pcbnewProcesses.Count)."
}

$statusScript = Join-Path $PSScriptRoot "kicad_ipc_status.py"
$ipcRun = Invoke-Console $livePython @($statusScript)
$ipcOutput = $ipcRun.Lines
$ipcExit = $ipcRun.ExitCode
foreach ($line in $ipcOutput) {
    Write-Output "ipc_$line"
}
if ($ipcExit -ne 0 -or -not ($ipcOutput -contains "status=ready")) {
    Stop-Preflight "ipc_not_ready" "KiCad IPC is not ready. Close modal dialogs and verify that the intended board is open."
}

# The editor doing Live work must be the KiCad whose version passed above, not an older one still open.
$liveVersionLines = @($ipcOutput | Where-Object { $_ -like "kicad_version=*" })
if ($liveVersionLines.Count -ne 1) {
    Stop-Preflight "ipc_identity_missing" "IPC did not report the running KiCad's version."
}
$liveVersion = $liveVersionLines[0].Substring("kicad_version=".Length)
# Exactly the stable release that passed above, so a test build of the same numbers does not pass either.
if ($IsLinux) {
    $liveMatch = ($liveVersion -match '^(\d+\.\d+\.\d+)(?:[+-].*)?$') -and ($liveVersion -notmatch $prereleasePattern)
    $liveBaseVersion = if ($liveMatch) { $Matches[1] } else { "" }
    if (-not $liveMatch -or $liveBaseVersion -cne $offlineBaseVersion) {
        Stop-Preflight "live_kicad_version_mismatch" "The running KiCad is $liveVersion, but the installation checked above is $($offlineParts[1]). Close that KiCad and open the project in $($offlineParts[1])."
    }
} elseif ($liveVersion -cne $offlineParts[1]) {
    Stop-Preflight "live_kicad_version_mismatch" "The running KiCad is $liveVersion, but the installation checked above is $($offlineParts[1]). Close that KiCad and open the project in $($offlineParts[1])."
}

$liveBoardLine = $ipcOutput | Where-Object { $_ -like "board_filename=*" } | Select-Object -First 1
if (-not $liveBoardLine) {
    Stop-Preflight "ipc_identity_missing" "IPC did not report the open board filename."
}
$liveBoard = "$liveBoardLine".Substring("board_filename=".Length)
if ($liveBoard -ne [IO.Path]::GetFileName($identity.BoardFile)) {
    Stop-Preflight "wrong_live_board" "Expected '$([IO.Path]::GetFileName($identity.BoardFile))' but IPC reports '$liveBoard'."
}

$liveProjectLine = $ipcOutput | Where-Object { $_ -like "project_path=*" } | Select-Object -First 1
if (-not $liveProjectLine) {
    Stop-Preflight "ipc_identity_missing" "IPC did not report the open project path."
}
$liveProjectRaw = "$liveProjectLine".Substring("project_path=".Length)
$liveProjectPath = [IO.Path]::GetFullPath($liveProjectRaw)
$acceptedLivePaths = @(
    [IO.Path]::GetFullPath($identity.ProjectFile),
    [IO.Path]::GetFullPath($identity.Directory)
)
if (-not ($acceptedLivePaths | Where-Object { $_.Equals($liveProjectPath, [StringComparison]::OrdinalIgnoreCase) })) {
    Stop-Preflight "wrong_live_project" "Expected '$($identity.ProjectFile)' but IPC reports '$liveProjectRaw'."
}

Write-Output "mode=Live"
Write-Output "live_python=$livePython"
Write-Output "live_identity=python-$($liveParts[0]) kicad-python-$($liveParts[1]) protobuf-$($liveParts[2]) pynng-$($liveParts[3])"
Write-Output "status=ready"
