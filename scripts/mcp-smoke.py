"""Stdio MCP smoke: starts viajante-mcp, checks the result envelope end to end.

Run it in an environment that has viajante and an `mcp` SDK installed. CI runs it
on the minimum supported SDK. Offline: calls only a local tool.
"""

import asyncio
import shutil
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ENVELOPE = {
    "status",
    "completeness",
    "empty_reason",
    "empty_note",
    "error_code",
    "retry_after",
    "retry_after_seconds",
    "observed_at",
    "observed_at_basis",
}


async def main() -> None:
    command = shutil.which("viajante-mcp") or str(Path(sys.executable).with_name("viajante-mcp"))
    async with stdio_client(StdioServerParameters(command=command)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            assert len(tools) == 16, [tool.name for tool in tools]
            bare = {tool.name for tool in tools if tool.outputSchema is None}
            assert bare == {"lookup_airports"}, bare
            for tool in tools:
                if tool.outputSchema is not None:
                    assert ENVELOPE <= set(tool.outputSchema["properties"]), tool.name
            result = await session.call_tool("get_runtime_info", {})
            assert not result.isError, result
            structured = result.structuredContent
            assert structured and ENVELOPE <= set(structured), structured
            assert structured["status"] == "ok", structured
            assert structured["completeness"] == "complete", structured
    print("mcp smoke ok")


asyncio.run(main())
