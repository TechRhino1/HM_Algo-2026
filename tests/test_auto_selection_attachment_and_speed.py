"""Tests that verify:
1. Auto-selection engine is ALWAYS attached and never reports 503 / "Live orchestrator is not attached".
2. Response is sub-50ms when served from cache / background warm-up.
3. Server started without orchestrator automatically self-heals and wires orchestrator.
"""
import time
import json
import threading
import unittest
import urllib.request

from jarvis.api.server import start_server, JarvisRequestHandler
from jarvis.api.intelligence_api import INTELLIGENCE


class AutoSelectionAttachmentAndSpeedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Start server with orchestrator=None (the exact condition that caused the bug on 8502)
        JarvisRequestHandler.orchestrator = None
        INTELLIGENCE.configure_orchestrator(None)
        cls.server = start_server(host="127.0.0.1", port=0, orchestrator=None)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
            cls.server.server_close()
            cls.thread.join(timeout=3)
        except Exception:
            pass

    def test_auto_selection_engine_attached_and_returns_ok(self):
        url = f"http://127.0.0.1:{self.port}/api/intelligence/auto-selection"
        req = urllib.request.Request(url)
        with self.opener.open(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode())
            self.assertEqual(data.get("status"), "OK")
            self.assertIn("decisions", data)
            self.assertNotIn("Live orchestrator is not attached", str(data))

    def test_auto_selection_cached_speed_under_50ms(self):
        url = f"http://127.0.0.1:{self.port}/api/intelligence/auto-selection"
        req = urllib.request.Request(url)
        # Ensure warm
        with self.opener.open(req, timeout=10):
            pass

        # Measure cached latency
        t0 = time.time()
        with self.opener.open(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            dt_ms = (time.time() - t0) * 1000.0

        self.assertEqual(resp.status, 200)
        self.assertEqual(data.get("status"), "OK")
        self.assertTrue(data.get("cached", False))
        self.assertLess(dt_ms, 50.0, f"Expected sub-50ms latency, got {dt_ms:.1f}ms")


if __name__ == "__main__":
    unittest.main()
