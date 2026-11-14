# Switch the sidecar between the live AI endpoint and the instant offline mock.
#   .\demo-mode.ps1 mock   -> deterministic draft, no network, instant
#   .\demo-mode.ps1 live   -> real AI call (falls back to mock after AI_TIMEOUT_SECONDS=20)
param([Parameter(Mandatory = $true)][ValidateSet('mock', 'live')][string]$Mode)

$envFile = Join-Path $PSScriptRoot '.env'
$value = if ($Mode -eq 'mock') { 'true' } else { 'false' }
(Get-Content $envFile) -replace '^USE_MOCK_LLM=.*', "USE_MOCK_LLM=$value" | Set-Content $envFile -Encoding ascii

Push-Location $PSScriptRoot
docker compose up -d sidecar_service | Out-Null
Pop-Location
Start-Sleep -Seconds 4
Write-Host "Sidecar mode: $Mode"
Invoke-RestMethod http://localhost:8000/health | ConvertTo-Json -Compress
