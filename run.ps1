<#
    BTP-2027 - one-command launcher (Windows / PowerShell)

        .\run.ps1              build the dashboard and serve everything on :8000
        .\run.ps1 -Dev         backend on :8000 + Vite dev server on :5173
        .\run.ps1 -Public      also open a public https:// tunnel
        .\run.ps1 -Setup       create the venv, install everything, seed the DB

    The whole product runs on ONE port in the default mode, because FastAPI
    serves the built dashboard itself. That is what makes -Public work with a
    single tunnel, WebSocket included.
#>
param(
    [switch]$Setup,
    [switch]$Dev,
    [switch]$Public,
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$root     = $PSScriptRoot
$backend  = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"
$py       = Join-Path $backend "venv\Scripts\python.exe"

function Say($msg, $colour = "Cyan") { Write-Host "  $msg" -ForegroundColor $colour }

# --------------------------------------------------------------- setup ----
if ($Setup -or -not (Test-Path $py)) {
    Say "Creating the Python virtual environment..."
    python -m venv (Join-Path $backend "venv")

    Say "Installing backend dependencies (this takes a minute)..."
    & $py -m pip install --quiet --disable-pip-version-check -r (Join-Path $backend "requirements.txt")

    if (-not (Test-Path (Join-Path $backend ".env"))) {
        Copy-Item (Join-Path $backend ".env.example") (Join-Path $backend ".env")
        Say ".env created from .env.example"
    }

    Say "Seeding danger zones, site plan and demo calibration..."
    Push-Location $backend; & $py "scripts\seed_demo_data.py"; Pop-Location
}

if (-not (Test-Path (Join-Path $frontend "node_modules"))) {
    Say "Installing frontend dependencies..."
    Push-Location $frontend; npm install --no-audit --no-fund; Pop-Location
}

# ----------------------------------------------------------------- dev ----
if ($Dev) {
    Say "Backend  -> http://localhost:$Port" "Green"
    Say "Frontend -> http://localhost:5173  (open this one)" "Green"
    Start-Process -FilePath "npm" -ArgumentList "run","dev" -WorkingDirectory $frontend
    Push-Location $backend
    & $py -m uvicorn app.main:app --reload --port $Port
    Pop-Location
    exit
}

# -------------------------------------------------------------- serve ----
Say "Building the dashboard..."
Push-Location $frontend; npm run build; Pop-Location

if ($Public) {
    $exe = Join-Path $root "cloudflared.exe"
    if (-not (Test-Path $exe)) {
        Say "Downloading cloudflared (one time, ~55 MB)..."
        Invoke-WebRequest -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -OutFile $exe
    }
    Say "Opening a public tunnel. The https:// URL appears below in a few seconds." "Yellow"
    Start-Process -FilePath $exe -ArgumentList "tunnel","--url","http://127.0.0.1:$Port","--no-autoupdate"
}

Say ""
Say "Dashboard -> http://localhost:$Port" "Green"
Say "API docs  -> http://localhost:$Port/docs" "Green"
Say "Ctrl-C to stop." "DarkGray"
Say ""

Push-Location $backend
& $py -m uvicorn app.main:app --host 0.0.0.0 --port $Port
Pop-Location
