# Give a GitHub Windows runner real (virtual) audio devices:
#   VB-Audio Virtual Cable  -> what binharic uses as the "Binharic Mic" (CABLE Input/Output)
#   VB-Audio Hi-Fi Cable    -> stands in for a microphone, so tests can "talk" into it
# Runners ship with the Windows Audio service off and no sound devices at all.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
# Settings -> Privacy & security -> Microphone: allow apps (and desktop apps) to record.
# It's off on runners, and recording then fails with "Unanticipated host error".
foreach ($root in "HKLM:", "HKCU:") {
  $k = "$root\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
  New-Item -Force -Path "$k\NonPackaged" | Out-Null
  Set-ItemProperty -Path $k -Name Value -Value Allow
  Set-ItemProperty -Path "$k\NonPackaged" -Name Value -Value Allow
}
New-Item -Force -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\AppPrivacy" | Out-Null
Set-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\AppPrivacy" -Name LetAppsAccessMicrophone -Value 1 -Type DWord

Set-Service -Name AudioEndpointBuilder -StartupType Automatic
Set-Service -Name Audiosrv -StartupType Automatic
Start-Service AudioEndpointBuilder, Audiosrv

function Trust-Drivers($dir) {
  # the setup programs install signed drivers; trust the publisher first so
  # Windows doesn't stop and ask (nobody can click "Install" on a runner)
  Get-ChildItem -Recurse $dir -Include *.cat, *.sys, *.exe | ForEach-Object {
    $sig = Get-AuthenticodeSignature $_.FullName
    if ($sig.SignerCertificate) {
      $cer = Join-Path $env:TEMP ($sig.SignerCertificate.Thumbprint + ".cer")
      Export-Certificate -Cert $sig.SignerCertificate -FilePath $cer | Out-Null
      Import-Certificate -FilePath $cer -CertStoreLocation Cert:\LocalMachine\TrustedPublisher | Out-Null
    }
  }
}

function Install-Pack($urls, $name, $setupGlob) {
  $zip = Join-Path $env:TEMP "$name.zip"; $dir = Join-Path $env:TEMP $name
  foreach ($u in $urls) {
    try { Invoke-WebRequest $u -OutFile $zip -UseBasicParsing; Write-Host "got $u"; break } catch { Write-Host "no $u" }
  }
  Expand-Archive -Force $zip $dir
  Trust-Drivers $dir
  $setup = Get-ChildItem -Recurse $dir -Filter $setupGlob | Select-Object -First 1
  Write-Host "running $($setup.FullName) -i -h"
  $p = Start-Process $setup.FullName -ArgumentList "-i", "-h" -Wait -PassThru
  Write-Host "$name setup exit code $($p.ExitCode)"
}

Install-Pack @("https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip",
               "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack43.zip") "vbcable" "VBCABLE_Setup_x64.exe"
Install-Pack @("https://download.vb-audio.com/Download_CABLE/HiFiCableAsioBridgeSetup_v1007.zip") "hificable" "HiFiCableAsioBridgeSetup*.exe"
Start-Sleep 5
Restart-Service Audiosrv -Force
Start-Sleep 3
Get-CimInstance Win32_SoundDevice | Format-Table Name, Status -AutoSize
