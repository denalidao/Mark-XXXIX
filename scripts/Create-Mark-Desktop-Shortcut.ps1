# Creates "Mark XXXIX.lnk" on the user's Desktop pointing at Start-Mark.bat.
# Run once:  powershell -ExecutionPolicy Bypass -File scripts\Create-Mark-Desktop-Shortcut.ps1

$ErrorActionPreference = "Stop"
# This file lives in Mark-XXXIX\scripts — repo root is parent of $PSScriptRoot
$repoRoot = Split-Path -Parent $PSScriptRoot
$bat = Join-Path $repoRoot "Start-Mark.bat"
$mainPy = Join-Path $repoRoot "main.py"
if (-not (Test-Path $mainPy)) {
    Write-Error "main.py not found under repo root: $repoRoot"
}
if (-not (Test-Path $bat)) {
    Write-Error "Start-Mark.bat not found: $bat"
}

$desktop = [Environment]::GetFolderPath("Desktop")
if (-not $desktop) {
    Write-Error "Could not resolve Desktop folder."
}

$lnkPath = Join-Path $desktop "Mark XXXIX.lnk"
$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnkPath)
$sc.TargetPath = $bat
$sc.WorkingDirectory = $repoRoot
$sc.WindowStyle = 1
$sc.Description = "Mark XXXIX (JARVIS) - local launcher"
$sc.IconLocation = "$env:SystemRoot\System32\shell32.dll,137"
$sc.Save()

Write-Host "Created: $lnkPath"
Write-Host "Target:  $bat"
