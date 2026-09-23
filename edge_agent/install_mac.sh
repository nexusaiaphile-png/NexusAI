#!/bin/bash
set -e
echo "NexusAI Edge Agent installer"
if ! command -v python3 >/dev/null 2>&1; then echo "Python 3 is required. Install Python 3.11+ and run again."; exit 1; fi
DIR="$HOME/Library/Application Support/NexusAI/EdgeAgent"
mkdir -p "$DIR"
curl -fsSL "https://raw.githubusercontent.com/nexusaiaphile-png/NexusAI/main/edge_agent/agent.py" -o "$DIR/agent.py"
curl -fsSL "https://raw.githubusercontent.com/nexusaiaphile-png/NexusAI/main/edge_agent/requirements.txt" -o "$DIR/requirements.txt"
python3 -m pip install --upgrade pip
python3 -m pip install -r "$DIR/requirements.txt"
echo "NexusAI Edge Agent installed."
echo "Start it with: python3 $DIR/agent.py"
