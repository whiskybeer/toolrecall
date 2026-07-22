# ToolRecall Go Client — `tr`

A standalone Go binary that connects to the ToolRecall daemon over a Unix Domain Socket.
Use it from any language or any agent — no Python dependency needed.

## Why?

The Go client exists for agents and tools that **don't run on Python**:

| Agent | Runtime | Use `tr` when... |
|-------|---------|-----------------|
| **OpenCode** | Node.js | Shell-level caching in prompt templates, build scripts, or any shell command — no MCP tools needed |
| **Claude Code** | Node.js | Cached file reads and terminal commands from shell, without adding MCP servers to Claude Code |
| **Cursor** | Node.js | Replace `cat file.py` with `tr cat file.py` for cached reads |
| **Any shell script** | Bash | `tr read config.yml` is faster than `cat config.yml` on repeat |
| **CI/CD** | Any | `tr read deployment.yml` in any pipeline step |
| **Rust / Ruby / Java** | Any | Shell out to `tr` for cached reads — no Python runtime needed |
| **herdr panes** | Any | Every agent in a herdr pane can call `tr read`, `tr term` directly — same daemon, shared cache |

## Build (recommended)

Requires Go 1.19+. Install Go from [go.dev](https://go.dev/dl/) or your package manager.

```bash
cd go-client
go build -o /usr/local/bin/tr .
```

The binary is statically linked. No runtime dependencies.

## Install (pre-built)

Download the latest release binary for Linux amd64:

```bash
curl -L https://github.com/whiskybeer/toolrecall/releases/latest/download/tr -o /usr/local/bin/tr
chmod +x /usr/local/bin/tr
```

## Usage

```bash
# Read a file through cache
tr read main.py
tr cat /etc/os-release

# Force a fresh read (skip cache)
tr read --bypass main.py
tr read --refresh config.yml

# Run a terminal command (cached)
tr term "hostname"
tr exec "whoami"

# Write content to a file (invalidates cache)
tr write /tmp/test.txt "hello world"

# Check daemon status
tr status
tr ping

# Help
tr help
```

## How It Works

The `tr` binary speaks the same protocol as the Python client:

1. Connect to `~/.toolrecall/toolrecall.sock` (or `$XDG_RUNTIME_DIR/toolrecall.sock`)
2. Send: `4-byte big-endian length prefix + JSON payload`
3. Receive: same format

The protocol is simple enough that you can call it from any language:

```bash
# Shell equivalent (for debugging)
echo -n '{"cmd":"cached_read","path":"main.py"}' | \
  { printf '%08x' $(wc -c) | xxd -r -p; cat; } | \
  socat - UNIX-CONNECT:~/.toolrecall/toolrecall.sock
```

## Environment

| Variable | Default | Description |
|----------|---------|-------------|
| `TOOLRECALL_TRANSPORT` | auto-detect | Override UDS socket path |

## Related

- **MCP Bridge** (`toolrecall mcp`): For agents that support MCP, the bridge provides
  `read_file`, `write_file`, `patch`, `terminal` as native MCP tools — no `tr` needed.
- **Forward Proxy** (`toolrecall serve`): For caching API responses at the HTTP level.
  Set `OPENAI_BASE_URL=http://localhost:8569/v1` and responses are cached automatically.
- **Python Shim** (`toolrecall shim --install`): For Python-based agents, the shim
  patches `builtins.open()` at the interpreter level — zero config per agent.