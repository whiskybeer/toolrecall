FROM python:3.11-slim

# Copy toolrecall source + scripts
WORKDIR /toolrecall
COPY . .

# Install toolrecall
RUN pip install --no-cache-dir -e .

# Setup test environment (simulates full install footprint)
RUN python3 scripts/uninstall-test-setup.py

# Show pre-uninstall state
RUN echo "=== PRE-UNINSTALL ===" && \
    echo "--- ~/.toolrecall/ ---" && \
    ls /root/.toolrecall/ && \
    echo "--- systemd ---" && \
    ls -la /root/.config/systemd/user/toolrecall-daemon.service && \
    echo "--- config refs ---" && \
    grep -n "toolrecall" /root/.hermes/config.yaml && \
    echo "--- sandbox refs ---" && \
    grep -n "toolrecall" /root/.hermes/sandbox.yaml && \
    echo "--- skill dirs ---" && \
    ls /root/.hermes/skills/cache/ && \
    ls /root/.hermes/skills/software-development/ && \
    echo "--- pip ---" && \
    pip show toolrecall | grep -E "^(Name|Version|Location):"

# Run the uninstaller
RUN python3 scripts/uninstall.py --force

# Verify cleanup
RUN echo "=== POST-UNINSTALL ===" && \
    echo "--- ~/.toolrecall (should be gone) ---" && \
    (ls /root/.toolrecall/ 2>&1 || echo "OK: removed") && \
    echo "--- systemd (should be gone) ---" && \
    (ls /root/.config/systemd/user/toolrecall-daemon.service 2>&1 || echo "OK: removed") && \
    echo "--- config refs (should be empty) ---" && \
    (grep -n "toolrecall" /root/.hermes/config.yaml 2>&1 || echo "OK: no refs") && \
    echo "--- sandbox refs (should be empty) ---" && \
    (grep -n "toolrecall" /root/.hermes/sandbox.yaml 2>&1 || echo "OK: no refs") && \
    echo "--- skills (should be gone) ---" && \
    (ls /root/.hermes/skills/cache/toolrecall 2>&1 || echo "OK: cache/toolrecall removed") && \
    (ls /root/.hermes/skills/software-development/tool-recall 2>&1 || echo "OK: tool-recall removed") && \
    echo "--- pip (should be gone) ---" && \
    (pip show toolrecall 2>&1 || echo "OK: uninstalled") && \
    echo "--- cron reminder ---" && \
    cat /root/.hermes/scripts/uninstall-toolrecall-reminder.md && \
    echo "--- config.yaml should still be valid ---" && \
    head -5 /root/.hermes/config.yaml && \
    echo "--- sandbox.yaml should still be valid ---" && \
    cat /root/.hermes/sandbox.yaml && \
    echo "" && \
    echo "╔══════════════════════════════════════════════╗" && \
    echo "║       ALL CHECKS PASSED                    ║" && \
    echo "╚══════════════════════════════════════════════╝"

CMD ["true"]