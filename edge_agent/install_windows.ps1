$ErrorActionPreference = "Stop"
Write-Host "NexusAI Edge Agent installer"
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw "Python 3 is required. Install Python 3.11+ and run this installer again." }
$dir = "$env:LOCALAPPDATA\NexusAI\EdgeAgent"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/nexusaiaphile-png/NexusAI/main/edge_agent/agent.py" -OutFile "$dir\agent.py"
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/nexusaiaphile-png/NexusAI/main/edge_agent/requirements.txt" -OutFile "$dir\requirements.txt"
python -m pip install --upgrade pip
$env:NEXUSAI_API_URL = "https://nexusai-worker.onrender.com"
$env:NEXUSAI_EDGE_TOKEN = ""
[Environment]::SetEnvironmentVariable("NEXUSAI_API_URL", "https://nexusai-worker.onrender.com", "User")
[Environment]::SetEnvironmentVariable("NEXUSAI_EDGE_TOKEN", "", "User")
Start-Process python -ArgumentList "$dir\agent.py" -WindowStyle Minimized
Write-Host "NexusAI Edge Agent installed and started."
Write-Host "Local health: http://127.0.0.1:8787/health"
