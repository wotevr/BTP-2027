<#
    Self-healing public tunnel for the BTP-2027 dashboard.

        .\tunnel.ps1              expose http://localhost:8000 publicly
        .\tunnel.ps1 -Port 5173   expose something else

    WHY THIS EXISTS
    ---------------
    `cloudflared tunnel --url ...` gives a free public URL with no account, but
    the tunnel is throwaway: Cloudflare expires it after a few hours. When that
    happens cloudflared does NOT recover - it sits in an infinite retry loop
    against a tunnel id that no longer exists:

        ERR Register tunnel error from server side error="Unauthorized: Tunnel not found"
        INF Retrying connection in up to 1m4s

    The process looks alive, the URL is dead, and nothing tells you. This script
    watches for that state, kills the stale process and starts a fresh tunnel,
    then writes the new address to tunnel-url.txt so there is always one place
    to look for the current link.

    The hostname CHANGES on every restart. That is inherent to free quick
    tunnels - for a fixed address you need a (free) Cloudflare account and a
    named tunnel. For a live demo, prefer http://localhost:8000.
#>
param(
    [int]$Port = 8000,
    [int]$CheckSeconds = 30
)

$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
$exe = Join-Path $root "cloudflared.exe"
$log = Join-Path $root "tunnel.log"
$urlFile = Join-Path $root "tunnel-url.txt"

function Say($m, $c = "Cyan") { Write-Host "  $m" -ForegroundColor $c }

if (-not (Test-Path $exe)) {
    Say "Downloading cloudflared (one time, ~55 MB)..."
    Invoke-WebRequest -UseBasicParsing `
        -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" `
        -OutFile $exe
}

# Refuse to start if there is nothing to expose - otherwise the tunnel comes up
# and every visitor gets a 502 with no clue why.
try {
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "http://127.0.0.1:$Port/health" | Out-Null
} catch {
    Say "Nothing is serving on port $Port." "Red"
    Say "Start the backend first:  .\run.ps1" "Red"
    exit 1
}

function Start-Tunnel {
    Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 1
    Remove-Item $log -ErrorAction SilentlyContinue

    Start-Process -FilePath $exe `
        -ArgumentList "tunnel", "--url", "http://127.0.0.1:$Port", "--no-autoupdate" `
        -RedirectStandardError $log -RedirectStandardOutput "$log.out" `
        -WindowStyle Hidden

    # The URL is printed a few seconds after the process starts.
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 2
        if (Test-Path $log) {
            $m = Select-String -Path $log -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" `
                 -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($m) { return $m.Matches[0].Value }
        }
    }
    return $null
}

$url = Start-Tunnel
if (-not $url) { Say "Tunnel did not come up. See $log" "Red"; exit 1 }

Set-Content -Path $urlFile -Value $url -Encoding utf8
Write-Host ""
Say "PUBLIC URL:  $url" "Green"
Say "also saved to tunnel-url.txt" "DarkGray"
Say "watching - will restart automatically if Cloudflare expires it" "DarkGray"
Write-Host ""

# ----------------------------------------------------------------- watchdog
while ($true) {
    Start-Sleep -Seconds $CheckSeconds

    $dead = $false
    if (-not (Get-Process cloudflared -ErrorAction SilentlyContinue)) {
        $dead = $true
    } elseif (Test-Path $log) {
        # The expiry signature. Only the tail matters: these lines appear once
        # the tunnel id is gone, and keep repeating forever.
        $tail = Get-Content $log -Tail 12 -ErrorAction SilentlyContinue
        if ($tail -match "Unauthorized: Tunnel not found") { $dead = $true }
    }

    if (-not $dead) { continue }

    Say "$(Get-Date -Format 'HH:mm:ss')  tunnel expired - restarting..." "Yellow"
    $url = Start-Tunnel
    if ($url) {
        Set-Content -Path $urlFile -Value $url -Encoding utf8
        Say "NEW PUBLIC URL:  $url" "Green"
    } else {
        Say "restart failed; will retry" "Red"
    }
}
