param()

$ErrorActionPreference = "Stop"
$InstallDir = "$env:ProgramFiles\NexusAI\SecurityBox"
$Exe = "$InstallDir\NexusAI-SecurityBox-Service.exe"

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

if (-not (Test-Path $Exe)) {
    throw "NexusAI-SecurityBox-Service.exe was not found in the installation package."
}

& $Exe install
sc.exe config NexusAISecurityBox start= delayed-auto | Out-Null
sc.exe failure NexusAISecurityBox reset= 900 actions= restart/60000/restart/120000/restart/300000 | Out-Null
& $Exe start

Write-Host "NexusAI Security Box installed and started."
