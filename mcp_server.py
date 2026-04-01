"""
Musubi MCP Server — thin entry point.
All logic lives in the musubi package.
"""

import sys
from musubi.server import mcp

if __name__ == "__main__":
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    mcp.run(transport=transport)
