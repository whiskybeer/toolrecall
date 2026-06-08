"""ToolRecall MCP Client Simulator

This script demonstrates how an autonomous agent (like Claude Code)
interacts with the ToolRecall daemon over standard JSON-RPC (stdio).

This proves that ToolRecall is completely framework agnostic and 
requires zero custom SDKs.
"""
import subprocess
import json
import time

def send_rpc(proc, method, params=None, req_id=None):
    """Helper to send JSON-RPC over stdin."""
    payload = {"jsonrpc": "2.0", "method": method}
    if params is not None: payload["params"] = params
    if req_id is not None: payload["id"] = req_id
    
    msg = json.dumps(payload) + "\n"
    proc.stdin.write(msg.encode("utf-8"))
    proc.stdin.flush()

def read_rpc(proc):
    """Helper to read JSON-RPC from stdout."""
    line = proc.stdout.readline()
    if not line: return None
    return json.loads(line.decode("utf-8").strip())

print("1. Starting 'toolrecall mcp' bridge as subprocess...")
proc = subprocess.Popen(["toolrecall", "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

try:
    print("2. Sending Handshake (Initialize)...")
    send_rpc(proc, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "python-demo-agent", "version": "1.0"}
    }, req_id=1)
    
    response = read_rpc(proc)
    print(f" -> Connected to: {response['result']['serverInfo']['name']}")
    
    # Complete handshake
    send_rpc(proc, "notifications/initialized")
    
    print("\n3. Sending a 'tools/call' request for a file read...")
    send_rpc(proc, "tools/call", {
        "name": "cached_read",
        "arguments": {"path": "~/.toolrecall/config.toml"}
    }, req_id=2)
    
    response = read_rpc(proc)
    if response is None:
        print(" -> Connection closed by server.")
    elif "error" in response:
        print(f" -> Server returned error: {response['error']}")
    elif "result" in response and response["result"] is not None and "content" in response["result"]:
        result_json = response['result']['content'][0]['text']
        result_data = json.loads(result_json)
        if "error" in result_data:
            print(f" -> Tool returned error: {result_data['error']}")
        else:
            print(f" -> Cached: {result_data.get('cached', False)}")
            print(f" -> Content length: {len(result_data.get('content', ''))} bytes")
    else:
        print(f" -> Unexpected response: {response}")

finally:
    proc.terminate()
