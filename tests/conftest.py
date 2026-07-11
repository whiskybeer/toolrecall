"""conftest for e2e test files — ensures daemon_running() is real."""
import toolrecall.client


def pytest_runtest_setup(item):
    """Before every test, restore daemon_running() from the actual module."""
    # If this test is marked e2e, we need the real daemon_running
    if item.get_closest_marker("e2e") or item.get_closest_marker("adk"):
        import importlib
        # Reload to get the original function
        importlib.reload(toolrecall.client)
        from toolrecall.client import daemon_running as real_func
        toolrecall.client.daemon_running = real_func