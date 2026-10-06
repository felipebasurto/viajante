"""Stdio MCP smoke: starts viajante-mcp, checks the result envelope end to end.

Run it in an environment that has viajante and an `mcp` SDK installed. CI runs it
on the minimum supported SDK. Offline: calls only a local tool. Exits 1 with the
failed check on stderr.
"""

import asyncio
import json
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


def server_command() -> str:
    beside = Path(sys.executable).with_name("viajante-mcp")
    return str(beside) if beside.exists() else shutil.which("viajante-mcp") or str(beside)


def check(condition: bool, message: str) -> None:
    if not condition:
        print(f"mcp smoke failed: {message}", file=sys.stderr)
        raise SystemExit(1)


async def main() -> None:
    async with stdio_client(StdioServerParameters(command=server_command())) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            check(len(tools) == 20, f"expected 20 tools, got {sorted(t.name for t in tools)}")
            bare = {tool.name for tool in tools if tool.outputSchema is None}
            check(bare == {"lookup_airports"}, f"tools without an output schema: {sorted(bare)}")
            for tool in tools:
                if tool.outputSchema is not None:
                    missing = ENVELOPE - set(tool.outputSchema["properties"])
                    check(not missing, f"{tool.name} schema lacks {sorted(missing)}")
            result = await session.call_tool("get_runtime_info", {})
            check(not result.isError, f"get_runtime_info errored: {result}")
            structured = result.structuredContent
            check(bool(structured), "get_runtime_info returned no structuredContent")
            missing = ENVELOPE - set(structured)
            check(not missing, f"structuredContent lacks {sorted(missing)}")
            check(structured["status"] == "ok", f"status {structured['status']!r}")
            check(structured["completeness"] == "complete", f"completeness {structured!r}")
            guide = await session.call_tool("get_guide", {})
            check(not guide.isError, f"get_guide errored: {guide}")
            structured = guide.structuredContent or {}
            check("guide" in structured, "get_guide returned no guide")
            check(not ENVELOPE - set(structured), "get_guide lacks the envelope")
            bad = await session.call_tool("get_runtime_info", {"verbos": True})
            check(bad.isError, "an undeclared argument was accepted")
            text = bad.content[0].text
            prefix = "Error executing tool get_runtime_info: "
            check(text.startswith(prefix + "{"), f"unexpected error text {text!r}")
            body = json.loads(text[len(prefix) :])["error"]
            check(body["code"] == "invalid_parameter", f"error body {body!r}")
            check(body["field"] == "verbos", f"error field {body!r}")
    print("mcp smoke ok")


asyncio.run(main())
