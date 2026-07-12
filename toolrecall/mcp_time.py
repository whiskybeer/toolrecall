"""ToolRecall Time MCP Server — stdlib-only.
Optional replacement for `npx -y @modelcontextprotocol/server-time`.
Zero dependencies, ~10 lines of actual logic.
"""
import json
import sys
from datetime import datetime, timezone, timedelta

TOOLS = [
    {"name": "get_time", "description": "Get current time in a timezone",
     "inputSchema": {"type": "object", "properties": {
         "timezone": {"type": "string", "description": "e.g. UTC, America/New_York"}},
         "required": ["timezone"]}},
    {"name": "list_timezones", "description": "List available timezone names",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
]

def _utc_offset(name: str) -> timedelta | None:
    """Simple UTC offset parser for common zones — no pytz needed."""
    ZONES = {
        "UTC": timedelta(0), "GMT": timedelta(0),
        "EST": timedelta(hours=-5), "EDT": timedelta(hours=-4),
        "CST": timedelta(hours=-6), "CDT": timedelta(hours=-5),
        "MST": timedelta(hours=-7), "MDT": timedelta(hours=-6),
        "PST": timedelta(hours=-8), "PDT": timedelta(hours=-7),
        "CET": timedelta(hours=1), "CEST": timedelta(hours=2),
        "EET": timedelta(hours=2), "EEST": timedelta(hours=3),
        "IST": timedelta(hours=5, minutes=30),
        "JST": timedelta(hours=9), "KST": timedelta(hours=9),
        "AEST": timedelta(hours=10), "AEDT": timedelta(hours=11),
        "NZST": timedelta(hours=12), "NZDT": timedelta(hours=13),
    }
    return ZONES.get(name.upper())

def _handle(method, params):
    if method == "get_time":
        tz_name = params.get("timezone", "UTC")
        offset = _utc_offset(tz_name)
        if offset is None:
            return {"error": f"Unknown timezone: {tz_name}. Use list_timezones for supported zones."}
        now = datetime.now(timezone.utc) + offset
        return {"timezone": tz_name, "utc_offset": str(offset),
                "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
                "iso": now.isoformat()}
    elif method == "list_timezones":
        return {"timezones": sorted(["UTC", "GMT", "EST", "EDT", "CST", "CDT",
                "MST", "MDT", "PST", "PDT", "CET", "CEST", "EET", "EEST",
                "IST", "JST", "KST", "AEST", "AEDT", "NZST", "NZDT"])}
    return None

def main():
    sys.stderr.write("ToolRecall Time MCP Server (Python stdlib, zero deps)\n")
    sys.stderr.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid, method, params = req.get("id", 0), req.get("method", ""), req.get("params", {})
        resp = {"jsonrpc": "2.0", "id": rid}
        if method == "initialize":
            resp["result"] = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "toolrecall-time", "version": "0.1.0"}}
        elif method == "tools/list":
            resp["result"] = {"tools": TOOLS}
        elif method == "tools/call":
            result = _handle(params.get("name", ""), params.get("arguments", {}))
            if result is None:
                resp["error"] = {"code": -32601, "message": "Unknown tool"}
            else:
                resp["result"] = {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}
        elif method in ("notifications/initialized", "close"):
            continue
        else:
            resp["error"] = {"code": -32601, "message": f"Unknown method: {method}"}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()

if __name__ == "__main__":
    main()
