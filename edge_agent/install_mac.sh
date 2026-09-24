#!/bin/bash
set -euo pipefail
echo "NexusAI Edge Agent installer"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.11+ is required. Install Python from python.org, then run this installer again."
  exit 1
fi

DIR="$HOME/Library/Application Support/NexusAI/EdgeAgent"
mkdir -p "$DIR"
cd "$DIR"

curl -fsSL "https://raw.githubusercontent.com/nexusaiaphile-png/NexusAI/main/edge_agent/agent.py" -o "$DIR/agent.py"
curl -fsSL "https://raw.githubusercontent.com/nexusaiaphile-png/NexusAI/main/edge_agent/requirements.txt" -o "$DIR/requirements.txt"

if [ ! -x "$DIR/.venv/bin/python" ]; then
  python3 -m venv "$DIR/.venv"
fi

"$DIR/.venv/bin/python" -m pip install --upgrade pip
"$DIR/.venv/bin/python" -m pip install -r "$DIR/requirements.txt"

cat > "$DIR/.env" <<EOF
NEXUSAI_API_URL=https://nexusai-worker.onrender.com
NEXUSAI_EDGE_TOKEN=
LOCAL_AGENT_HOST=0.0.0.0
LOCAL_AGENT_PORT=8787
EOF

chmod 700 "$DIR"
chmod 600 "$DIR/.env"

pkill -f "$DIR/agent.py" >/dev/null 2>&1 || true
nohup "$DIR/.venv/bin/python" "$DIR/agent.py" > "$DIR/agent.out.log" 2>&1 < /dev/null &

sleep 2
if curl -fsS "http://127.0.0.1:8787/health" >/dev/null 2>&1; then
  echo "NexusAI Edge Agent installed and running."
else
  echo "NexusAI Edge Agent started, but health check did not respond yet."
  echo "Log: $DIR/agent.out.log"
fi
echo "Local health: http://127.0.0.1:8787/health"
