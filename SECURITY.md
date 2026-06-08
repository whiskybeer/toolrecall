# Security Architecture & Exploit Mitigation

ToolRecall is designed as a **Zero-Trust Web Application Firewall (WAF)** for Large Language Model (LLM) agents. Because autonomous agents operate on untrusted data (e.g., reading web pages or open-source repositories), they are highly vulnerable to **Prompt Injection**. 

ToolRecall assumes the LLM *will* be compromised. Its architecture ensures that a compromised agent cannot pivot from a prompt injection into a system compromise.

## 1. Input Sanitation & Exploit Mitigations

ToolRecall applies 5 layers of mathematical input sanitation to all data passing between the Agent and the Host OS:

### A. SQL Injection Prevention
All tool arguments and state-hashes are stored in the local SQLite database. 
- **Mitigation:** ToolRecall strictly utilizes parameterized queries (`?` placeholders) via the Python `sqlite3` library (e.g., `conn.execute("SELECT ... WHERE command_hash = ?", (cmd_hash,))`).
- **Result:** It is mathematically impossible for an agent to inject executable SQL commands, drop tables, or manipulate the cache logic via chat payloads.

### B. Directory Traversal (Path Canonicalization)
A compromised agent might attempt to read sensitive host files outside its working directory using relative paths (e.g., `read_file("../../../etc/shadow")` or `~/.ssh/id_rsa`).
- **Mitigation:** ToolRecall resolves every requested path through a strict cryptographic canonicalization process (`os.path.realpath()`). It computes the absolute, symlink-free target path and compares it against the user-defined `allowed_paths` array in `config.toml`.
- **Result:** If the resolved path falls outside the allowed tree, the Daemon drops the payload immediately with an `Access Denied` error before the OS filesystem is touched.

### C. Buffer Overflows & OOM (Out-of-Memory) Attacks
A malicious payload might attempt to crash the ToolRecall Daemon by streaming gigabytes of data into the context or asking the tool to read a 10GB log file, exhausting RAM.
- **Mitigation (IPC Layer):** The Unix Domain Socket daemon enforces a strict 4-byte header check. If the incoming JSON payload exceeds **1 Megabyte**, the connection is instantly severed (`Request too large`) before the string is decoded into memory.
- **Mitigation (File Layer):** The `cached_read` tool enforces a hard **5 Megabyte** limit using `os.stat()`. Files larger than 5MB are rejected to protect both the Daemon's RAM and the LLM's context window.

### D. Shell Command Escapes (`shlex`)
If terminal execution is enabled (`allow_terminal = true`), an injected agent might attempt to concatenate destructive commands (e.g., `git status; rm -rf /`).
- **Mitigation:** ToolRecall parses caching-safe shell commands through Python's `shlex.split(posix=True)`. This ensures that concatenated malicious instructions are treated as literal string arguments to the primary command, rather than being evaluated by a sub-shell.

### E. Protocol Strictness
- **Mitigation:** ToolRecall does not accept raw text. All inputs over the multiplexer must be strictly formed JSON-RPC 2.0 payloads. Malformed escape sequences or corrupted payloads crash the `json.loads()` decoder instantly and are discarded.

---

## 2. The Ultimate Read-Only Sandbox

The strongest feature of ToolRecall's security posture is the **Read-Only Sandbox**. 

By enabling `[security] read_only_sandbox = true` in `config.toml`, ToolRecall acts as a semantic firewall for the Model Context Protocol (MCP).
- It intercepts every tool call targeting *any* of your multiplexed servers (GitHub, Bash, Postgres, etc.).
- If the requested tool name contains modifying verbs defined in `dangerous_tool_keywords` (e.g., `write`, `execute`, `delete`, `push`, `commit`), the payload is dropped entirely.

This allows developers to unleash autonomous agents on their entire local codebase or database with the mathematical guarantee that the agent is physically unable to alter a single byte of state.

---

## 3. Network & Secrets Isolation

### Air-Gapped API Keys
Standard agents load API keys into their environment variables, making them trivial to steal via a prompt-injected `echo $GITHUB_TOKEN`.
ToolRecall manages MCP servers internally as isolated subprocesses. The daemon authenticates with external APIs using `~/.toolrecall/.env`. **The LLM never sees the actual tokens**, preventing exfiltration.

### Unix Domain Sockets (IPC)
The Daemon does not open any TCP ports. All communication happens over Unix Domain Sockets (`/run/user/1000/toolrecall.sock`). This renders ToolRecall completely immune to Server-Side Request Forgery (SSRF) and remote port-scanning attacks.