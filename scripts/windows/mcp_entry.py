"""PyInstaller entry point for rebuild-mcp.exe (stdio MCP server used by the optional client packages)."""
import multiprocessing
import sys

from rebuild_controller.mcp.server import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
