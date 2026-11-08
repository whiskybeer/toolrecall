"""The debug server's GET /read must resolve the path in-process before sending.

`toolrecall debug` serves `GET /read?path=X` on localhost. Its own help text and
the docs advertise a *relative* argument (`curl .../read?path=README.md`), so the
path must be resolved against the SERVER's working directory — the daemon opens
it against its own cwd instead, which served whatever the daemon happened to have
and shared one cache entry across projects (see cache._resolve_cache_path).
"""

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall.proxy import DebugHandler


class _CapturedClient:
    """Records the payload handed to the daemon client."""

    def __init__(self, response=None):
        self.payload = {}
        self._response = response if response is not None else {"content": "x"}

    def send(self, payload):
        self.payload = dict(payload)
        return self._response


class _ReadHandler(DebugHandler):
    """DebugHandler without BaseHTTPRequestHandler's socket setup."""

    def __init__(self, request_path, client):
        self.path = request_path
        self._client = client
        self.wfile = io.BytesIO()
        self.status = None

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, keyword, value):
        pass

    def end_headers(self):
        pass


class TestDebugServerReadPath(unittest.TestCase):
    def test_read_endpoint_resolves_relative_path(self):
        client = _CapturedClient()
        handler = _ReadHandler("/read?path=README.md", client)
        handler.do_GET()

        self.assertEqual(
            client.payload.get("path"),
            os.path.join(os.getcwd(), "README.md"),
            client.payload,
        )

    def test_read_endpoint_keeps_absolute_path(self):
        client = _CapturedClient()
        handler = _ReadHandler("/read?path=/tmp/definitely-absolute.txt", client)
        handler.do_GET()

        self.assertEqual(client.payload.get("path"), "/tmp/definitely-absolute.txt")

    def test_read_endpoint_still_reports_missing_param(self):
        """The empty-argument branch must keep working."""
        client = _CapturedClient()
        handler = _ReadHandler("/read", client)
        handler.do_GET()

        self.assertEqual(client.payload, {})


if __name__ == "__main__":
    unittest.main()
