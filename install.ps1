# Windows installer. In PowerShell, from the repo folder:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#   powershell -ExecutionPolicy Bypass -File .\install.ps1 -Uninstall
# `binharic update` runs it with -Update: only binharic itself is reinstalled, and `binharic fix` is skipped.
param([switch]$Uninstall, [switch]$NoGpu, [switch]$Update)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Data = Join-Path $env:LOCALAPPDATA "binharic"
$Venv = Join-Path $Data "venv"
$Bin  = Join-Path $Data "bin"
$Exe  = Join-Path $Venv "Scripts\binharic.exe"
$Py   = Join-Path $Venv "Scripts\python.exe"
$Shortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "Binharic.lnk"

function Step($t) { Write-Host "`n:: $t" -ForegroundColor Red }
function Retry([scriptblock]$cmd) {
  for ($i = 1; $i -le 3; $i++) { & $cmd; if ($LASTEXITCODE -eq 0) { return $true }; Write-Host "   retrying ($i/3)..."; Start-Sleep 3 }
  return $false
}

if ($Uninstall) {
  Remove-Item -Recurse -Force $Venv, $Bin, $Shortcut -ErrorAction SilentlyContinue
  Write-Host "removed (speech models are still in $Data - delete that folder to free the space)"
  exit 0
}

Step "uv (Python manager)"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
  $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
}
uv python install 3.12
if (-not (Test-Path $Py)) { uv venv --python 3.12 $Venv }

Step "installing binharic + Whisper speech recognition (a few hundred MB)"
$env:UV_HTTP_TIMEOUT = "600"
# always rebuild binharic itself from this folder; an update (-Update) leaves every other
# package alone, a fresh install upgrades them
$Pip = @("pip", "install", "--python", $Py, "--reinstall-package", "binharic")
if (-not $Update) { $Pip += "--upgrade" }
Remove-Item -Recurse -Force build -ErrorAction SilentlyContinue
if (-not (Retry { uv @Pip ".[all]" })) {
  Write-Host "!! Whisper didn't install; installing the lighter Vosk engine instead"
  uv @Pip ".[vosk]"
  if ($LASTEXITCODE -ne 0) { Write-Host "!! installation failed (see the error above)"; exit 1 }
}
# which commit this is: `binharic update` compares it with the clone
$Commit = (git rev-parse HEAD 2>$null)
if ($LASTEXITCODE -eq 0 -and $Commit) { Set-Content -Encoding ascii (Join-Path $Venv "binharic-commit") $Commit }
else { Remove-Item (Join-Path $Venv "binharic-commit") -ErrorAction SilentlyContinue }

Step "command and Start-menu shortcut"
New-Item -ItemType Directory -Force $Bin | Out-Null
Set-Content -Encoding ascii (Join-Path $Bin "binharic.cmd") "@`"$Exe`" %*"
$userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
if (($userPath -split ";") -notcontains $Bin) {
  [Environment]::SetEnvironmentVariable("PATH", "$Bin;$userPath", "User")
  $env:PATH = "$Bin;$env:PATH"
  Write-Host "   added $Bin to your PATH (new terminals will have 'binharic')"
}
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($Shortcut); $lnk.TargetPath = $Exe; $lnk.Arguments = "gui --passthrough"
$lnk.WorkingDirectory = $Data; $lnk.Save()

Step "speech models"
Retry { & $Exe download-model } | Out-Null

if (-not $Update) {  # `binharic update`: the setup was already checked when it was installed
  Step "finding and fixing problems (it will ask you to talk)"
  if ($NoGpu) { & $Exe fix --no-gpu } else { & $Exe fix }
}

if ($Update) { & $Exe --version; Write-Host "`nUpdated. You can close this window."; Start-Sleep 4; exit 0 }

Write-Host @"

Done. Try it:   binharic test
Then:           binharic          (opens the window)
For Discord you need VB-Audio Virtual Cable (free, https://vb-audio.com/Cable/):
binharic plays into 'CABLE Input'; pick 'CABLE Output' as your mic in Discord.
"@
