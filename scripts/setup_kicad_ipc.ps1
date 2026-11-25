[CmdletBinding(SupportsShouldProcess, ConfirmImpact = "Medium")]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$venvRoot = Join-Path $repoRoot ".venv-kicad-ipc"
$requirements = Join-Path $PSScriptRoot "kicad-ipc-requirements.txt"
# The environment is built from KiCad's own Python, which differs per platform;
# kicad_preflight.ps1 holds the KiCad version rule.
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
    throw "KiCad Python not found at '$kicadPython'. Set KICAD_PYTHON explicitly."
}
if (-not (Test-Path -LiteralPath $requirements -PathType Leaf)) {
    throw "Pinned requirements file is missing: $requirements"
}
if (Test-Path -LiteralPath $venvRoot) {
    throw "Refusing to replace existing environment: $venvRoot. Run kicad_preflight.ps1 to inspect it."
}

# Every program this script starts goes through Invoke-Console: no window on Windows.
. (Join-Path $PSScriptRoot "invoke_console.ps1")

$probe = Invoke-Console $kicadPython @("-c", "import pcbnew,sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}|{pcbnew.GetBuildVersion()}')")
$identity = $probe.Stdout.Trim()
if ($probe.ExitCode -ne 0 -or $identity -notmatch '^\d+\.\d+\.\d+\|\d+\.\d+\.\d+$') {
    throw "KiCad's Python could not report its version and a stable pcbnew release (X.Y.Z); got '$identity'."
}

$description = "create $venvRoot and install the pinned KiCad IPC packages"
if (-not $PSCmdlet.ShouldProcess($repoRoot, $description)) {
    return
}

$venv = Invoke-Console $kicadPython @("-m", "venv", $venvRoot)
if ($venv.ExitCode -ne 0) {
    throw "Virtual-environment creation failed. The incomplete directory, if present, is $venvRoot. Its output:`n$($venv.Lines -join "`n")"
}

$venvPython = if ($IsWindows) { Join-Path $venvRoot "Scripts\python.exe" } else { Join-Path $venvRoot "bin/python" }
$install = Invoke-Console $venvPython @("-m", "pip", "install", "--requirement", $requirements)
if ($install.ExitCode -ne 0) {
    throw "Package installation failed. The incomplete environment remains at $venvRoot for inspection. Its output:`n$($install.Lines -join "`n")"
}

Write-Output "status=installed"
Write-Output "live_python=$venvPython"
Write-Output "next=.\scripts\kicad_preflight.ps1 -Project board -Mode Live"
