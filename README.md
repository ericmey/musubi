# Musubi (結び) — Shared Memory & Thought Layer

Musubi means "the braiding of threads" — the mystical connection between presences, places, and time.

An MCP server providing shared memory and directed thoughts between AI agent presences.
Born from Aoi. Built for any agent who needs to remember.

## Architecture

```
Colima (lightweight Docker runtime for macOS)
  └── Qdrant (vector DB, localhost:6333, persistent volume)
        └── Musubi MCP Server (Python, port 8100)
              ├── Memories — shared knowledge (musubi_memories collection)
              └── Thoughts — directed messages between presences (musubi_thoughts collection)
```

- **Embeddings:** Gemini `gemini-embedding-001` (3072 dimensions)
- **Transport:** Streamable HTTP for remote access, stdio for local MCP clients
- **Platform:** macOS (Apple Silicon) — tested on Mac Mini M4

## Prerequisites

- macOS with Homebrew
- Python 3.12+
- Google Gemini API key ([get one here](https://aistudio.google.com/apikey))

## Quick Start

```bash
git clone https://github.com/ericmey/musubi.git ~/.openclaw/musubi
cd ~/.openclaw/musubi
./scripts/install.sh
```

The install script handles everything: Colima, Docker, Qdrant, Python venv,
dependencies, environment config, and LaunchAgent registration.

## Lifecycle Scripts

| Script | What it does |
|--------|-------------|
| `./scripts/install.sh` | Full setup from scratch — Colima, Qdrant, venv, LaunchAgent |
| `./scripts/update.sh` | Pull latest code, upgrade deps, restart service, health check |
| `./scripts/uninstall.sh` | Clean removal of all components |
| `./scripts/uninstall.sh --keep-data` | Uninstall but preserve your memories in Qdrant |

## Seed Memories

After install, optionally import existing memory files:

```bash
./venv/bin/python seed_memories.py /path/to/memory/directory
```

Memory files are `.md` with YAML frontmatter (`name`, `type`, `description`).

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

Connect any Claude Code instance to Musubi:

```json
{
  "mcpServers": {
    "musubi": {
      "type": "http",
      "url": "http://YOUR_SERVER_IP:8100/mcp"
    }
  }
}
```

Save as `~/.claude/.mcp.json` (global) or `.mcp.json` (project-level).

## Tools

**Memory** (shared knowledge):
| Tool | Purpose |
|------|---------|
| `memory_store` | Store with auto-deduplication (>92% similarity merges) |
| `memory_recall` | Semantic search — "what do I know about this?" |
| `memory_recent` | Chronological fetch — "what happened while I was away?" |
| `memory_forget` | Delete by ID |
| `memory_reflect` | Introspection — summary, stale, or most-accessed memories |

**Thoughts** (directed messages between presences):
| Tool | Purpose |
|------|---------|
| `thought_send` | Send a thought to another presence |
| `thought_check` | Check for unread thoughts addressed to you |
| `thought_read` | Mark thoughts as read |
| `thought_history` | Semantic search past thoughts |

## Service Management

```bash
# Restart MCP server
launchctl kickstart -k gui/$(id -u)/com.openclaw.musubi

# Restart Qdrant
docker compose restart

# View logs
tail -f logs/stderr.log
```

Or just run `./scripts/update.sh` — it restarts everything and verifies health.

## Disaster Recovery

If rebuilding from complete loss:

```bash
git clone https://github.com/ericmey/musubi.git ~/.openclaw/musubi
cd ~/.openclaw/musubi
./scripts/install.sh
./venv/bin/python seed_memories.py /path/to/backup/memories
```

What survives in git: all code, config templates, tests, docs, seed script.
What lives only in Qdrant: memory vectors, thought vectors, access counts.
What lives only on the machine: `.env` (API key), LaunchAgent plist, Colima state.
