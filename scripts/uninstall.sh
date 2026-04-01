#!/bin/bash
# Musubi Uninstall — Clean removal of all Musubi components
# Usage: ./scripts/uninstall.sh [--keep-data]
#
# Removes: LaunchAgent, venv, logs, Qdrant container
# Optional: --keep-data preserves Qdrant volume (your memories survive)

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
step()  { echo -e "\n${GREEN}---${NC} $1"; }

KEEP_DATA=false
for arg in "$@"; do
    case "$arg" in
        --keep-data) KEEP_DATA=true ;;
    esac
done

echo ""
echo "Musubi Uninstaller"
echo "=================="
if [[ "$KEEP_DATA" == "true" ]]; then
    echo "Mode: uninstall (keeping Qdrant data volume)"
else
    echo "Mode: full uninstall (all data will be deleted)"
fi
echo ""

# --- Confirm ---
read -p "Are you sure? This cannot be undone. [y/N] " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Cancelled."
    exit 0
fi

# --- Stop LaunchAgent ---
step "Stopping Musubi service"

if [[ -f "$PLIST_PATH" ]]; then
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    rm -f "$PLIST_PATH"
    info "LaunchAgent removed"
else
    info "No LaunchAgent found"
fi

# --- Stop Qdrant container ---
step "Stopping Qdrant"

cd "$MUSUBI_DIR"
if docker compose ps 2>/dev/null | grep -q "qdrant"; then
    docker compose down
    info "Qdrant container stopped and removed"
else
    info "No Qdrant container running"
fi

if [[ "$KEEP_DATA" == "false" ]]; then
    # Remove the named volume
    VOLUME_NAME=$(docker volume ls -q 2>/dev/null | grep "musubi" | head -1 || true)
    if [[ -n "$VOLUME_NAME" ]]; then
        docker volume rm "$VOLUME_NAME" 2>/dev/null || true
        info "Qdrant data volume removed"
    fi
else
    warn "Qdrant data volume preserved (--keep-data)"
fi

# --- Remove venv ---
step "Removing Python environment"

if [[ -d "$VENV_DIR" ]]; then
    rm -rf "$VENV_DIR"
    info "Virtual environment removed"
else
    info "No virtual environment found"
fi

# --- Remove logs ---
step "Removing logs"

if [[ -d "$LOG_DIR" ]]; then
    rm -rf "$LOG_DIR"
    info "Logs removed"
else
    info "No logs found"
fi

# --- Remove .env ---
step "Removing configuration"

if [[ -f "$MUSUBI_DIR/.env" ]]; then
    rm -f "$MUSUBI_DIR/.env"
    info ".env removed"
else
    info "No .env found"
fi

# --- Summary ---
echo ""
info "Musubi has been uninstalled."
echo ""
echo "What was removed:"
echo "  - LaunchAgent (com.openclaw.musubi)"
echo "  - Qdrant container"
if [[ "$KEEP_DATA" == "false" ]]; then
    echo "  - Qdrant data volume (memories and thoughts)"
fi
echo "  - Python virtual environment"
echo "  - Log files"
echo "  - .env configuration"
echo ""
echo "What was NOT removed:"
echo "  - This git repository ($MUSUBI_DIR)"
echo "  - Colima and Docker (shared tools — remove manually if unused)"
if [[ "$KEEP_DATA" == "true" ]]; then
    echo "  - Qdrant data volume (your memories are preserved)"
fi
echo ""
echo "To reinstall: ./scripts/install.sh"
