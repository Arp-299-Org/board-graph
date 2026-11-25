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

    [switch]$AllowHistoricalV2
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$jobRoot = Join-Path $repoRoot "tmp\kicad-background"
[IO.Directory]::CreateDirectory($jobRoot) | Out-Null

$jobId = "{0}-{1}" -f (Get-Date -Format "yyyyMMdd-HHmmss"), ([guid]::NewGuid().ToString("N").Substring(0, 8))
$jobPath = Join-Path $jobRoot "$jobId.json"
$stdoutPath = Join-Path $jobRoot "$jobId.stdout.log"
$stderrPath = Join-Path $jobRoot "$jobId.stderr.log"
$statusPath = Join-Path $jobRoot "$jobId.status.json"
$child = Join-Path $PSScriptRoot "kicad_run_background_child.ps1"
$runner = Join-Path $PSScriptRoot "kicad_run.ps1"

if (-not (Test-Path -LiteralPath $child -PathType Leaf)) {
    throw "KiCad background child script is missing: $child"
}

$job = [ordered]@{
    schema_version = 1
    runner = $runner
    mode = $Mode
    project = $Project
    script = $Script
    script_args = @($ScriptArgs)
    allow_historical_v2 = [bool]$AllowHistoricalV2
    status_path = $statusPath
}
$job | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $jobPath -Encoding utf8

$pwsh = (Get-Process -Id $PID).Path
$childArguments = @("-NoLogo", "-NoProfile", "-NonInteractive", "-File", $child, "-Job", $jobPath)
if ($IsWindows) {
    $process = Start-Process `
        -FilePath $pwsh `
        -ArgumentList $childArguments `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -PassThru
    $processId = $process.Id
} else {
    # macOS has no window to hide. Start-Process with redirected output loses
    # the child's output there (Python exits 120, unable to flush stdout), so
    # the shell starts the job under nohup, which also lets it outlive the
    # session that launched it.
    $launch = 'out=$1; err=$2; shift 2; nohup "$@" >"$out" 2>"$err" </dev/null & echo $!'
    $processId = [int](& /bin/sh -c $launch sh $stdoutPath $stderrPath $pwsh @childArguments)
}

[pscustomobject]@{
    job_id = $jobId
    process_id = $processId
    status = "running"
    stdout = $stdoutPath
    stderr = $stderrPath
    result = $statusPath
} | ConvertTo-Json -Compress
