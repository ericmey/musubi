#!/bin/bash
# Musubi Update — Pull latest, update deps, restart service
# Usage: ./scripts/update.sh
#
# Steps: brew upgrade → git pull → pip upgrade → restart → health check

set -euo pipefail

MUSUBI_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$MUSUBI_DIR/venv"
LOG_DIR="$MUSUBI_DIR/logs"
PLIST_NAME="com.openclaw.musubi"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}⚠${NC} $1"; }
fail()  { echo -e "${RED}✗${NC} $1"; exit 1; }
step()  { echo -e "\n${GREEN}---${NC} $1"; }

# --- Preflight ---
step "Preflight"

if [[ ! -d "$VENV_DIR" ]]; then
    fail "Musubi is not installed. Run ./scripts/install.sh first."
fi

if [[ ! -f "$PLIST_PATH" ]]; then
    warn "LaunchAgent not found — service restart will be skipped"
    HAS_SERVICE=false
else
    HAS_SERVICE=true
fi

# Snapshot current state for rollback info
CURRENT_COMMIT=$(cd "$MUSUBI_DIR" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")
info "Current version: $CURRENT_COMMIT"

# --- Brew ---
step "Updating Homebrew packages"

BREW_UPDATED=false
for pkg in colima docker docker-compose; do
    if brew list "$pkg" &>/dev/null; then
        OUTDATED=$(brew outdated "$pkg" 2>/dev/null || true)
        if [[ -n "$OUTDATED" ]]; then
            echo "  Upgrading $pkg..."
            brew upgrade "$pkg"
            BREW_UPDATED=true
            info "$pkg upgraded"
        else
            info "$pkg up to date"
        fi
    else
        warn "$pkg not installed — skipping (run install.sh for full setup)"
    fi
done

# --- Git ---
step "Pulling latest code"

cd "$MUSUBI_DIR"

# Check for local changes
if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
    warn "Local changes detected — stashing before pull"
    git stash push -m "musubi-update-$(date +%Y%m%d-%H%M%S)"
    STASHED=true
else
    STASHED=false
fi

# Pull latest
BEFORE=$(git rev-parse --short HEAD)
git pull origin main --ff-only 2>&1 || {
    warn "Fast-forward pull failed — you may have diverged from main"
    if [[ "$STASHED" == "true" ]]; then
        git stash pop
    fi
    fail "Update aborted. Resolve manually with: git pull origin main"
}
AFTER=$(git rev-parse --short HEAD)

if [[ "$BEFORE" == "$AFTER" ]]; then
    info "Already up to date ($AFTER)"
else
    info "Updated: $BEFORE → $AFTER"
fi

# Restore stash if needed
if [[ "$STASHED" == "true" ]]; then
    git stash pop
    info "Local changes restored"
fi

# --- Python deps ---
step "Updating Python dependencies"

"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -r "$MUSUBI_DIR/requirements.txt" --upgrade --quiet
info "Dependencies synced"

# --- Restart service ---
step "Restarting Musubi"

if [[ "$HAS_SERVICE" == "true" ]]; then
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    sleep 1
    launchctl load "$PLIST_PATH"
    info "LaunchAgent restarted"
    sleep 3
else
    warn "No LaunchAgent — skipping restart"
fi

# --- Health check ---
step "Health check"

HEALTHY=true

# Colima
if command -v colima &>/dev/null && colima status 2>&1 | grep -qi "is running"; then
    info "Colima: running"
else
    warn "Colima: not detected (may be using Docker Desktop)"
fi

# Qdrant
if curl -s --max-time 5 http://127.0.0.1:6333/healthz 2>/dev/null | grep -q "passed"; then
    info "Qdrant: healthy"
else
    warn "Qdrant: not responding"
    HEALTHY=false
fi

# MCP
RESP=$(curl -s --max-time 5 -X POST http://127.0.0.1:8100/mcp \
    -H "Content-Type: application/json" \
    -H "Accept: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"update","version":"1.0"}}}' 2>/dev/null)
if echo "$RESP" | grep -q "musubi"; then
    info "Musubi MCP: alive"
else
    warn "Musubi MCP: not responding. Check logs: tail -f $LOG_DIR/stderr.log"
    HEALTHY=false
fi

# Data
MEM_COUNT=$(curl -s --max-time 5 http://127.0.0.1:6333/collections/musubi_memories 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['points_count'])" 2>/dev/null || echo "0")
if [[ "$MEM_COUNT" -gt 0 ]]; then
    info "Memories: $MEM_COUNT points"
else
    warn "Memories: empty or unreachable"
fi

# --- Summary ---
echo ""
if [[ "$HEALTHY" == "true" ]]; then
    info "Update complete. Musubi is healthy."
else
    warn "Update complete with warnings. Check the issues above."
fi
