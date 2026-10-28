# Windows, like a user: install with install.ps1 (Windows PowerShell, as the
# README says), then run the end-to-end suite against the installed binharic,
# then uninstall. Run .github/scripts/windows-audio.ps1 first for sound devices.
param([Parameter(ValueFromRemainingArguments = $true)] [string[]]$E2eArgs)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..\..")

Write-Host "::group::install.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -NoGpu
if ($LASTEXITCODE -ne 0) { throw "install.ps1 failed ($LASTEXITCODE)" }
Write-Host "::endgroup::"

$Data = Join-Path $env:LOCALAPPDATA "binharic"
$Py = Join-Path $Data "venv\Scripts\python.exe"
$Exe = Join-Path $Data "venv\Scripts\binharic.exe"
$Cmd = Join-Path $Data "bin\binharic.cmd"
foreach ($f in $Py, $Exe, $Cmd, (Join-Path ([Environment]::GetFolderPath("Programs")) "Binharic.lnk")) {
  if (-not (Test-Path $f)) { throw "the installer didn't create $f" }
}
& $Cmd --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "binharic.cmd --help failed" }
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH" }
uv pip install --python $Py --quiet pyte pywinpty pillow

$env:BINHARIC_E2E_CMD = $Exe
& $Py -m tests.e2e.run @E2eArgs
$code = $LASTEXITCODE

Write-Host "::group::install.ps1 -Uninstall"
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Uninstall
if (Test-Path (Join-Path $Data "venv")) { throw "uninstall left the environment behind" }
Write-Host "::endgroup::"
exit $code
