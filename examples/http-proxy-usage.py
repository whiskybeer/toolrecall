"""Example: ToolRecall for Claude Code / Codex / Cursor.

These agents use HTTP endpoints. Start the proxy:
    toolrecall serve

Then configure the agent to use http://localhost:8567 as a tool proxy.
"""

USAGE_HTTP = """
# Start the ToolRecall proxy
$ toolrecall serve
|ToolRecall HTTP proxy running on http://localhost:8567

# Available endpoints:
# GET /cached_read?path=/path/to/file
# GET /cached_terminal?cmd=git+status
# GET /cached_skill?name=skill-name
# GET /docs_search?query=keyword
# GET /health

# Example with curl:
curl "http://localhost:8567/cached_read?path=README.md"
curl "http://localhost:8567/cached_terminal?cmd=hostname"
curl "http://localhost:8567/docs_search?query=deployment"
"""

if __name__ == "__main__":
    print(USAGE_HTTP)