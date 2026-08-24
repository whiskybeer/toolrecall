# ToolRecall — Developer Makefile
#
# This Makefile is for **contributors and maintainers only**.
# End-users install via `pip install toolrecall` or `pipx install toolrecall`.
#
# Key design choices:
#   - All commands use `uv run` for speed (uv replaces pip install in editable mode)
#   - If uv isn't installed, a comment flag triggers automatic pip fallback
#   - Test, lint, format, and type-check commands match CI gate expectations
#   - Dev-server target orchestrates daemon + MCP bridge + proxy locally
#
.ONESHELL:
SHELL := /bin/bash

# ─── Detect uv availability ───────────────────────────────────
HAVE_UV := $(shell command -v uv >/dev/null 2>&1 && echo yes || echo no)
ifeq ($(HAVE_UV),yes)
    PY_RUN   := uv run
    PYTHON   := uv run python
    PIP_INST := uv pip install
    PIP_SYNC := uv sync --extra dev
    VENV_ACT := . .venv/bin/activate
else
    $(warning uv not found — falling back to pip. Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh)
    PY_RUN   := python3
    PYTHON   := python3
    PIP_INST := pip install
    PIP_SYNC := pip install -e ".[dev]"
    VENV_ACT := :
endif

# ─── Default target ────────────────────────────────────────────
.PHONY: help
help:
	@echo "ToolRecall Developer Commands"
	@echo ""
	@echo "Bootstrap"
	@echo "  make setup          Install dev deps in editable mode"
	@echo "  make sync           Sync venv with lock file (uv only)"
	@echo ""
	@echo "Testing"
	@echo "  make test           Run full test suite"
	@echo "  make test-unit      Release-gate selection (excl. e2e/ADK)"
	@echo "  make test-fast      Run unit tests only (skip e2e)"
	@echo "  make test-e2e       Run end-to-end daemon tests"
	@echo "  make test-file      Run a single test file (FILE=tests/test_*.py)"
	@echo "  make test-kw        Run tests matching keyword (KW=registry)"
	@echo ""
	@echo "Lint & Format"
	@echo "  make lint           Run ruff check"
	@echo "  make format         Run ruff format"
	@echo "  make check          Lint + format check (CI gate)"
	@echo ""
	@echo "Type Checking"
	@echo "  make type           Run mypy (if installed)"
	@echo ""
	@echo "Pre-push Gate"
	@echo "  make validate       check + type + test-unit in one command"
	@echo ""
	@echo "Benchmarks"
	@echo "  make bench-env      Create/install /tmp/bench-env harness venv"
	@echo "  make bench-run      Single arm (ARM= WORKLOAD= SEED= TURNS=)"
	@echo "  make bench-interleave  Interleaved paired run (WORKLOAD= SEEDS= TURNS=)"
	@echo "  make bench-dry      Dry run, no API calls (ARM= WORKLOAD=)"
	@echo "  make bench-analyze  Charts + report from benchmark DB"
	@echo ""
	@echo "Docker"
	@echo "  make docker-build   docker compose build"
	@echo "  make docker-up      Build + start stack"
	@echo "  make docker-down    docker compose down"
	@echo "  make docker-test    Build + run Dockerfile.test image"
	@echo ""
	@echo "Clean"
	@echo "  make clean          Remove cache, temp files, __pycache__"
	@echo "  make clean-all      Also remove .venv and build artifacts"
	@echo ""
	@echo "Dev Server (local MCP + Daemon + Proxy)"
	@echo "  make dev-server     Start full dev stack: daemon + proxy + MCP bridge"
	@echo "  make dev-daemon     Start daemon only (foreground)"
	@echo "  make dev-mcp        Start MCP bridge only (needs running daemon)"
	@echo "  make dev-proxy      Start forward proxy only (needs running daemon)"
	@echo "  make dev-stop       Stop all dev processes"
	@echo ""
	@echo "Package"
	@echo "  make build          Build wheel + sdist"
	@echo "  make publish-prep   Check build, tag, dry-run"
	@echo ""
	@echo "Deploy"
	@echo "  make reinstall      Reinstall from local tree (pipx --force)"
	@echo "  make daemon-restart Restart systemd daemon"

# ─── Bootstrap ────────────────────────────────────────────────

.PHONY: setup
setup:
	$(VENV_ACT)
	$(PIP_SYNC)
	$(PY_RUN) -c "import toolrecall; print(f'✓ toolrecall v{toolrecall.__version__}')"

.PHONY: sync
sync:
	uv sync --frozen --extra dev

# ─── Testing ──────────────────────────────────────────────────

.PHONY: test
test: test-dedup
	$(PY_RUN) -m pytest tests/ -v --tb=short --no-header

# Selection mirrors the release gate (scripts/release.sh stage 6). Single
# source of truth for what ships — used by both `make validate` and the release.
.PHONY: test-unit
test-unit:
	$(PY_RUN) -m pytest tests/ -q --tb=short -p no:capture -k "not test_client_without_daemon and not e2e and not adk and not gemini"

.PHONY: test-fast
test-fast: test-dedup
	$(PY_RUN) -m pytest tests/ -v --tb=short --no-header -k "not e2e"

.PHONY: test-e2e
test-e2e:
	$(PY_RUN) -m pytest tests/ -v --tb=short --no-header -k "e2e"

.PHONY: test-file
test-file:
	$(PY_RUN) -m pytest $(FILE) -v --tb=short

.PHONY: test-dedup
test-dedup:
	$(PY_RUN) bench/litellm_dedup/property_test_quality.py

.PHONY: test-kw
test-kw:
	$(PY_RUN) -m pytest tests/ -v --tb=short -k "$(KW)"

# ─── Lint & Format ────────────────────────────────────────────

.PHONY: lint
lint:
	$(PY_RUN) ruff check toolrecall/ tests/ bench/ scripts/

.PHONY: format
format:
	$(PY_RUN) ruff format toolrecall/ tests/ bench/ scripts/

# CI gate. Note: bench/ deliberately excluded from `check` — it contains scratch
# + string-patch scripts (fix_clean.py, final_fix.py) whose embedded patch strings
# ruff reformatting would corrupt; lint/format still cover them for dev hygiene.
.PHONY: check
check:
	$(PY_RUN) ruff check toolrecall/ tests/
	$(PY_RUN) ruff format --check toolrecall/ tests/

# ─── Type Checking ────────────────────────────────────────────

.PHONY: type
type:
	@if $(PYTHON) -c "import mypy" 2>/dev/null; then
		$(PYTHON) -m mypy toolrecall/ --ignore-missing-imports --strict-optional
	else
		@echo "⚠️  mypy not installed. Run: $(PIP_INST) mypy"
	fi

# ─── Pre-push gate ─────────────────────────────────────────────
# One command combining lint + format-check + type-check + the release test
# selection. This is what CI / a pre-push hook should run.
.PHONY: validate
validate: check type test-unit
	@echo "✓ validate passed (lint + format + type + unit tests)"

# ─── Benchmarks ─────────────────────────────────────────────────
# Three-arm benchmark (naive / prefix / toolrecall), see bench/README.md.
# bench deps (tiktoken, numpy, pandas, matplotlib, scipy) are NOT in the
# project's dev extras, so the harness uses its own venv at /tmp/bench-env.
# The toolrecall arm imports `toolrecall.client`, so the repo root is put on
# PYTHONPATH (mirrors the README's `PYTHONPATH=~/toolrecall`).
BENCH_ENV := /tmp/bench-env
BENCH_BIN := $(BENCH_ENV)/bin
BENCH_PY  := $(BENCH_BIN)/python3
BENCH_REQ := tiktoken numpy pandas matplotlib scipy
# Exported so the sub-process can `import toolrecall` from the repo root.
export PYTHONPATH := $(CURDIR)

.PHONY: bench-env
bench-env:
	@if [ ! -x $(BENCH_PY) ]; then \
		python3 -m venv $(BENCH_ENV); \
	fi
	@$(BENCH_BIN)/pip install -q $(BENCH_REQ)
	@echo "✓ bench env ready at $(BENCH_ENV)"

# Single arm:  make bench-run ARM=toolrecall WORKLOAD=review SEED=42 TURNS=30
.PHONY: bench-run
bench-run: bench-env
	$(BENCH_PY) bench/run_arm.py $(ARM) $(WORKLOAD) --seed $(or $(SEED),42) --max-turns $(or $(TURNS),30)

# Interleaved paired run: make bench-interleave WORKLOAD=review SEEDS=3 TURNS=200
.PHONY: bench-interleave
bench-interleave: bench-env
	$(BENCH_PY) bench/interleave.py $(WORKLOAD) --seeds $(or $(SEEDS),3) --max-turns $(or $(TURNS),200)

# Dry run (tests plumbing, no API calls): make bench-dry ARM=toolrecall WORKLOAD=review
.PHONY: bench-dry
bench-dry: bench-env
	$(BENCH_PY) bench/run_arm.py $(ARM) $(WORKLOAD) --seed $(or $(SEED),42) --max-turns $(or $(TURNS),5) --dry-run

# Charts + report from ~/.toolrecall/benchmark.db
.PHONY: bench-analyze
bench-analyze:
	$(BENCH_PY) bench/analyze.py

# ─── Docker ─────────────────────────────────────────────────────
.PHONY: docker-build
docker-build:
	docker compose build

.PHONY: docker-up
docker-up: docker-build
	docker compose up -d
	docker compose ps

.PHONY: docker-down
docker-down:
	docker compose down

# Build + run the self-contained test image (Dockerfile.test)
.PHONY: docker-test
docker-test:
	docker build -f Dockerfile.test -t toolrecall-test .
	docker run --rm toolrecall-test

# ─── Clean ────────────────────────────────────────────────────

.PHONY: clean
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete
	find . -type d -name '*.egg-info' -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	@echo "✓ Cleaned"

.PHONY: clean-all
clean-all: clean
	rm -rf .venv dist build
	@echo "✓ Cleaned all (venv, build artifacts)"

# ─── Dev Server (local orchestration) ─────────────────────────

# Start daemon in background, wait for socket readiness
.PHONY: dev-daemon
dev-daemon:
	$(PY_RUN) -m toolrecall.cli daemon --foreground &
	@echo "Waiting for daemon socket..."
	@for i in $$(seq 1 10); do
		if [ -S ~/.toolrecall/tc.sock ] || [ -S /tmp/toolrecall.sock ]; then
			echo "✓ Daemon ready (socket found)"
			break
		fi
		sleep 0.5
	done

# Start forward proxy on :8569 (needs daemon)
.PHONY: dev-proxy
dev-proxy:
	$(PY_RUN) -m toolrecall.cli serve &
	@echo "✓ Forward proxy starting on :8569"

# Start MCP bridge (stdio → daemon, needs daemon)
.PHONY: dev-mcp
dev-mcp:
	@echo "Starting MCP Bridge (stdio → daemon)..."
	$(PY_RUN) -m toolrecall.cli mcp

# Full stack: daemon + proxy + MCP bridge
.PHONY: dev-server
dev-server:
	@echo "═══ Starting ToolRecall Dev Server ═══"
	$(MAKE) dev-daemon
	$(MAKE) dev-proxy
	@echo ""
	@echo "Dev stack running:"
	@echo "  ✓ Daemon    (Unix socket)"
	@echo "  ✓ Proxy     (localhost:8569)"
	@echo ""
	@echo "Run 'toolrecall mcp' in another terminal for the MCP bridge."
	@echo "Or connect your agent's MCP config to 'python -m toolrecall.cli mcp'."
	@echo "Run 'make dev-stop' to stop."

# Stop all toolrecall processes
.PHONY: dev-stop
dev-stop:
	@pkill -f "toolrecall.*daemon" 2>/dev/null && echo "✓ Daemon stopped" || echo "  (daemon not running)"
	@pkill -f "toolrecall.*serve" 2>/dev/null && echo "✓ Proxy stopped" || echo "  (proxy not running)"
	@pkill -f "toolrecall.*mcp" 2>/dev/null && echo "✓ MCP bridge stopped" || echo "  (MCP bridge not running)"
	@echo ""
	@echo "All ToolRecall processes stopped."

# ─── Package ──────────────────────────────────────────────────

.PHONY: build
build:
	$(PY_RUN) -m build
	@echo "✓ Built:"
	@ls -lh dist/

.PHONY: publish-prep
publish-prep:
	@echo "=== Pre-publish Checklist ==="
	$(PY_RUN) -m build
	@echo ""
	@echo "To publish:"
	@echo "  1. twine check dist/*"
	@echo "  2. twine upload dist/*"
	@echo ""
	@echo "Or with uv:"
	@echo "  uv publish"

# ─── Release ────────────────────────────────────────────────────

.PHONY: release
release:
	./scripts/release.sh $(VERSION)

.PHONY: reinstall
reinstall:
	pipx install --force . 2>&1
	@echo "✓ Reinstalled from local tree"

.PHONY: daemon-restart
daemon-restart:
	systemctl --user restart toolrecall-daemon 2>/dev/null || toolrecall daemon --restart
	@echo "✓ Daemon restarted"
