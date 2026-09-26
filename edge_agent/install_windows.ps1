$ErrorActionPreference = "Stop"
Write-Host "NexusAI Local Security Service"
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw "Python 3.11+ is required." }
$dir = "$env:LOCALAPPDATA\NexusAI\EdgeAgent"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/nexusaiaphile-png/NexusAI/main/edge_agent/agent.py" -OutFile "$dir\agent.py"
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/nexusaiaphile-png/NexusAI/main/edge_agent/requirements.txt" -OutFile "$dir\requirements.txt"
if (-not (Test-Path "$dir\.venv\Scripts\python.exe")) { python -m venv "$dir\.venv" }
& "$dir\.venv\Scripts\python.exe" -m pip install --upgrade pip
& "$dir\.venv\Scripts\python.exe" -m pip install -r "$dir\requirements.txt"
$existingToken = ""
if (Test-Path "$dir\.env") {
  $line = Get-Content "$dir\.env" | Where-Object { $_ -like "NEXUSAI_EDGE_TOKEN=*" } | Select-Object -First 1
  if ($line) { $existingToken = $line.Substring("NEXUSAI_EDGE_TOKEN=".Length) }
}
@"
NEXUSAI_API_URL=https://nexusai-worker.onrender.com
NEXUSAI_EDGE_TOKEN=$existingToken
LOCAL_AGENT_HOST=0.0.0.0
LOCAL_AGENT_PORT=8787
"@ | Set-Content -Encoding UTF8 "$dir\.env"
if ([string]::IsNullOrWhiteSpace($existingToken)) { Write-Warning "Edge Agent cloud token is not configured; cloud authentication will remain OFFLINE until provisioned." }
$startup = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup"
$shortcut = "$startup\NexusAI Local Security Service.lnk"
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($shortcut)
$sc.TargetPath = "$dir\.venv\Scripts\python.exe"
$sc.Arguments = $dir + "\agent.py"
$sc.WorkingDirectory = $dir
$sc.WindowStyle = 7
$sc.Save()
Start-Process "$dir\.venv\Scripts\python.exe" -ArgumentList ($dir + "\agent.py") -WorkingDirectory $dir -WindowStyle Hidden
Write-Host "NexusAI Local Security Service installed and started."
