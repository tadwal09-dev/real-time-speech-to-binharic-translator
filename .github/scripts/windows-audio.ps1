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

# The Hi-Fi Cable is "bit-perfect": it never resamples, so both of its ends must use
# the same rate. Out of the box its playback end is 48 kHz and its recording end
# 44.1 kHz, and everything sent through it arrives warped (drifting, with dropped
# chunks). Set the recording end to 48 kHz the way the Sound control panel does
# (Recording -> Properties -> Advanced -> Default Format): IPolicyConfig.
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
[Guid("f8679f50-850a-41cf-9c72-430f290290c8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPolicyConfig {
  [PreserveSig] int GetMixFormat([MarshalAs(UnmanagedType.LPWStr)] string id, out IntPtr format);
  [PreserveSig] int GetDeviceFormat([MarshalAs(UnmanagedType.LPWStr)] string id, [MarshalAs(UnmanagedType.Bool)] bool deflt, out IntPtr format);
  [PreserveSig] int ResetDeviceFormat([MarshalAs(UnmanagedType.LPWStr)] string id);
  [PreserveSig] int SetDeviceFormat([MarshalAs(UnmanagedType.LPWStr)] string id, IntPtr endpointFormat, IntPtr mixFormat);
}
[ComImport, Guid("870af99c-171d-4f9e-af0d-e63df40c2bc9")] class CPolicyConfigClient {}
public static class EndpointFormat {
  // returns "old rate -> new rate" or an error
  public static string SetRate(string id, int rate) {
    var pc = (IPolicyConfig)new CPolicyConfigClient();
    IntPtr cur;
    int hr = pc.GetDeviceFormat(id, false, out cur);
    if (hr != 0) return "GetDeviceFormat failed 0x" + hr.ToString("x8");
    short cbSize = Marshal.ReadInt16(cur, 16);
    int size = 18 + cbSize;
    byte[] fmt = new byte[size];
    Marshal.Copy(cur, fmt, 0, size);
    int old = BitConverter.ToInt32(fmt, 4);
    short blockAlign = BitConverter.ToInt16(fmt, 12);
    BitConverter.GetBytes(rate).CopyTo(fmt, 4);               // nSamplesPerSec
    BitConverter.GetBytes(rate * blockAlign).CopyTo(fmt, 8);  // nAvgBytesPerSec
    IntPtr p = Marshal.AllocHGlobal(size);
    Marshal.Copy(fmt, 0, p, size);
    hr = pc.SetDeviceFormat(id, p, p);
    Marshal.FreeHGlobal(p);
    return hr == 0 ? (old + " Hz -> " + rate + " Hz") : ("SetDeviceFormat failed 0x" + hr.ToString("x8"));
  }
}
"@
$desc = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"  # PKEY_Device_DeviceDesc
$mm = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio"
foreach ($e in @(@{Kind = "Capture"; Flow = "1"; Name = "Hi-Fi Cable Output"}, @{Kind = "Render"; Flow = "0"; Name = "Hi-Fi Cable Input"})) {
  $key = Get-ChildItem "$mm\$($e.Kind)" | Where-Object {
    (Get-ItemProperty "$($_.PSPath)\Properties" -ErrorAction SilentlyContinue).$desc -eq $e.Name } | Select-Object -First 1
  if ($key) {
    $id = "{0.0.$($e.Flow).00000000}.$($key.PSChildName)"
    Write-Host "$($e.Name): $([EndpointFormat]::SetRate($id, 48000))"
  } else {
    Write-Host "!! endpoint '$($e.Name)' not found"
  }
}
Restart-Service Audiosrv -Force
Start-Sleep 3
Get-CimInstance Win32_SoundDevice | Format-Table Name, Status -AutoSize
