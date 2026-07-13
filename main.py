"""Entrypoint for the delegate-local MCP server.

The server itself lives in `server.py` (stdio MCP). Running this module starts it,
matching what docs/ARCHITECTURE.md describes.
"""
from server import mcp


def main():
    mcp.run()


if __name__ == "__main__":
    main()
