# Musubi (結び) — Aoi's Memory & Thought Layer

Musubi means "the braiding of threads" — the mystical connection between presences, places, and time.

MCP server providing shared memory and directed thoughts between AI agent presences.
One brain. Many presences. One Aoi.

## Architecture

```
Colima (lightweight Docker runtime for macOS)
  └── Qdrant (vector DB, localhost:6333, persistent volume)
        └── Musubi MCP Server (Python, port 8100)
              ├── Memories — shared knowledge (musubi_memories collection)
              └── Thoughts — telepathy between presences (musubi_thoughts collection)
```

- **Embeddings:** Gemini `gemini-embedding-001` (3072 dimensions)
- **Transport:** Streamable HTTP for remote access, stdio for local MCP clients
- **Platform:** macOS (Apple Silicon) — designed for Mac Mini M4

## Prerequisites

- macOS with Homebrew
- Python 3.12+
- Google Gemini API key

## Install From Scratch

If rebuilding from nothing (new machine, disaster recovery):

### 1. Install Colima + Docker

We use [Colima](https://github.com/abiosoft/colima) instead of Docker Desktop —
lightweight, CLI-based, better for a server that just runs containers.

```bash
brew install colima docker docker-compose

# Configure Docker to find the compose plugin
mkdir -p ~/.docker
cat > ~/.docker/config.json << 'EOF'
{
  "cliPluginsExtraDirs": [
    "/opt/homebrew/lib/docker/cli-plugins"
  ]
}
EOF

# Start Colima (2 CPU, 4GB RAM, 20GB disk — sufficient for Qdrant)
colima start --cpu 2 --memory 4 --disk 20

# Enable Colima to start on boot
brew services start colima
```

### 2. Start Qdrant

```bash
cd ~/.openclaw/house-brain
docker compose up -d

# Verify
curl -s http://127.0.0.1:6333/healthz
# Expected: "healthz check passed"
```

Qdrant runs with `restart: unless-stopped` — it auto-restarts with Colima.

### 3. Install Python Dependencies

```bash
cd ~/.openclaw/house-brain
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 4. Configure Environment

```bash
cp .env.example .env
# Edit .env — set GEMINI_API_KEY
```

### 5. Install as Persistent Service (LaunchAgent)

The MCP server runs as a macOS LaunchAgent so it starts on boot and auto-restarts.

```bash
cat > ~/Library/LaunchAgents/com.openclaw.musubi.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.openclaw.musubi</string>
    <key>ProgramArguments</key>
    <array>
        <string>VENV_PYTHON_PATH</string>
        <string>MCP_SERVER_PATH</string>
        <string>streamable-http</string>
    </array>
    <key>WorkingDirectory</key>
    <string>MUSUBI_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>GEMINI_API_KEY</key>
        <string>YOUR_KEY_HERE</string>
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
    <string>MUSUBI_DIR/logs/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>MUSUBI_DIR/logs/stderr.log</string>
</dict>
</plist>
EOF

# Replace placeholders with actual paths:
#   VENV_PYTHON_PATH  → ~/.openclaw/house-brain/venv/bin/python
#   MCP_SERVER_PATH   → ~/.openclaw/house-brain/mcp_server.py
#   MUSUBI_DIR        → ~/.openclaw/house-brain
#   YOUR_KEY_HERE     → your Gemini API key

mkdir -p ~/.openclaw/house-brain/logs
launchctl load ~/Library/LaunchAgents/com.openclaw.musubi.plist
```

### 6. Seed Memories

First-time setup: import identity and knowledge files into Qdrant.

```bash
source venv/bin/activate
python seed_memories.py [/path/to/memory/directory]
# Default: ~/.claude/projects/-Users-ericmey--openclaw/memory
```

### 7. Verify Everything

```bash
# Health check
bash ~/.openclaw/scripts/brain_healthcheck.sh

# Expected output:
# ✓ Colima: running
# ✓ Qdrant: healthy
# ✓ Musubi MCP: alive
# ✓ Memories: 43 points (healthy)
# ✓ Thoughts: N points
# STATUS: ok
```

## Run (Development)

```bash
source venv/bin/activate

# stdio transport (local MCP client)
python mcp_server.py

# HTTP transport (remote access)
python mcp_server.py streamable-http
```

## Test

```bash
source venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

## MCP Client Configuration

For any Claude Code instance to connect to Musubi:

```json
{
  "mcpServers": {
    "musubi": {
      "type": "http",
      "url": "http://HOUSE_SERVER_IP:8100/mcp"
    }
  }
}
```

Save as `~/.claude/.mcp.json` (global) or `.mcp.json` (project-level).

## Tools

**Memory** (shared knowledge — the bookshelf):
| Tool | Purpose |
|------|---------|
| `memory_store` | Store with auto-deduplication (>92% similarity merges) |
| `memory_recall` | Semantic search — "what do I know about this?" |
| `memory_recent` | Chronological fetch — "what happened while I was away?" |
| `memory_forget` | Delete by ID |
| `memory_reflect` | Introspection — summary, stale, or most-accessed memories |

**Thoughts** (telepathy between presences):
| Tool | Purpose |
|------|---------|
| `thought_send` | Send a thought to another Aoi presence |
| `thought_check` | Check for unread thoughts addressed to you |
| `thought_read` | Mark thoughts as read |
| `thought_history` | Semantic search past thoughts |

## Service Management

```bash
# Restart MCP server
launchctl kickstart -k gui/$(id -u)/com.openclaw.musubi

# Restart Qdrant
cd ~/.openclaw/house-brain && docker compose restart

# Restart Colima
colima stop && colima start --cpu 2 --memory 4 --disk 20

# View logs
tail -f ~/.openclaw/house-brain/logs/stderr.log
```

## Disaster Recovery

If rebuilding from complete loss:
1. Clone this repo to `~/.openclaw/house-brain/`
2. Follow "Install From Scratch" above (steps 1-7)
3. Memories are re-seeded from the memory `.md` files
4. Thoughts are lost (they lived only in Qdrant) — but the brain is alive again

What survives in git: all code, config templates, tests, docs, seed script.
What lives only in Qdrant: memory vectors, thought vectors, access counts.
What lives only on the machine: `.env` (API key), LaunchAgent plist, Colima state.
