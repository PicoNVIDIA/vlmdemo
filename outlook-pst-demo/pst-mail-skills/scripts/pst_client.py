import asyncio
import sys
import json
from fastmcp import Client

MCP_SERVER_URL = "http://host.openshell.internal:9003/mcp"


async def call_tool(tool_name: str, arguments: dict) -> str:
    """Call a specific tool on the PST MCP server and return the text result."""
    async with Client(MCP_SERVER_URL) as client:
        result = await client.call_tool(tool_name, arguments)
    return result.content[0].text


def main():
    if len(sys.argv) < 3:
        print("Usage: pst_client.py <tool_name> <json_arguments>", file=sys.stderr)
        sys.exit(1)

    tool_name = sys.argv[1]
    try:
        arguments = json.loads(sys.argv[2])
    except json.JSONDecodeError as e:
        print(f"Invalid JSON arguments: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        output = asyncio.run(call_tool(tool_name, arguments))
    except Exception as e:
        print(f"Error calling MCP server: {e}", file=sys.stderr)
        sys.exit(1)

    print(output)


if __name__ == "__main__":
    main()
