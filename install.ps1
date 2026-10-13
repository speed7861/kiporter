# KiCad Part Importer - automatic installer
#  * finds (or installs) Python 3.9+
#  * creates a private virtual environment in %LOCALAPPDATA%\KiCadPartImporter
#  * installs dependencies
#  * creates Desktop + Start Menu shortcuts and launches the app
$ErrorActionPreference = 'Stop'
$AppDir  = $PSScriptRoot
$DataDir = Join-Path $env:LOCALAPPDATA 'KiCadPartImporter'
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

Write-Host '== KiCad Part Importer setup ==' -ForegroundColor Cyan
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

# Icon: borrow KiCad's if available
$icon = $null
foreach ($root in @("$env:ProgramFiles\KiCad", "$env:LOCALAPPDATA\Programs\KiCad")) {
    if (Test-Path $root) {
        $k = Get-ChildItem $root -Directory | Sort-Object { [version]($_.Name -replace '[^\d.]', '') } -Descending -ErrorAction SilentlyContinue |
             ForEach-Object { Join-Path $_.FullName 'bin\kicad.exe' } | Where-Object { Test-Path $_ } | Select-Object -First 1
        if ($k) { $icon = "$k,0"; break }
    }
}

$shell = New-Object -ComObject WScript.Shell
$targets = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'KiCad Part Importer.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Programs')) 'KiCad Part Importer.lnk')
)
foreach ($lnkPath in $targets) {
    $lnk = $shell.CreateShortcut($lnkPath)
    $lnk.TargetPath = $vpyw
    $lnk.Arguments = '"' + (Join-Path $AppDir 'run.pyw') + '"'
    $lnk.WorkingDirectory = $AppDir
    $lnk.Description = 'Search LCSC / Digi-Key / Mouser and import parts into KiCad'
    if ($icon) { $lnk.IconLocation = $icon }
    $lnk.Save()
    Write-Host "Shortcut: $lnkPath"
}

Write-Host 'Setup complete - launching KiCad Part Importer.' -ForegroundColor Green
Start-Process -FilePath $vpyw -ArgumentList ('"' + (Join-Path $AppDir 'run.pyw') + '"') -WorkingDirectory $AppDir
