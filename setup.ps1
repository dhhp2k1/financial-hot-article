# financial-hot-article setup: creates .venv inside this skill folder and installs dependencies.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$py = $null
foreach ($c in @("python", "py")) {
    if (Get-Command $c -ErrorAction SilentlyContinue) { $py = $c; break }
}
if (-not $py) { Write-Host "Python not found. Install Python 3.10+ from python.org first."; exit 1 }

if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
    Write-Host "[1/3] Creating .venv ..."
    & $py -m venv "$root\.venv"
} else {
    Write-Host "[1/3] .venv already exists, reusing it."
}
$vpy = "$root\.venv\Scripts\python.exe"

Write-Host "[2/3] Installing dependencies ..."
& $vpy -m pip install -U pip -q
& $vpy -m pip install -U -r "$root\requirements.txt"
if ($LASTEXITCODE -ne 0) {
    Write-Host "PyPI failed, retrying with Tsinghua mirror ..."
    & $vpy -m pip install -U -r "$root\requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
}

Write-Host "[3/3] Self test ..."
$env:PYTHONUTF8 = "1"
& $vpy "$root\scripts\mkt.py" selftest
Write-Host ""
Write-Host "Done. Dependencies live in: $root\.venv  (delete this folder to uninstall)"
