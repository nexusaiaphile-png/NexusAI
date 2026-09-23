$ErrorActionPreference = "Stop"
Write-Host "NexusAI Edge Agent installer"
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw "Python 3 is required. Install Python 3.11+ and run this installer again." }
$dir = "$env:LOCALAPPDATA\NexusAI\EdgeAgent"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/nexusaiaphile-png/NexusAI/main/edge_agent/agent.py" -OutFile "$dir\agent.py"
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/nexusaiaphile-png/NexusAI/main/edge_agent/requirements.txt" -OutFile "$dir\requirements.txt"
python -m pip install --upgrade pip
python -m pip install -r "$dir\requirements.txt"
Write-Host "NexusAI Edge Agent installed."
Write-Host "Start it with: python $dir\agent.py"
