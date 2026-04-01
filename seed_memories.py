"""
Seed Musubi with existing memory files.
Reads all .md files from a memory directory, parses frontmatter,
and stores each as a vector in Qdrant via the musubi package directly.
"""

import os
import re
import sys
from pathlib import Path

from qdrant_client import QdrantClient

from musubi.collections import ensure_collections
from musubi.config import MEMORY_COLLECTION, QDRANT_HOST, QDRANT_PORT
from musubi.memory import memory_store

# Default memory directory
DEFAULT_MEMORY_DIR = os.path.expanduser("~/.claude/projects/-Users-ericmey--openclaw/memory")


def parse_memory_file(filepath: str) -> dict | None:
    """Parse a memory .md file with YAML frontmatter."""
    with open(filepath) as f:
        content = f.read()

    # Skip MEMORY.md index file
    if os.path.basename(filepath) == "MEMORY.md":
        return None

    # Parse frontmatter
    frontmatter = {}
    body = content
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, re.DOTALL)
    if fm_match:
        fm_text = fm_match.group(1)
        body = fm_match.group(2).strip()
        for line in fm_text.split("\n"):
            if ":" in line:
                key, val = line.split(":", 1)
                frontmatter[key.strip()] = val.strip()

    if not body:
        return None

    # Determine type from frontmatter or filename
    mem_type = frontmatter.get("type", "reference")
    if mem_type not in ("user", "feedback", "project", "reference"):
        mem_type = "reference"

    # Determine agent from content or default
    agent = "aoi"
    filename = os.path.basename(filepath)

    # Extract tags from filename
    tags = []
    tag_parts = filename.replace(".md", "").split("_")
    if tag_parts[0] in ("feedback", "project", "reference", "user"):
        tags.append(tag_parts[0])
        tags.extend(tag_parts[1:])
    else:
        tags = tag_parts

    return {
        "content": body,
        "type": mem_type,
        "agent": agent,
        "tags": tags,
        "context": f"Migrated from file: {filename}",
    }


def main():
    memory_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MEMORY_DIR

    if not os.path.isdir(memory_dir):
        print(f"Memory directory not found: {memory_dir}")
        sys.exit(1)

    # Connect to Qdrant directly
    qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)

    if not ensure_collections(qdrant):
        print("Cannot connect to Qdrant — collection setup failed.")
        sys.exit(1)

    # Check existing memory count
    try:
        info = qdrant.get_collection(MEMORY_COLLECTION)
        print(f"Musubi status: ok ({info.points_count} existing memories)")
    except Exception as e:
        print(f"Cannot read collection info: {e}")
        sys.exit(1)

    # Find all memory files
    md_files = sorted(Path(memory_dir).glob("*.md"))
    print(f"Found {len(md_files)} memory files")

    stored = 0
    updated = 0
    skipped = 0

    for filepath in md_files:
        memory = parse_memory_file(str(filepath))
        if not memory:
            print(f"  SKIP  {filepath.name} (empty or index)")
            skipped += 1
            continue

        try:
            result = memory_store(
                qdrant,
                content=memory["content"],
                type=memory["type"],
                agent=memory["agent"],
                tags=memory["tags"],
                context=memory["context"],
            )
            status = result.get("status", "unknown")
            if status == "stored":
                print(f"  NEW   {filepath.name} -> {result['id'][:8]}")
                stored += 1
            elif status == "updated":
                print(
                    f"  MERGE {filepath.name} -> {result['id'][:8]} (sim: {result.get('similarity', 0):.2f})"
                )
                updated += 1
            elif "error" in result:
                print(f"  ERROR {filepath.name}: {result['error']}")
            else:
                print(f"  ???   {filepath.name} -> {result}")
        except Exception as e:
            print(f"  ERROR {filepath.name}: {e}")

    print(f"\nDone. Stored: {stored}, Updated: {updated}, Skipped: {skipped}")


if __name__ == "__main__":
    main()
