# Windows installer. In PowerShell, from the repo folder:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#   powershell -ExecutionPolicy Bypass -File .\install.ps1 -Uninstall
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
if (-not (Retry { uv pip install --python $Py --upgrade ".[all]" })) {
  Write-Host "!! Whisper didn't install; installing the lighter Vosk engine instead"
  uv pip install --python $Py --upgrade ".[vosk]"
}

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

Write-Host @"

Done. Try it:   binharic test
Then:           binharic          (opens the window)
For Discord you need VB-Audio Virtual Cable (free, https://vb-audio.com/Cable/):
binharic plays into 'CABLE Input'; pick 'CABLE Output' as your mic in Discord.
"@
