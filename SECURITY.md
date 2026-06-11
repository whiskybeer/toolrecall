# Security Architecture & Exploit Mitigation

ToolRecall is designed as a **Zero-Trust Web Application Firewall (WAF)** for Large Language Model (LLM) agents. Because autonomous agents operate on untrusted data (e.g., reading web pages or open-source repositories), they are highly vulnerable to **Prompt Injection**. 

ToolRecall assumes the LLM *will* be compromised. Its architecture ensures that a compromised agent cannot pivot from a prompt injection into a system compromise.

## 1. Input Sanitation & Exploit Mitigations

ToolRecall applies 5 layers of mathematical input sanitation to all data passing between the Agent and the Host OS:

### A. SQL Injection Prevention
All tool arguments and state-hashes are stored in the local SQLite database. 
- **Mitigation:** ToolRecall strictly utilizes parameterized queries (`?` placeholders) via the Python `sqlite3` library (e.g., `conn.execute("SELECT ... WHERE command_hash = ?", (cmd_hash,))`).
- **Result:** It is mathematically impossible for an agent to inject executable SQL commands, drop tables, or manipulate the cache logic via chat payloads.

### B. Directory Traversal (Default-Deny Path Allowlist + Sensitive File Blocklist)
A compromised agent might attempt to read sensitive host files outside its working directory using relative paths (e.g., `read_file("../../../etc/shadow")` or `~/.ssh/id_rsa`).

#### B1. Default-Deny Path Allowlist (Primary Control)
- ToolRecall uses a **default-deny** allowlist: when `mcp.allowed_paths` is empty, NO paths are readable.
- The user MUST explicitly add directories to `allowed_paths` in `config.toml`.
- Every file read is checked against the allowlist via `os.path.realpath()` canonicalization — the resolved absolute path must start with one of the allowed directory prefixes.
- **Consequences of adding a path:** Every file under that directory becomes accessible through ToolRecall's MCP layer. If the agent is prompt-injected, an attacker could read files under allowed paths. For best security, list only directories the agent needs to read.
- Setup: `toolrecall init` walks the user through an interactive security banner explaining these consequences before creating the config.

#### B2. Sensitive File Blocklist (Secondary Safety Net)
- Even within allowed paths, a built-in blocklist prevents access to known credential files: `.env`, `.ssh/` directories, `.pem`/`.key`/`.cert` files, `.gitconfig`, `.npmrc`, `.netrc`, cloud CLI configs (`.aws/`, `.azure/`, `.config/gcloud/`, `.config/gh/`), and others.
- The blocklist is a **path-name regex check** — renaming a file bypasses it. It is a safety net, not a primary security control. Real protection comes from keeping the `allowed_paths` allowlist tightly scoped.
- Both mechanisms compose: the allowlist defines trust (what the agent *may* touch), the blocklist prevents accidental credential disclosure within trusted directories.
- **Design rationale:** The allowlist is the trust boundary (deny by default). The blocklist catches slips — a user who adds `~/` to `allowed_paths` still cannot have their `.env` or `.ssh` read through ToolRecall.

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

## 2. MCP Keyword Access Control

The **MCP Keyword Access Control** (`tool_access_control`) is a **keyword-based access control on MCP tool names**, not an OS sandbox.

**What it is:** A string-substring filter on tool names passing through the MCP multiplexer.

**What it is NOT:**
- ❌ NOT Docker/gVisor process isolation
- ❌ NOT cgroups or namespace-based containment
- ❌ NOT a guarantee against all state-modifying operations

**How it works:**
- Enabled via `[security] tool_access_control = true` in `config.toml`.
- It intercepts every MCP tool call targeting any multiplexed server.
- If the tool name contains a substring from `dangerous_tool_keywords` (e.g. `write`, `delete`, `push`, `commit`), the call is dropped.
- Tools whose names do NOT contain any keyword (e.g. `post_to_slack`, `run_migration`, `execute_query`) pass through — even if they modify state.

**Limitations:**
1. **Substring match only** — `create_comment` is blocked (matches `create`), but `post_message` is not.
2. **Only MCP multiplexer** — direct `cached_terminal` calls bypass this entirely.
3. **Keyword list must be hand-maintained** — new tools need new keywords.
4. **No behavioral analysis** — a tool named `read_and_delete` could be blocked by `delete` but `safe_delete_all` also matches `delete`.
5. **English-only** — non-English tool names bypass the filter.

**Use case:** Safety net for exploratory sessions. For real OS-level sandboxing, combine with Docker, gVisor, or Firecracker.

---

## 3. Network & Secrets Isolation

### Air-Gapped API Keys
Standard agents load API keys into their environment variables, making them trivial to steal via a prompt-injected `echo $GITHUB_TOKEN`.
ToolRecall manages MCP servers internally as isolated subprocesses. The daemon authenticates with external APIs using `~/.toolrecall/.env`. **The LLM never sees the actual tokens**, preventing exfiltration.

### Unix Domain Sockets (IPC)
The Daemon does not open any TCP ports. All communication happens over Unix Domain Sockets (`/run/user/1000/toolrecall.sock`). This renders ToolRecall completely immune to Server-Side Request Forgery (SSRF) and remote port-scanning attacks.

---

## 4. Trust Boundary

| Layer | Trusted | Untrusted |
|---|---|---|
| Daemon process | ✅ Runs on your machine, under your user | ❌ Not audited by third party |
| MCP subprocesses | ✅ Isolated stdio, no network exposure to daemon | ❌ Downstream MCP servers (GitHub API, etc.) |
| LLM agent | ❌ Assumed compromised (prompt injection) | — |
| SQLite cache DB | ✅ Local file, no remote access | ❌ Readable by any process under your user |
| Install path | `pip install toolrecall` — standard PyPI | `bash <(curl ...)` — NOT recommended, not advertised |

**Install security recommendation:** Use `pip install toolrecall`. The repo no longer advertises curl-pipe install. The `scripts/setup.sh` script is intended for use after a `git clone` — verify the source before running it locally.

**Author trust:** The project is authored by Robin Schultka (whiskybeer) as a solo open-source project. There is no third-party security audit, no CVE history, and no formal verification. Apply standard open-source risk assessment before deploying in production.

**Auditability:** The entire codebase is MIT-licensed and readable. The daemon has 176+ unit tests covering cache logic, security gates, and injection vectors. The cognitive-scan and AST-validation test suites are standalone and reproducible (`tests/test_cognitive_scan.py`, `tests/test_ast_security.py`).

---

## 5. Known Limitations

| Claim/Feature | Honest Assessment |
|---|---|
| `allowed_paths` security | **Default-deny:** empty list = NO readable paths. The blocklist (.env, .ssh, .pem) applies even within allowed paths as a secondary safety net. The allowlist is the primary trust boundary — the blocklist catches slips. |
| `tool_access_control = true` | **Substring match on tool names** — not an OS sandbox. A tool named `post_message` passes through even if it modifies state. The keyword list (`write`, `delete`, `push`, etc.) is a best-effort allowlist. For real OS isolation, pair with Docker/gVisor. |
| Deterministic injection detection | **Regex + AST scan, not ML.** Covers ~86% of patterns in the labeled test corpus (70 injection, 50 legitimate). Remaining 14% are encoding-evasion variants, fabricated URLs, and zero-day patterns. The ONNX classifier (cold path fallback) is optional and unverified. |
| Token reduction | **81% fewer input tokens** is measured on a specific workload (13-file project, 3-10x re-reads). Your mileage varies with project structure and agent behavior. |
| Server-side prompt caching | **Requires same-temperature, same-model runs** across turns. Agent-imposed randomness (sampling params, multi-turn conversation drift) busts this. The daemon freezes OS output, but cannot control the LLM API's internal cache policy. |
| Micro-RAG | **Agent must actively drop and re-fetch cache entries.** ToolRecall provides the cache backend — it doesn't enforce eviction. The agent (or its system prompt) decides when to re-fetch. |

---

## 7. Interface Exposure & Default Transport Security

ToolRecall's security depends not just on what it *blocks*, but on what it *exposes*. A caching layer that opens ports or sockets is itself an attack surface.

### Default Transport: UDS (POSIX) / TCP Loopback (Windows)

| Platform | Default Transport | Path/Address | Accessible from |
|----------|-----------------|-------------|-----------------|
| Linux | Unix Domain Socket | `~/.toolrecall/toolrecall.sock` (or `$XDG_RUNTIME_DIR/toolrecall.sock`) | Same user, same machine only. Socket file permissions: `600`. |
| macOS | Unix Domain Socket | `~/.toolrecall/toolrecall.sock` | Same as Linux. |
| Windows | TCP loopback | `127.0.0.1:8568` | Localhost only — no remote access. |

### Agent Connection Path — Never Direct Socket Access

External agents (Claude Code, Cursor, Cline, Hermes) do **not** connect to the socket directly:

```
Agent ──stdio──► toolrecall mcp (bridge) ──TransportClient──► Daemon
                                                                    │
                                                          SecurityGate prüft:
                                                           • allowed_paths
                                                           • _is_sensitive_path()
                                                           • tool_access_control
                                                           • cognitive scan
                                                           • API key air-gap
```

The `toolrecall mcp` bridge process authenticates via UDS to the daemon and applies ALL security checks. An attacker who compromises only the MCP bridge (a stdio subprocess) has no way to bypass the SecurityGate — all commands pass through the daemon's validation loop.

### Shared Memory (SHM) — Not Exposed to Agents

A common concern: "If agents use shared memory, a swarm of compromised agents could read each other's state."

**ToolRecall's architecture does not expose SHM to agents.** The `demo_shm/` proof-of-concept was a latency micro-benchmark (daemon-internal, between process threads) and has been deleted. The actual implementation:

- **Agents → UDS only** (validated, slow path)
- **Daemon internal → SQLite + LRU** (no SHM)

There is no SHM transport in any deployed version of ToolRecall. The architecture never exposed it.

### Multi-User Systems

On a multi-user Linux machine, the UDS socket file is created in the user's home directory (`~/.toolrecall/toolrecall.sock`) with `600` permissions. Only the owning user can connect. A different user on the same machine cannot access the socket even if they know its path — the OS enforces file permissions on AF_UNIX sockets.

### Swarm / Fleet Risk

For deployments at scale (100+ agents on one machine, or agents across machines):

| Risk | Mitigation | Status |
|------|-----------|--------|
| Another process connects to UDS | Socket in `$HOME` with `600` perms | ✅ OS-enforced |
| Cross-machine socket access | UDS is machine-local. TCP bound to `127.0.0.1`. | ✅ Network-isolated |
| Agent reads another agent's cache | Cache entries are process-visible. Cache DB is single-user. | ⚠️ Single-user assumption |
| MCP bridge bypasses SecurityGate | All commands pass through daemon validation. Bridge is a thin proxy. | ✅ Validated per call |

**Summary:** ToolRecall exposes no network ports, no SHM, and no cross-user sockets. The only transport is a user-scoped UDS file (POSIX) or localhost TCP (Windows). Every command passes through SecurityGate validation — no bypass path exists from any agent interface.

---

## 8. Reporting Vulnerabilities

This project is maintained by a solo developer. For security issues, open a GitHub issue with the `security` label or contact the author directly. There is no bug bounty program.