"""Real ctypes regressions; set PARROT_TEST_NATIVE_LIBRARY and PARROT_TEST_ABI_SERVER.

Both artifacts are built by the maintainer test command, not by ordinary users.
All socket traffic goes to the supplied loopback fixture. No account/network config.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from parrot_go_tls_client import Client, NativeError, __version__


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.lib = os.environ["PARROT_TEST_NATIVE_LIBRARY"]
        self.server = subprocess.Popen([os.environ["PARROT_TEST_ABI_SERVER"]], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, text=True)
        self.messages = queue.Queue()
        def reader():
            for line in self.server.stdout:
                self.messages.put(json.loads(line))
        self.reader = threading.Thread(target=reader)
        self.reader.start()
        self.info = self.messages.get(timeout=5)
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.server.stdin.close()
        self.server.wait(timeout=5)
        self.reader.join(2)
        self.server.stdout.close()
        self.assertFalse(self.reader.is_alive())

    def client(self, **kwargs):
        client = Client(lib_path=self.lib, ca_pem=self.info["ca"], **kwargs)
        self.clients.append(client)
        return client

    def saw(self, path):
        message = self.messages.get(timeout=3)
        self.assertEqual(message["path"], path)

    def drain(self, stream):
        events = []
        until = time.monotonic() + 3
        while time.monotonic() < until:
            event = stream.poll(100)
            events.append(event)
            if event["kind"] in ("eof", "error"):
                return events
        self.fail("native event stream stalled")

    def test_parent_close_preserves_unpolled_sent_fact(self):
        client = self.client()
        stream = client.start("POST", self.info["url"] + "/hold", body=b"side effect")
        self.saw("/hold")  # No poll before receipt, exactly the N-01 reproduction.
        client.close()
        self.assertTrue(stream.close()["dispatched"])
        self.assertTrue(stream.poll()["dispatched"])
        self.assertTrue(stream.dispatched)
        self.assertFalse(client._requests)
        # Native handles have been reclaimed, not kept in an unbounded tombstone map.
        missing = client._call("ParrotPoll", stream.handle, 0)
        self.assertTrue(missing["dispatch_unknown"])
        self.assertTrue(missing["dispatched"])

    def test_request_close_then_poll_and_concurrent_close(self):
        client = self.client()
        for _ in range(15):
            stream = client.start("POST", self.info["url"] + "/hold", body=b"x")
            self.saw("/hold")
            results = []
            threads = [threading.Thread(target=lambda: results.append(stream.close())) for _ in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())
            self.assertTrue(all(item["dispatched"] for item in results))
            self.assertEqual(stream.poll(), results[0])
            results[0]["dispatched"] = False  # Cannot mutate the cached terminal.
            self.assertTrue(stream.poll()["dispatched"])
            self.assertFalse(client._requests)

    def test_poll_racing_parent_close_never_reports_unsent_after_received(self):
        client = self.client()
        stream = client.start("POST", self.info["url"] + "/hold", body=b"x")
        self.saw("/hold")
        events = []
        thread = threading.Thread(target=lambda: events.extend(stream.poll(1000) for _ in range(20)))
        thread.start()
        client.close()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(events and all(item["dispatched"] for item in events))

    def test_confirmed_unsent_stays_unsent_on_parent_close(self):
        client = self.client()
        stream = client.start("GET", "http://never-connect.invalid/")
        client.close()
        self.assertFalse(stream.close()["dispatched"])
        self.assertFalse(stream.poll()["dispatched"])
        self.assertFalse(client._requests)

    def test_complete_empty_response_delayed_poll_not_read_timeout(self):
        client = self.client()
        stream = client.start("GET", self.info["url"] + "/empty", read_timeout=.05)
        self.saw("/empty")
        time.sleep(.15)
        events = self.drain(stream)
        self.assertEqual(events[-1]["kind"], "eof", events)
        self.assertTrue(any(item.get("status") == 204 for item in events))
        stream.close()

    def test_convenience_override_inherits_proxy_auth_ca_and_library(self):
        client = self.client(proxy=self.info["proxy"], proxy_auth=("fixture", "secret"))
        for insecure in (False, True):
            # Inherited concrete library path must survive a changed environment.
            with patch.dict(os.environ, {"PARROT_GO_TLS_LIBRARY": "/not/a/native/library.so"}):
                response = client.get(self.info["url"] + "/echo", insecure=insecure,
                                      headers=[(b"X-Raw", b"caf\xe9")])
            self.assertEqual(response.status, 200)
            proxied = self.messages.get(timeout=3)
            self.assertTrue(proxied["proxy"] and proxied["authenticated"])
            self.saw("/echo")
            self.assertIn((b"X-Raw", b"caf\xe9"), response.headers_raw)
        self.assertFalse(client._requests)

    def test_header_bytes_roundtrip_and_legacy_strings(self):
        values = [b"caf\xe9", b"\xff\x80", b"valid\xc3\xa9"]
        client = self.client()
        with client.start("GET", self.info["url"] + "/echo",
                          headers=[(b"X-Raw", v) for v in values] + [("X-Raw", "café")]) as stream:
            events = self.drain(stream)
            self.assertEqual(events[-1]["kind"], "eof", events)
            header = next(item for item in events if item["kind"] == "headers")
            raw = header["headers_raw"]
            self.assertEqual([v for k, v in raw if k.lower() == b"x-raw"], values + ["café".encode()])
            self.assertEqual([v for k, v in raw if k.lower() == b"set-cookie"], [b"a=caf\xe9", b"b=\xff\x80"])
            self.assertEqual(raw, [tuple(base64.b64decode(v) for v in pair) for pair in header["headers_b64"]])
        self.saw("/echo")

    def test_legacy_c_json_input_and_build_identity(self):
        client = self.client()
        self.assertEqual(__version__, "0.2.1")
        self.assertEqual(client.native_info["version"], __version__)
        self.assertEqual(client.native_info["abi"], 3)
        self.assertEqual(client.native_info["profile"], "antigravity-ls-2.22.0")
        self.assertTrue(client.native_info["source_id"])
        payload = json.dumps({"method": "GET", "url": self.info["url"] + "/echo",
                              "headers": [["X-Raw", "legacy"]]}).encode()
        handle = client._call("ParrotStart", client._handle, payload)["id"]
        try:
            while True:
                event = client._call("ParrotPoll", handle, 1000)
                if event["kind"] == "headers":
                    self.assertIn((b"X-Raw", b"legacy"), event["headers_raw"])
                    break
                self.assertNotEqual(event["kind"], "error", event)
        finally:
            client._call("ParrotCloseRequest", handle)
        self.saw("/echo")

    def test_exception_paths_body_cap_and_repeated_handle_cleanup(self):
        client = self.client()
        with self.assertRaisesRegex(ValueError, "max_body_bytes"):
            client.get(self.info["url"] + "/large", max_body_bytes=1 << 20)
        self.saw("/large")
        self.assertFalse(client._requests)
        for _ in range(500):
            with client.start("GET", "http://never-connect.invalid/") as stream:
                end = self.drain(stream)[-1]
                self.assertEqual(end["code"], "invalid_request")
                self.assertFalse(end["dispatched"])
        self.assertFalse(client._requests)
        malformed = client._call("ParrotStart", client._handle, b'{"headers_b64":[["%%%","%%%"]]}')
        self.assertEqual(malformed["code"], "invalid_request")
        self.assertFalse(malformed["dispatched"])
        client._lib.ParrotFree(None)


if __name__ == "__main__":
    unittest.main()
