#!/bin/bash
# Musubi Install — Full setup from scratch
# Usage: ./scripts/install.sh
#
# Installs: Colima, Docker, Qdrant, Python venv, LaunchAgent
# Requires: macOS with Homebrew, Python 3.12+, a Gemini API key

set -euo pipefail

MUSUBI_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$MUSUBI_DIR/venv"
LOG_DIR="$MUSUBI_DIR/logs"
PLIST_NAME="com.openclaw.musubi"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"
ENV_FILE="$MUSUBI_DIR/.env"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}⚠${NC} $1"; }
fail()  { echo -e "${RED}✗${NC} $1"; exit 1; }
step()  { echo -e "\n${GREEN}---${NC} $1"; }

# --- Preflight ---
step "Preflight checks"

if [[ "$(uname)" != "Darwin" ]]; then
    fail "Musubi currently supports macOS only. Linux support coming soon."
fi

if ! command -v brew &>/dev/null; then
    fail "Homebrew is required. Install it from https://brew.sh"
fi

if ! command -v python3 &>/dev/null; then
    fail "Python 3 is required. Install via: brew install python@3.12"
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)
if [[ "$PYTHON_MAJOR" -lt 3 ]] || [[ "$PYTHON_MAJOR" -eq 3 && "$PYTHON_MINOR" -lt 12 ]]; then
    fail "Python 3.12+ required (found $PYTHON_VERSION). Install via: brew install python@3.12"
fi
info "Python $PYTHON_VERSION"

# --- Colima + Docker ---
step "Installing Colima and Docker"

for pkg in colima docker docker-compose; do
    if brew list "$pkg" &>/dev/null; then
        info "$pkg already installed"
    else
        echo "  Installing $pkg..."
        brew install "$pkg"
        info "$pkg installed"
    fi
done

# Docker compose plugin config
mkdir -p "$HOME/.docker"
if [[ ! -f "$HOME/.docker/config.json" ]] || ! grep -q "cliPluginsExtraDirs" "$HOME/.docker/config.json" 2>/dev/null; then
    cat > "$HOME/.docker/config.json" << 'DOCKEREOF'
{
  "cliPluginsExtraDirs": [
    "/opt/homebrew/lib/docker/cli-plugins"
  ]
}
DOCKEREOF
    info "Docker compose plugin configured"
else
    info "Docker compose plugin already configured"
fi

# Start Colima if not running
if colima status 2>&1 | grep -qi "is running"; then
    info "Colima already running"
else
    echo "  Starting Colima (2 CPU, 4GB RAM, 20GB disk)..."
    colima start --cpu 2 --memory 4 --disk 20
    info "Colima started"
fi

# Enable Colima on boot
if brew services list | grep -q "colima.*started"; then
    info "Colima boot service already enabled"
else
    brew services start colima
    info "Colima boot service enabled"
fi

# --- Qdrant ---
step "Starting Qdrant"

cd "$MUSUBI_DIR"
if docker compose ps 2>/dev/null | grep -q "qdrant"; then
    info "Qdrant already running"
else
    docker compose up -d
    echo "  Waiting for Qdrant to be ready..."
    for i in $(seq 1 30); do
        if curl -s --max-time 2 http://127.0.0.1:6333/healthz 2>/dev/null | grep -q "passed"; then
            break
        fi
        sleep 1
    done
    if curl -s --max-time 2 http://127.0.0.1:6333/healthz 2>/dev/null | grep -q "passed"; then
        info "Qdrant healthy"
    else
        fail "Qdrant failed to start. Check: docker compose logs"
    fi
fi

# --- Python venv ---
step "Setting up Python environment"

if [[ -d "$VENV_DIR" ]]; then
    info "Virtual environment already exists"
else
    python3 -m venv "$VENV_DIR"
    info "Virtual environment created"
fi

"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -r "$MUSUBI_DIR/requirements.txt" --quiet
info "Dependencies installed"

# --- Environment file ---
step "Configuring environment"

if [[ -f "$ENV_FILE" ]]; then
    info ".env file already exists"
else
    if [[ -f "$MUSUBI_DIR/.env.example" ]]; then
        cp "$MUSUBI_DIR/.env.example" "$ENV_FILE"
    else
        cat > "$ENV_FILE" << 'ENVEOF'
GEMINI_API_KEY=your-key-here
QDRANT_HOST=localhost
QDRANT_PORT=6333
BRAIN_PORT=8100
ENVEOF
    fi
    warn ".env file created — edit it to set your GEMINI_API_KEY"
    warn "  → $ENV_FILE"
fi

# Check if API key is set
if grep -q "your-key-here" "$ENV_FILE" 2>/dev/null; then
    warn "GEMINI_API_KEY is not set yet. Edit $ENV_FILE before starting the service."
    SKIP_SERVICE=true
else
    SKIP_SERVICE=false
fi

# --- Log directory ---
mkdir -p "$LOG_DIR"

# --- LaunchAgent ---
step "Installing LaunchAgent"

if [[ "$SKIP_SERVICE" == "true" ]]; then
    warn "Skipping LaunchAgent — set GEMINI_API_KEY first, then run: launchctl load $PLIST_PATH"
else
    # Read API key from .env
    GEMINI_KEY=$(grep "^GEMINI_API_KEY=" "$ENV_FILE" | cut -d= -f2-)

    cat > "$PLIST_PATH" << PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_NAME</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV_DIR/bin/python</string>
        <string>$MUSUBI_DIR/mcp_server.py</string>
        <string>streamable-http</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$MUSUBI_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>GEMINI_API_KEY</key>
        <string>$GEMINI_KEY</string>
        <key>QDRANT_HOST</key>
        <string>localhost</string>
        <key>QDRANT_PORT</key>
        <string>6333</string>
        <key>BRAIN_PORT</key>
        <string>8100</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/stderr.log</string>
</dict>
</plist>
PLISTEOF

    # Unload if already loaded, then load
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    launchctl load "$PLIST_PATH"
    info "LaunchAgent installed and started"

    # Verify MCP is responding
    sleep 3
    RESP=$(curl -s --max-time 5 -X POST http://127.0.0.1:8100/mcp \
        -H "Content-Type: application/json" \
        -H "Accept: application/json" \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"install","version":"1.0"}}}' 2>/dev/null)
    if echo "$RESP" | grep -q "musubi"; then
        info "Musubi MCP responding"
    else
        warn "Musubi MCP not responding yet. Check logs: tail -f $LOG_DIR/stderr.log"
    fi
fi

# --- Done ---
step "Installation complete"
echo ""
echo "Musubi is installed at: $MUSUBI_DIR"
echo ""
echo "Next steps:"
if [[ "$SKIP_SERVICE" == "true" ]]; then
    echo "  1. Set your Gemini API key: edit $ENV_FILE"
    echo "  2. Start the service: ./scripts/install.sh (re-run) or launchctl load $PLIST_PATH"
    echo "  3. Seed memories: $VENV_DIR/bin/python seed_memories.py /path/to/memories"
else
    echo "  1. Seed memories: $VENV_DIR/bin/python seed_memories.py /path/to/memories"
    echo "  2. Connect MCP client — add to ~/.claude/.mcp.json:"
    echo '     {"mcpServers":{"musubi":{"type":"http","url":"http://localhost:8100/mcp"}}}'
fi
echo ""
echo "Manage:"
echo "  Update:    ./scripts/update.sh"
echo "  Uninstall: ./scripts/uninstall.sh"
echo "  Logs:      tail -f $LOG_DIR/stderr.log"
