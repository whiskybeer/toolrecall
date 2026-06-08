"""ToolRecall GitHub MCP Server — stdlib-only, no npm.

Optional replacement for `npx -y @modelcontextprotocol/server-github`.
Supply chain: zero dependencies, 100% auditable.

Supports: create_repository, create_or_update_file, push_files, list_commits

Token is loaded from the ToolRecall daemon environment (never exposed to subprocess).
"""
import base64, json, os, sys, urllib.request, urllib.error

TOKEN = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""

if not TOKEN:
    sys.stderr.write("ERROR: No GITHUB_PERSONAL_ACCESS_TOKEN or GITHUB_TOKEN in environment.\n")
    sys.stderr.write("  Set the token in ~/.toolrecall/.env and restart the daemon.\n")
    sys.stderr.flush()

API_BASE = "https://api.github.com"

HEADERS = {
    "Authorization": f"token {TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "User-Agent": "toolrecall-github-mcp",
}

def _api(method, path, data=None):
    url = f"{API_BASE}/{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "message": e.read().decode()[:200]}

def _handle(method, params):
    if method == "create_repository":
        return _api("POST", "user/repos", {
            "name": params["name"],
            "description": params.get("description", ""),
            "private": params.get("private", False),
        })
    elif method == "create_or_update_file":
        owner, repo = params["owner"], params["repo"]
        path, content, branch = params["path"], params["content"], params["branch"]
        data = {"message": params["message"], "content": content, "branch": branch}
        if "sha" in params:
            data["sha"] = params["sha"]
        return _api("PUT", f"repos/{owner}/{repo}/contents/{path}", data)
    elif method == "push_files":
        owner, repo, branch = params["owner"], params["repo"], params["branch"]
        # Create tree from files
        files = params.get("files", params.get("changes", []))
        tree = []
        for f in files:
            tree.append({
                "path": f["path"],
                "mode": "100644",
                "type": "blob",
                "content": base64.b64decode(f["content"]).decode("utf-8", errors="replace"),
            })
        # Get last commit SHA
        ref = _api("GET", f"repos/{owner}/{repo}/git/ref/heads/{branch}")
        if "error" in ref:
            return ref
        last_sha = ref["object"]["sha"]
        commit_data = _api("GET", f"repos/{owner}/{repo}/git/commits/{last_sha}")
        if "error" in commit_data:
            return commit_data
        base_tree = commit_data["tree"]["sha"]
        # Create tree
        new_tree = _api("POST", f"repos/{owner}/{repo}/git/trees", {
            "base_tree": base_tree,
            "tree": tree,
        })
        if "error" in new_tree:
            return new_tree
        # Create commit
        new_commit = _api("POST", f"repos/{owner}/{repo}/git/commits", {
            "message": params.get("message", "Update via ToolRecall"),
            "tree": new_tree["sha"],
            "parents": [last_sha],
        })
        if "error" in new_commit:
            return new_commit
        # Update ref
        return _api("PATCH", f"repos/{owner}/{repo}/git/refs/heads/{branch}", {
            "sha": new_commit["sha"],
            "force": False,
        })
    elif method == "list_commits":
        owner, repo = params["owner"], params["repo"]
        return _api("GET", f"repos/{owner}/{repo}/commits?per_page=10")
    elif method == "list_repos":
        return _api("GET", "user/repos?per_page=30")
    return None

def main():
    """Minimal stdio MCP server loop."""
    import sys
    sys.stderr.write(f"ToolRecall GitHub MCP Server (Python stdlib)\n")
    sys.stderr.write(f"  Token: {TOKEN[:8]}... ({len(TOKEN)} chars)\n" if TOKEN else "  No token!\n")
    sys.stderr.flush()

    tools = [
        {"name": "create_repository", "description": "Create a new GitHub repository",
         "inputSchema": {"type": "object", "properties": {
             "name": {"type": "string"}, "description": {"type": "string"},
             "private": {"type": "boolean"}}, "required": ["name"]}},
        {"name": "create_or_update_file", "description": "Create or update a file",
         "inputSchema": {"type": "object", "properties": {
             "owner": {"type": "string"}, "repo": {"type": "string"},
             "path": {"type": "string"}, "content": {"type": "string"},
             "message": {"type": "string"}, "branch": {"type": "string"},
             "sha": {"type": "string"}}, "required": ["owner", "repo", "path", "content", "message", "branch"]}},
        {"name": "push_files", "description": "Push multiple files in a single commit",
         "inputSchema": {"type": "object", "properties": {
             "owner": {"type": "string"}, "repo": {"type": "string"},
             "branch": {"type": "string"}, "message": {"type": "string"},
             "files": {"type": "array", "items": {"type": "object",
                 "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                 "required": ["path", "content"]}}},
             "required": ["owner", "repo", "branch", "files"]}},
        {"name": "list_commits", "description": "List recent commits",
         "inputSchema": {"type": "object", "properties": {
             "owner": {"type": "string"}, "repo": {"type": "string"}},
             "required": ["owner", "repo"]}},
        {"name": "list_repos", "description": "List user repos",
         "inputSchema": {"type": "object", "properties": {}, "required": []}},
    ]

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = req.get("id", 0)
        method = req.get("method", "")
        params = req.get("params", {})
        resp = {"jsonrpc": "2.0", "id": rid}

        if method == "initialize":
            resp["result"] = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "toolrecall-github", "version": "0.1.0",
                               "security": {"token_local": True}},
            }
        elif method == "tools/list":
            resp["result"] = {"tools": tools}
        elif method == "tools/call":
            tn = params.get("name", "")
            args = params.get("arguments", {})
            result = _handle(tn, args)
            if result is None:
                resp["error"] = {"code": -32601, "message": f"Unknown: {tn}"}
            else:
                resp["result"] = {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}
        elif method in ("notifications/initialized", "close"):
            continue
        else:
            resp["error"] = {"code": -32601, "message": f"Method not found: {method}"}

        out = json.dumps(resp) + "\n"
        sys.stdout.write(out)
        sys.stdout.flush()

if __name__ == "__main__":
    main()
