# Run only on the receiver, in an Administrator PowerShell window.
$ErrorActionPreference = "Stop"
$fleetRoot = Split-Path $PSScriptRoot -Parent
$fleetConfigPath = Join-Path $fleetRoot "config.receiver.local.json"
if (-not (Test-Path -LiteralPath $fleetConfigPath)) {
    $fleetConfigPath = Join-Path $fleetRoot "config.receiver.example.json"
}
$fleetPort = (Get-Content -LiteralPath $fleetConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json).port
if ($fleetPort -lt 1024 -or $fleetPort -gt 65535) { throw "Invalid receiver UDP port" }
$fleetRuleName = "LionFleet-LAN-UDP"
Get-NetFirewallRule -Name $fleetRuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -Name $fleetRuleName -DisplayName "LionFleet LAN UDP" -Direction Inbound -Action Allow -Protocol UDP -LocalPort $fleetPort -RemoteAddress LocalSubnet -Profile Private
Write-Host "Allowed LAN UDP port $fleetPort on Private networks."
