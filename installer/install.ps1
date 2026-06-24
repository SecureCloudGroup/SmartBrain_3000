# Bootstrap (Windows): find a real Python 3 and run the installer.
# NOTE: the Python installer is the cross-platform core; this wrapper only
# launches it. (Verified on macOS/Linux; the Windows path is unverified here.)
$ErrorActionPreference = "Stop"

# Microsoft Store ships 0-byte "App Execution Alias" stubs for python/python3
# under ...\WindowsApps\ that open the Store instead of running Python. We skip
# any candidate resolving there, and prefer the Python Launcher 'py' (which is a
# real install, never a Store alias).
function Find-Python {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher -and $launcher.Source -notlike "*\WindowsApps\*") {
        return , @($launcher.Source, "-3")
    }
    foreach ($name in @("python3", "python")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -notlike "*\WindowsApps\*") {
            return , @($cmd.Source)
        }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Write-Error "Python 3 is required. Install it (https://www.python.org/downloads/ or 'winget install Python.Python.3'), then re-run."
    exit 1
}

$cmdArgs = @()
if ($python.Count -gt 1) { $cmdArgs += $python[1..($python.Count - 1)] }  # e.g. '-3' for the launcher
$cmdArgs += (Join-Path $PSScriptRoot "install.py")
$cmdArgs += $args
& $python[0] @cmdArgs
exit $LASTEXITCODE
