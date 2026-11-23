[CmdletBinding()]
param(
    [ValidateSet('Start', 'Prepare', 'Status', 'Stop')]
    [string]$Action = 'Start',
    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'

function Invoke-Checked {
    param([string]$Command, [string[]]$Arguments)
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Command exited with code $LASTEXITCODE."
    }
}

function Install-LocalEnvironment {
    param([string]$Directory, [string[]]$InstallOptions)

    $venv = Join-Path $Directory '.venv'
    $bin = Join-Path $venv 'Scripts'
    if (-not (Test-Path -LiteralPath (Join-Path $bin 'python.exe') -PathType Leaf)) {
        Invoke-Checked py @('-3.12', '-m', 'venv', $venv)
    }

    $previousVenv = $env:VIRTUAL_ENV
    $previousPath = $env:PATH
    Push-Location -LiteralPath $Directory
    try {
        $env:VIRTUAL_ENV = $venv
        $env:PATH = "$bin;$previousPath"
        Invoke-Checked poetry (@('install', '--no-root') + $InstallOptions)
    } finally {
        $env:VIRTUAL_ENV = $previousVenv
        $env:PATH = $previousPath
        Pop-Location
    }
}

$previousEnvFile = $env:P2_ENV_FILE
Push-Location -LiteralPath $PSScriptRoot
try {
    $env:P2_ENV_FILE = Join-Path $PSScriptRoot '.env'
    if (-not (Test-Path -LiteralPath $env:P2_ENV_FILE -PathType Leaf)) {
        throw 'Create .env from .env.example and fill in the local passwords. See docs/local-runbook.md.'
    }

    $compose = @('compose', '--env-file', $env:P2_ENV_FILE, '-f', 'compose.local.yml')
    switch ($Action) {
        'Status' {
            Invoke-Checked docker ($compose + @('ps', '-a'))
            try {
                $status = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 3
                Write-Host "Web: HTTP $($status.StatusCode) on port $Port"
            } catch {
                Write-Host "Web: unavailable on port $Port"
            }
            return
        }
        'Stop' {
            Invoke-Checked docker ($compose + @('stop'))
            return
        }
    }

    Install-LocalEnvironment $PSScriptRoot @('--with', 'business,local-runtime')
    Install-LocalEnvironment (Join-Path $PSScriptRoot 'flight_role_validation') @()
    Invoke-Checked docker ($compose + @('up', '-d', '--wait', '--wait-timeout', '90'))

    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    Invoke-Checked $python @('-B', 'scripts/prepare_business_db.py', 'migrate')
    Invoke-Checked $python @('-B', 'scripts/prepare_business_db.py', 'seed')
    Invoke-Checked $python @('-B', 'scripts/prepare_graph_db.py')

    if ($Action -eq 'Start') {
        Write-Host "Open http://127.0.0.1:$Port/ after Uvicorn completes startup."
        Invoke-Checked $python @(
            '-B', '-m', 'uvicorn', 'web_app.app.runtime:app',
            '--host', '127.0.0.1', '--port', "$Port", '--reload',
            '--reload-dir', 'customer_support_chat', '--reload-dir', 'web_app'
        )
    }
} finally {
    $env:P2_ENV_FILE = $previousEnvFile
    Pop-Location
}
