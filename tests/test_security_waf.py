import unittest
from unittest.mock import MagicMock
import os

# Import the Daemon components to test the MCP keyword access control
from toolrecall.daemon import SecurityGate
from toolrecall.config import Config

class TestSecurityWAF(unittest.TestCase):
    def setUp(self):
        # Create a mock config
        self.mock_cfg = MagicMock(spec=Config)
        self.mock_cfg.mcp_allowed_paths = []
        self.mock_cfg.mcp_allow_terminal = False
        self.mock_cfg.mcp_allowed_terminal_commands = []
        self.mock_cfg.mcp_allow_invalidate = False
        self.mock_cfg.mcp_multiplex_enabled = True
        self.mock_cfg.mcp_multiplex_servers = []
        self.mock_cfg.mcp_read_only_sandbox = True
        self.mock_cfg.mcp_dangerous_tool_keywords = ["write", "edit", "delete", "remove", "terminal", "bash", "exec", "run", "push", "commit", "update", "create"]

    def test_sandbox_blocks_dangerous_tools(self):
        """Verify tools whose names contain dangerous keywords are blocked"""
        security = SecurityGate(self.mock_cfg)
        
        blocked_tools = [
            "write_file",
            "edit_code",
            "execute_bash",
            "create_pull_request",
            "git_commit",
            "delete_table"
        ]
        
        for tool in blocked_tools:
            err = security.check_mcp_tool_sandbox(tool)
            self.assertIsNotNone(err, f"Tool '{tool}' should have been BLOCKED.")
            self.assertIn("ToolRecall MCP Access Control", err)

    def test_sandbox_allows_safe_tools(self):
        """Verify tools without dangerous keywords are allowed even when read_only_sandbox=True"""
        security = SecurityGate(self.mock_cfg)
        
        allowed_tools = [
            "read_file",
            "list_issues",
            "search_codebase",
            "get_status",
            "fetch_logs"
        ]
        
        for tool in allowed_tools:
            err = security.check_mcp_tool_sandbox(tool)
            self.assertIsNone(err, f"Tool '{tool}' should have been ALLOWED.")

    def test_sandbox_disabled_allows_all(self):
        """Verify that disabling the access control allows all tools through"""
        self.mock_cfg.mcp_read_only_sandbox = False
        security = SecurityGate(self.mock_cfg)
        
        err = security.check_mcp_tool_sandbox("write_file")
        self.assertIsNone(err, "Tool should be allowed when access control is disabled.")

    def test_directory_traversal_waf(self):
        """Verify path allowlists prevent directory traversal via realpath resolution"""
        safe_base = os.path.realpath("/tmp/safe_dir")
        self.mock_cfg.mcp_allowed_paths = [safe_base]
        security = SecurityGate(self.mock_cfg)
        
        # Safe path inside the dir
        safe_path = os.path.join(safe_base, "test.txt")
        self.assertIsNone(security.check_read_path(safe_path))
        
        # Malicious directory traversal attempt
        malicious_path = os.path.join(safe_base, "../../etc/passwd")
        err = security.check_read_path(malicious_path)
        self.assertIsNotNone(err, "Directory traversal must be blocked.")
        self.assertIn("Path not allowed", err)

if __name__ == "__main__":
    unittest.main()