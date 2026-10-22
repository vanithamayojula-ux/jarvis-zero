<#
.SYNOPSIS
  One-time setup so your phone can call JARVIS Zero over your own local network (free, no phone number).

.DESCRIPTION
  1. Shows this laptop's IP address on every active network adapter, and the SIP address to dial for each.
  2. Asks Windows (UAC prompt) for permission, then adds INBOUND firewall rules that let JARVIS's Python
     receive SIP (UDP+TCP 5062) and call audio (RTP, UDP 40000-40101) on PRIVATE networks only, and only from the
     local subnet. Public networks such as hostel/cafe Wi-Fi stay closed.
  3. Explains how to turn on Windows Mobile Hotspot so your phone can reach the laptop directly.

  Run it yourself, once:  right-click > "Run with PowerShell", or
      powershell -ExecutionPolicy Bypass -File scripts\setup-phone.ps1
  Undo:  powershell -ExecutionPolicy Bypass -File scripts\setup-phone.ps1 -Remove

.PARAMETER InfoOnly
  Only print addresses and instructions; change nothing (no UAC prompt).
.PARAMETER Remove
  Delete the firewall rules this script created.
.PARAMETER WindowsHotspotRule
  Also allow devices on the Windows Mobile Hotspot subnet (192.168.137.0/24 only) when Windows classifies the
  hotspot adapter as a Public network. Asked interactively if not given.
#>
param(
    [switch]$InfoOnly,
    [switch]$Remove,
    [switch]$WindowsHotspotRule,
    [switch]$Elevated
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$RulePrefix = "JARVIS Zero phone"
$SipPort = 5062
$RtpPorts = "40000-40101"     # telephony.rtp_port_min..rtp_port_max (+1 for RTCP)
$HotspotSubnet = "192.168.137.0/24"

function Read-Telephony {
    $settingsFile = Join-Path $Root "data\settings.json"
    if (Test-Path $settingsFile) {
        try {
            $tel = (Get-Content $settingsFile -Raw | ConvertFrom-Json).telephony
            if ($tel.sip_port) { $script:SipPort = [int]$tel.sip_port }
            if ($tel.rtp_port_min -and $tel.rtp_port_max) {
                $script:RtpPorts = "$([int]$tel.rtp_port_min)-$([int]$tel.rtp_port_max + 1)"
            }
        } catch { }
    }
}

function Get-PythonPrograms {
    # The venv's python.exe is only a launcher: on Windows the process that actually owns the sockets is the base
    # interpreter it starts. Firewall program rules must name that one, so we allow both.
    $venvPy = Join-Path $Root ".venv\Scripts\python.exe"
    $list = @()
    if (Test-Path $venvPy) {
        $list += (Resolve-Path $venvPy).Path
        try {
            $base = & $venvPy -c "import sys, os; print(os.path.realpath(getattr(sys, '_base_executable', sys.executable)))"
            if ($base -and (Test-Path $base.Trim())) { $list += $base.Trim() }
        } catch { }
    }
    return $list | Select-Object -Unique
}

function Show-Addresses {
    Write-Host ""
    Write-Host "=== This laptop's addresses ===" -ForegroundColor Cyan
    $profiles = @{}
    try { Get-NetConnectionProfile | ForEach-Object { $profiles[$_.InterfaceIndex] = $_ } } catch { }
    $found = $false
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
        ForEach-Object {
            $found = $true
            $p = $profiles[$_.InterfaceIndex]
            $category = if ($p) { "$($p.NetworkCategory) network '$($p.Name)'" } else { "no network profile (usually treated as Public)" }
            $hint = ""
            if ($_.IPAddress -like "192.168.137.*") { $hint = "  <- Windows Mobile Hotspot (phones that join it can call)" }
            Write-Host ("  {0,-30} {1,-16} {2}{3}" -f $_.InterfaceAlias, $_.IPAddress, $category, $hint)
            if ($hint -or ($p -and $p.NetworkCategory -eq "Private")) {
                Write-Host ("      dial from the phone:  sip:jarvis@{0}:{1}" -f $_.IPAddress, $SipPort) -ForegroundColor Green
            } else {
                Write-Host ("      (Public network: stays closed to calls - use a hotspot, see below)") -ForegroundColor DarkGray
            }
        }
    if (-not $found) { Write-Host "  No active network adapter with an IPv4 address." -ForegroundColor Yellow }
}

function Show-HotspotHelp {
    Write-Host ""
    Write-Host "=== Why a hotspot? ===" -ForegroundColor Cyan
    Write-Host "  Hostel / campus / cafe Wi-Fi (like 'LPU Hostels-5G') almost always has client isolation: devices on"
    Write-Host "  it cannot reach each other, so your phone can't reach the laptop there. A personal hotspot fixes"
    Write-Host "  that for free (calls stay on the local link; no mobile data is used for the call itself)."
    Write-Host ""
    Write-Host "  Option A (recommended) - the phone joins the LAPTOP's hotspot:" -ForegroundColor White
    Write-Host "    1. Windows Settings > Network & internet > Mobile hotspot."
    Write-Host "    2. 'Share my internet connection from': Wi-Fi.  'Share over': Wi-Fi."
    Write-Host "    3. Properties > Edit: note the network name and password. Turn Mobile hotspot ON."
    Write-Host "    4. On the phone, join that Wi-Fi. The laptop is then 192.168.137.1 for the phone."
    Write-Host "    5. Dial  sip:jarvis@192.168.137.1:$SipPort  from Linphone."
    Write-Host "    (If the toggle is greyed out, your Wi-Fi card can't host while connected; use option B.)"
    Write-Host ""
    Write-Host "  Option B - the laptop joins the PHONE's hotspot:" -ForegroundColor White
    Write-Host "    1. Turn on the phone's hotspot; connect the laptop to it. When Windows asks, choose"
    Write-Host "       'Private network' (or Settings > Network > that Wi-Fi > Network profile type: Private)."
    Write-Host "    2. Re-run this script to see the laptop's new address, then add that network's subnet in the"
    Write-Host "       dashboard (Calls tab > Allowed networks), e.g. 172.20.10.0/28 for an iPhone hotspot."
    Write-Host "    Note: the laptop's other internet traffic will then use the phone's mobile data."
    Write-Host ""
}

function Remove-Rules {
    Get-NetFirewallRule -DisplayName "$RulePrefix*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    Write-Host "Removed firewall rules named '$RulePrefix*'." -ForegroundColor Green
}

function Add-Rules {
    $programs = Get-PythonPrograms
    if (-not $programs) { throw "Could not find .venv\Scripts\python.exe under $Root. Run install.ps1 first." }
    Remove-Rules | Out-Null
    $ports = @("$SipPort", $RtpPorts)
    foreach ($prog in $programs) {
        Write-Host "  allowing $prog"
        New-NetFirewallRule -DisplayName "$RulePrefix (UDP)" -Description "JARVIS Zero: SIP + RTP from your own phone on a private network" `
            -Direction Inbound -Action Allow -Program $prog -Protocol UDP -LocalPort $ports `
            -Profile Private -RemoteAddress LocalSubnet | Out-Null
        New-NetFirewallRule -DisplayName "$RulePrefix (TCP)" -Description "JARVIS Zero: SIP over TCP from your own phone on a private network" `
            -Direction Inbound -Action Allow -Program $prog -Protocol TCP -LocalPort $SipPort `
            -Profile Private -RemoteAddress LocalSubnet | Out-Null
    }
    Write-Host "Added inbound rules (Private networks, local subnet only): UDP $SipPort,$RtpPorts and TCP $SipPort." -ForegroundColor Green

    # Windows Mobile Hotspot adapter: Windows often gives it no Private profile. Offer a rule scoped to its subnet only.
    $hotspot = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -like "192.168.137.*" }
    $isPrivate = $false
    if ($hotspot) {
        try { $isPrivate = (Get-NetConnectionProfile -InterfaceIndex $hotspot[0].InterfaceIndex -ErrorAction Stop).NetworkCategory -eq "Private" } catch { }
    }
    $want = $WindowsHotspotRule
    if (-not $want -and -not $isPrivate) {
        Write-Host ""
        Write-Host "Windows Mobile Hotspot clients use $HotspotSubnet. Windows may treat that adapter as Public, which"
        Write-Host "the rules above don't cover. A separate rule can allow ONLY $HotspotSubnet (nothing from the hostel Wi-Fi)."
        $ans = Read-Host "Add the hotspot-only rule? [Y/n]"
        $want = ($ans -eq "" -or $ans -match "^[Yy]")
    }
    if ($want) {
        foreach ($prog in $programs) {
            New-NetFirewallRule -DisplayName "$RulePrefix (Windows hotspot UDP)" -Direction Inbound -Action Allow -Program $prog `
                -Protocol UDP -LocalPort $ports -Profile Any -RemoteAddress $HotspotSubnet | Out-Null
            New-NetFirewallRule -DisplayName "$RulePrefix (Windows hotspot TCP)" -Direction Inbound -Action Allow -Program $prog `
                -Protocol TCP -LocalPort $SipPort -Profile Any -RemoteAddress $HotspotSubnet | Out-Null
        }
        Write-Host "Added hotspot-only rules (remote address $HotspotSubnet only)." -ForegroundColor Green
    }
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# ------------------------------------------------------------------ main
Read-Telephony
Show-Addresses

if ($InfoOnly) {
    Show-HotspotHelp
    return
}

if (-not (Test-Admin)) {
    if ($Elevated) { throw "Administrator rights were not granted; nothing was changed." }
    Write-Host ""
    Write-Host "Windows will now ask for permission (UAC) to add the firewall rules..." -ForegroundColor Yellow
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-Elevated")
    if ($Remove) { $argList += "-Remove" }
    if ($WindowsHotspotRule) { $argList += "-WindowsHotspotRule" }
    try {
        $proc = Start-Process -FilePath "powershell.exe" -ArgumentList $argList -Verb RunAs -Wait -PassThru
        if ($proc.ExitCode -eq 0) { Write-Host "Firewall step finished." -ForegroundColor Green }
        else { Write-Host "Firewall step exited with code $($proc.ExitCode)." -ForegroundColor Yellow }
    } catch {
        Write-Host "Permission was declined; no firewall rules were changed." -ForegroundColor Yellow
    }
    Show-HotspotHelp
    Write-Host "Next: open the dashboard (http://127.0.0.1:8600/#calls), enable 'SIP over local network', and call." -ForegroundColor Cyan
    return
}

try {
    if ($Remove) { Remove-Rules } else { Add-Rules }
} catch {
    Write-Host "Error: $_" -ForegroundColor Red
    if ($Elevated) { Read-Host "Press Enter to close" | Out-Null }
    exit 1
}
if ($Elevated) {
    Write-Host ""
    Read-Host "Done. Press Enter to close this window" | Out-Null
} else {
    Show-HotspotHelp
}
