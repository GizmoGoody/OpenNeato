# Windows helper around tools/vacuula/provision.py: runs the hand-over and joins /
# leaves the robot's open setup network ("Neato-<mac>") with this PC's Wi-Fi
# adapter when the script asks for it, so a wired connection keeps the bridge
# reachable throughout. The WiFi password is read from a file and never printed.
#
#   .\run_handover.ps1 -Bridge neato.local -Server 192.168.1.10 -Ssid MyWiFi -PasswordFile C:\path\pw.txt
#
# The Wi-Fi profile for the robot's network is created on the fly (open network).
# Output is mirrored to $env:TEMP\handover-run.log.
param(
    [Parameter(Mandatory = $true)][string]$Bridge,
    [Parameter(Mandatory = $true)][string]$Server,
    [Parameter(Mandatory = $true)][string]$Ssid,
    [Parameter(Mandatory = $true)][string]$PasswordFile,
    [string]$WifiIface = "Wi-Fi",
    [string]$RobotAp = ""
)
$ErrorActionPreference = "Continue"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$log = "$env:TEMP\handover-run.log"
if (-not (Test-Path $PasswordFile)) { Write-Output "password file missing: $PasswordFile"; exit 2 }

if (-not $RobotAp) {
    # The setup network is named after the robot's MAC, the second field of its serial number.
    try {
        $st = Invoke-RestMethod -Uri "http://$Bridge/api/vacuula/status" -TimeoutSec 15
        $mac = ($st.serial -split ",")[1]
        if ($mac) { $RobotAp = "Neato-$mac" }
    } catch { }
    if (-not $RobotAp) { Write-Output "could not derive the robot's setup network name from the bridge; pass -RobotAp"; exit 2 }
}
$profileXml = @"
<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>$RobotAp</name>
  <SSIDConfig><SSID><name>$RobotAp</name></SSID></SSIDConfig>
  <connectionType>ESS</connectionType>
  <connectionMode>manual</connectionMode>
  <MSM><security><authEncryption><authentication>open</authentication><encryption>none</encryption><useOneX>false</useOneX></authEncryption></security></MSM>
</WLANProfile>
"@
$pf = Join-Path $env:TEMP "robot-ap-profile.xml"
[IO.File]::WriteAllText($pf, $profileXml)
netsh wlan add profile filename="$pf" user=current | Out-Null
Remove-Item $pf -ErrorAction SilentlyContinue
Remove-Item $log -ErrorAction SilentlyContinue

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = "python"
$psi.Arguments = "-u `"$repo\tools\vacuula\provision.py`" --bridge $Bridge --server $Server --ssid `"$Ssid`" --password-file `"$PasswordFile`""
$psi.WorkingDirectory = $repo
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.UseShellExecute = $false
$proc = [System.Diagnostics.Process]::Start($psi)

$joined = $false
$left = $false
$deadline = (Get-Date).AddMinutes(30)
$reader = $proc.StandardOutput
while (-not $proc.HasExited -or -not $reader.EndOfStream) {
    if ((Get-Date) -gt $deadline) { Write-Output "runner timeout, killing"; $proc.Kill(); break }
    $line = $reader.ReadLine()
    if ($null -eq $line) { Start-Sleep -Milliseconds 300; continue }
    Add-Content -Path $log -Value $line
    Write-Output $line
    if (-not $joined -and $line -match "Connect this computer to the robot") {
        Start-Sleep -Seconds 5
        $r = netsh wlan connect name="$RobotAp" interface="$WifiIface"
        Write-Output ("[runner] netsh connect: " + ($r -join " "))
        $joined = $true
    }
    if ($joined -and -not $left -and ($line -match "moved on|Reconnect this computer|gave up|Stopping here")) {
        $r = netsh wlan disconnect interface="$WifiIface"
        Write-Output ("[runner] netsh disconnect: " + ($r -join " "))
        $left = $true
    }
}
$err = $proc.StandardError.ReadToEnd()
if ($err) { Write-Output "[stderr] $err" }
$proc.WaitForExit()
if ($joined -and -not $left) { netsh wlan disconnect interface="$WifiIface" | Out-Null }
Write-Output "[runner] provision.py exit code: $($proc.ExitCode)"
exit $proc.ExitCode
