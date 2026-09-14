"""Minimal MCP (Model Context Protocol) client for the Tapetide MCP server.

Implements just enough of the MCP "Streamable HTTP" transport to talk to
https://mcp.tapetide.com/mcp using only the Python standard library:

  1. ``initialize``      -> handshake, returns server capabilities + session id
  2. ``tools/list``      -> enumerates available tools
  3. ``tools/call``      -> invokes a tool with arguments

Authentication (per Tapetide docs at https://tapetide.com/mcp/llms-full.txt):

  - API token:  ``Authorization: Bearer <token>`` header (token from
    https://tapetide.com/settings/tokens)
  - OAuth:      browser-based sign-in, handled by the MCP client app — not
    usable from a plain HTTP client like this one.

The token is read from the ``TAPETIDE_TOKEN`` environment variable or passed
via ``--token``. It is never logged or written to disk.

CLI usage::

    # Verify auth + list tools
    python3 tapetide_downloader/mcp_client.py --token $TAPETIDE_TOKEN verify

    # Show the full tool catalog
    python3 tapetide_downloader/mcp_client.py tools

    # Call a tool
    python3 tapetide_downloader/mcp_client.py call get_stock_quote \
        --args '{"symbol": "RELIANCE"}'

    # Pretty-print a financials call
    python3 tapetide_downloader/mcp_client.py call get_financials \
        --args '{"symbol": "SBIN"}' --pretty
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

MCP_URL = "https://mcp.tapetide.com/mcp"
PROTOCOL_VERSION = "2025-03-26"
CLIENT_NAME = "tapetide-downloader"
CLIENT_VERSION = "1.0.0"
DEFAULT_TIMEOUT = 60


class McpError(RuntimeError):
    """Raised when the MCP server returns an error or an unexpected response."""


class TapetideMcpClient:
    """Tiny Streamable-HTTP MCP client (stdlib only)."""

    def __init__(self, token: str | None = None, url: str = MCP_URL,
                 timeout: int = DEFAULT_TIMEOUT) -> None:
        self.url = url
        self.timeout = timeout
        self.token = (token or os.environ.get("TAPETIDE_TOKEN", "")).strip()
        self.session_id: str | None = None
        self._request_id = 0

    # ---- transport -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        # NOTE: Tapetide sits behind Cloudflare with a WAF rule that blocks
        # the default "Python-urllib" User-Agent (Error 1010). A normal
        # browser-style UA passes through fine.
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
        }
        if self.token:
            # Never print the token itself.
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _post(self, payload: dict[str, Any]) -> tuple[int, str, str]:
        """POST a JSON-RPC message; returns (status, body, session_id)."""
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url, data=data,
                                     headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
                body = resp.read().decode("utf-8", errors="replace")
                session_id = resp.headers.get("Mcp-Session-Id")
        except urllib.error.HTTPError as exc:
            # Read the error body too — the server often explains the problem.
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            return exc.code, body, exc.headers.get("Mcp-Session-Id") or ""
        return status, body, session_id or ""

    # ---- JSON-RPC --------------------------------------------------------

    def _rpc(self, method: str, params: dict[str, Any] | None = None,
             notification: bool = False) -> dict[str, Any] | None:
        self._request_id += 1
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        if not notification:
            payload["id"] = self._request_id

        status, body, session_id = self._post(payload)
        if session_id and not self.session_id:
            self.session_id = session_id

        if status == 401 or status == 403:
            raise McpError(
                f"Authentication failed (HTTP {status}). Get a free token at "
                f"https://tapetide.com/settings/tokens and pass it via "
                f"TAPETIDE_TOKEN or --token."
            )
        if status == 404 and method == "initialize":
            # Some servers don't hand out a session until after the first
            # POST; retry once without the session header.
            self.session_id = None
            status, body, session_id = self._post(payload)
            if session_id:
                self.session_id = session_id
        if status >= 400:
            raise McpError(f"HTTP {status} from {self.url}: {body[:300]}")

        if notification:
            return None

        # Body may be raw JSON or an SSE stream ("data: {...}" lines).
        data = self._parse_body(body)
        if data is None:
            raise McpError(f"Empty/invalid response for {method}: {body[:300]}")
        if "error" in data:
            err = data["error"]
            raise McpError(f"MCP error {err.get('code')}: {err.get('message')}")
        return data.get("result", {})

    @staticmethod
    def _parse_body(body: str) -> dict[str, Any] | None:
        body = body.strip()
        if not body:
            return None
        if body.startswith("{"):
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return None
        # SSE: take the last "data:" line that parses as JSON.
        for line in reversed(body.splitlines()):
            line = line.strip()
            if line.startswith("data:"):
                candidate = line[len("data:"):].strip()
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    continue
        return None

    # ---- MCP lifecycle ---------------------------------------------------

    def initialize(self) -> dict[str, Any]:
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        )
        # Notification of initialization (no response expected).
        self._rpc("notifications/initialized", notification=True)
        return result or {}

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._rpc("tools/list") or {}
        return result.get("tools", [])

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        result = self._rpc("tools/call", {"name": name,
                                          "arguments": arguments or {}})
        if not isinstance(result, dict):
            return result
        if result.get("isError"):
            parts = result.get("content", [])
            text = "; ".join(
                p.get("text", "") for p in parts if isinstance(p, dict)
            )
            raise McpError(f"Tool '{name}' returned an error: {text[:400]}")
        return result


# ---- helpers -----------------------------------------------------------------

def extract_text(result: dict[str, Any]) -> str:
    """Join the text content of a tools/call result."""
    parts = result.get("content", []) if isinstance(result, dict) else []
    texts = [p.get("text", "") for p in parts
             if isinstance(p, dict) and p.get("type") == "text"]
    return "\n".join(t for t in texts if t)


def tool_to_dict(tool: dict[str, Any]) -> dict[str, Any]:
    """Compact tool description for listing."""
    return {
        "name": tool.get("name"),
        "description": (tool.get("description") or "").split("\n")[0][:120],
    }


# ---- CLI ---------------------------------------------------------------------

def _cmd_verify(client: TapetideMcpClient, _args: argparse.Namespace) -> int:
    print(f"Endpoint:    {client.url}")
    print(f"Auth method: {'Bearer token' if client.token else 'NONE (anonymous)'}")
    try:
        info = client.initialize()
    except McpError as exc:
        print(f"AUTH:        ❌ FAILED — {exc}")
        return 2
    server = info.get("serverInfo", {})
    print(f"AUTH:        ✅ OK")
    print(f"Server:      {server.get('name', '?')} v{server.get('version', '?')}")
    try:
        tools = client.list_tools()
    except McpError as exc:
        print(f"TOOLS:       ❌ {exc}")
        return 2
    print(f"TOOLS:       {len(tools)} available")
    for tool in tools:
        d = tool_to_dict(tool)
        print(f"  - {d['name']:<28} {d['description']}")
    print("MCP CONNECTION VERIFIED")
    return 0


def _cmd_tools(client: TapetideMcpClient, _args: argparse.Namespace) -> int:
    tools = client.list_tools()
    print(json.dumps([tool_to_dict(t) for t in tools], indent=2))
    return 0


def _cmd_call(client: TapetideMcpClient, args: argparse.Namespace) -> int:
    try:
        arguments = json.loads(args.args) if args.args else {}
    except json.JSONDecodeError as exc:
        print(f"Invalid --args JSON: {exc}", file=sys.stderr)
        return 2
    result = client.call_tool(args.tool, arguments)
    text = extract_text(result) if isinstance(result, dict) else str(result)
    if args.pretty:
        try:
            print(json.dumps(json.loads(text), indent=2)[:20000])
            return 0
        except (json.JSONDecodeError, TypeError):
            pass
    print(text[:20000])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Minimal MCP client for the Tapetide MCP server."
    )
    parser.add_argument("--token", default=None,
                        help="API token (falls back to TAPETIDE_TOKEN env var)")
    parser.add_argument("--url", default=MCP_URL, help="MCP endpoint URL")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify", help="Verify authentication and list tools")
    sub.add_parser("tools", help="List available tools as JSON")
    call_p = sub.add_parser("call", help="Call a tool")
    call_p.add_argument("tool", help="Tool name, e.g. get_stock_quote")
    call_p.add_argument("--args", default=None,
                        help='Tool arguments as JSON, e.g. \'{"symbol": "SBIN"}\'')
    call_p.add_argument("--pretty", action="store_true",
                        help="Pretty-print JSON output")

    args = parser.parse_args(argv)
    client = TapetideMcpClient(token=args.token, url=args.url)

    if args.command == "verify":
        return _cmd_verify(client, args)
    if args.command == "tools":
        return _cmd_tools(client, args)
    if args.command == "call":
        return _cmd_call(client, args)
    parser.print_usage()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
