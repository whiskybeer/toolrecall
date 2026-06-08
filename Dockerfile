# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

LABEL org.opencontainers.image.source="https://github.com/whiskybeer/toolrecall"
LABEL org.opencontainers.image.description="ToolRecall — L1 Cache & MCP Multiplexer for LLM Agents"
LABEL org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1
ENV TOOLRECALL_HOME=/toolrecall
ENV TOOLRECALL_CACHE_DB=/data/cache.db
ENV TOOLRECALL_KNOWLEDGE_DB=/data/knowledge.db
ENV TOOLRECALL_UDS_PATH=/data/tc.sock

# Create non-root user (used by daemon, proxy, mcp-bridge targets)
RUN groupadd --gid 1001 toolrecall && \
    useradd --uid 1001 --gid toolrecall --create-home --shell /bin/bash toolrecall && \
    mkdir -p /data /projects /toolrecall && \
    chown -R toolrecall:toolrecall /data /projects /toolrecall

WORKDIR /toolrecall

# --- Base install ---
COPY pyproject.toml README.md ./
COPY toolrecall/ ./toolrecall/
RUN pip install --no-cache-dir -e .

# --- Volumes ---
VOLUME ["/data", "/projects"]

# --- Health check ---
HEALTHCHECK --interval=30s --timeout=5s --start-period=3s --retries=3 \
  CMD python3 -c "import os,socket,json; s=socket.socket(socket.AF_UNIX); s.settimeout(3); s.connect(os.environ.get('TOOLRECALL_UDS_PATH','/data/tc.sock')); s.sendall(json.dumps({'action':'ping'}).encode()); r=json.loads(s.recv(4096).decode()); s.close(); exit(0 if r.get('status')=='pong' else 1)"

# ============================================================
# Service: Daemon (cache + MCP multiplexer)
# ============================================================
FROM base AS daemon
USER toolrecall
EXPOSE 8567
CMD ["toolrecall", "daemon", "--foreground"]

# ============================================================
# Service: MCP Bridge (stdio → UDS)
# ============================================================
FROM base AS mcp-bridge
USER toolrecall
CMD ["toolrecall", "mcp"]

# ============================================================
# Service: HTTP Proxy (standalone)
# ============================================================
FROM base AS proxy
USER toolrecall
EXPOSE 8567
CMD ["toolrecall", "serve"]

# ============================================================
# All-in-one: Daemon + Model Runner (Ollama)
# ============================================================
FROM base AS with-ollama

# Install Ollama (benötigt root)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    zstd \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://ollama.com/install.sh | sh

# Expose toolrecall proxy + ollama API
EXPOSE 8567 11434

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Run as root (ollama benötigt root für Device-Zugriff),
# entrypoint.sh wechselt zu toolrecall für den Daemon
ENTRYPOINT ["/entrypoint.sh"]
CMD ["all"]

# ============================================================
# All-in-one: Daemon + Proxy + MCP Bridge (default target)
# ============================================================
FROM base AS full
USER toolrecall
EXPOSE 8567
COPY docker/supervisord.conf /etc/supervisord.conf
RUN pip install --no-cache-dir supervisor
CMD ["supervisord", "-c", "/etc/supervisord.conf"]