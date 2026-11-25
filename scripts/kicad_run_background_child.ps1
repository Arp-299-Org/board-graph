[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Job
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$exitCode = 1
$started = Get-Date
$statusPath = $null
try {
    $spec = Get-Content -LiteralPath $Job -Raw | ConvertFrom-Json
    if ($spec.schema_version -ne 1) {
        throw "Unsupported KiCad background-job schema: $($spec.schema_version)"
    }
    $statusPath = [string]$spec.status_path
    $parameters = @{
        Mode = [string]$spec.mode
        Project = [string]$spec.project
        Script = [string]$spec.script
        ScriptArgs = @($spec.script_args)
        AllowHistoricalV2 = [bool]$spec.allow_historical_v2
    }
    & ([string]$spec.runner) @parameters
    $exitCode = $LASTEXITCODE
} catch {
    Write-Error $_
    $exitCode = 1
} finally {
    if ($statusPath) {
        [ordered]@{
            schema_version = 1
            status = if ($exitCode -eq 0) { "completed" } else { "failed" }
            exit_code = $exitCode
            started_at = $started.ToString("o")
            finished_at = (Get-Date).ToString("o")
        } | ConvertTo-Json | Set-Content -LiteralPath $statusPath -Encoding utf8
    }
}

exit $exitCode
