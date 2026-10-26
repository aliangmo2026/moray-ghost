"""一次性冒烟：官方 MCP Client 经 Stdio 拉起 mcp_server → initialize → tools/list。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import anyio  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


async def main() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "mcp_server.py")],
        env={**os.environ, "GHOST_CAPABILITIES": '["python.exe"]'})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            info = await s.initialize()
            si = getattr(info, "server_info", None) or info.serverInfo
            print("INIT_OK", si.name, getattr(si, "version", None), flush=True)
            tools = await s.list_tools()
            names = [t.name for t in tools.tools]
            print("TOOLS", json.dumps(names, ensure_ascii=False), flush=True)
            for t in tools.tools:
                schema = getattr(t, "input_schema", None) or t.inputSchema
                props = sorted((schema or {}).get("properties", {}).keys())
                print("  -", t.name, props, flush=True)


if __name__ == "__main__":
    anyio.run(main)
