# On Windows a console program started straight from the agent's shell can open
# a window on the user's screen (importing pcbnew in Windows KiCad Python does).
# Every program the KiCad scripts start goes through here: on Windows with no
# window and its output captured; macOS has no such window, so there programs
# start as they always did. Loaded by kicad_preflight.ps1 and setup_kicad_ipc.ps1.
function Invoke-Console {
    param([string]$FilePath, [string[]]$Arguments)

    if (-not $IsWindows) {
        $output = @(& $FilePath @Arguments 2>&1 | ForEach-Object { "$_" })
        return [PSCustomObject]@{
            ExitCode = $LASTEXITCODE
            Stdout = $output -join "`n"
            Lines = @($output | Where-Object { $_ -ne "" })
        }
    }

    $info = [System.Diagnostics.ProcessStartInfo]::new($FilePath)
    foreach ($argument in $Arguments) {
        $info.ArgumentList.Add($argument)
    }
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::Start($info)
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()

    [PSCustomObject]@{
        ExitCode = $process.ExitCode
        Stdout = $stdout.Result
        Lines = @("$($stdout.Result)`n$($stderr.Result)" -split "\r?\n" | Where-Object { $_ -ne "" })
    }
}
