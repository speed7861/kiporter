# Kiporter - Windows installer
#  * finds (or installs) Python 3.9+
#  * creates a private virtual environment in %LOCALAPPDATA%\Kiporter
#  * installs dependencies
#  * creates Desktop + Start Menu shortcuts (with the Kiporter logo) and launches the app
$ErrorActionPreference = 'Stop'
$AppDir  = $PSScriptRoot
$DataDir = Join-Path $env:LOCALAPPDATA 'Kiporter'
$Venv    = Join-Path $DataDir 'venv'

function Find-Python {
    foreach ($cand in @(@('py', '-3'), @('python'), @('python3'))) {
        $exe = $cand[0]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $pyArgs = @($cand | Select-Object -Skip 1) + @('-c', 'import sys,tkinter;print(sys.executable) if sys.version_info>=(3,9) else exit(1)')
        try {
            $out = & $exe @pyArgs 2>$null
            if ($LASTEXITCODE -eq 0 -and $out -and ($out -notmatch 'WindowsApps')) { return ($out | Select-Object -Last 1).Trim() }
        } catch { }
    }
    return $null
}

Write-Host '== Kiporter setup ==' -ForegroundColor Cyan
$py = Find-Python
if (-not $py) {
    Write-Host 'Python 3.9+ not found - installing Python 3.12 for the current user via winget...'
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $py = Find-Python
    if (-not $py) { throw 'Python install failed. Install Python 3.12 from python.org (tick "tcl/tk") and run Install.bat again.' }
}
Write-Host "Using Python: $py"

New-Item -ItemType Directory -Force $DataDir | Out-Null
if (-not (Test-Path (Join-Path $Venv 'Scripts\python.exe'))) {
    Write-Host 'Creating virtual environment...'
    & $py -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create virtual environment.' }
}
$vpy  = Join-Path $Venv 'Scripts\python.exe'
$vpyw = Join-Path $Venv 'Scripts\pythonw.exe'

Write-Host 'Installing dependencies...'
& $vpy -m pip install --disable-pip-version-check -q --upgrade pip
& $vpy -m pip install --disable-pip-version-check -q -r (Join-Path $AppDir 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install failed (check your internet connection / proxy).' }

$icon = Join-Path $AppDir 'kiporter\assets\logo.ico'
$shell = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$programs = [Environment]::GetFolderPath('Programs')
foreach ($dir in @($desktop, $programs)) {   # remove shortcuts from the pre-Kiporter name
    $old = Join-Path $dir 'KiCad Part Importer.lnk'
    if (Test-Path $old) { Remove-Item $old }
}
foreach ($lnkPath in @((Join-Path $desktop 'Kiporter.lnk'), (Join-Path $programs 'Kiporter.lnk'))) {
    $lnk = $shell.CreateShortcut($lnkPath)
    $lnk.TargetPath = $vpyw
    $lnk.Arguments = '"' + (Join-Path $AppDir 'run.pyw') + '"'
    $lnk.WorkingDirectory = $AppDir
    $lnk.Description = 'Kiporter - search suppliers and import parts into KiCad'
    if (Test-Path $icon) { $lnk.IconLocation = "$icon,0" }
    $lnk.Save()
    Write-Host "Shortcut: $lnkPath"
}

Write-Host 'Setup complete - launching Kiporter.' -ForegroundColor Green
Start-Process -FilePath $vpyw -ArgumentList ('"' + (Join-Path $AppDir 'run.pyw') + '"') -WorkingDirectory $AppDir
