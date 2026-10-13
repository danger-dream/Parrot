"""ABI 3 raw-body regressions. Uses the same explicitly built loopback fixture
and PARROT_TEST_NATIVE_LIBRARY/PARROT_TEST_ABI_SERVER as test_native.py.
"""
from __future__ import annotations

import asyncio
import base64
import ctypes
import hashlib
import json
import statistics
import sys
import threading
import time
import unittest
from unittest.mock import patch

import test_native as fixtures
import parrot_go_tls_client.client as wrapper


class RawBodyTests(unittest.TestCase):
    setUp = fixtures.NativeTests.setUp
    tearDown = fixtures.NativeTests.tearDown
    client = fixtures.NativeTests.client
    saw = fixtures.NativeTests.saw
    drain = fixtures.NativeTests.drain

    @staticmethod
    def body():
        return bytes(range(256)) * 65536  # 16 MiB, including embedded NULs/invalid UTF-8.

    def response_json(self, stream):
        events = self.drain(stream)
        self.assertEqual(events[-1]["kind"], "eof", events)
        return json.loads(b"".join(base64.b64decode(e["data"]) for e in events if e["kind"] == "data"))

    def test_16mib_true_ctypes_binary_fidelity_without_python_body_encoding(self):
        body = self.body()
        expected_pointer = ctypes.cast(ctypes.c_char_p(body), ctypes.c_void_p).value
        client = self.client()
        original_call, original_encode = client._call, base64.b64encode
        metadata_sizes = []
        def checked_encode(value, *args, **kwargs):
            self.assertLess(len(value), 4096, "whole request body encoded under the GIL")
            return original_encode(value, *args, **kwargs)
        def checked_call(name, *args):
            if name == "ParrotStartRaw":
                _handle, metadata, pointer, size = args
                self.assertNotIn("body_b64", json.loads(metadata))
                self.assertLess(len(metadata), 4096)
                self.assertEqual(pointer, expected_pointer, "wrapper copied the Python body")
                self.assertEqual(size, len(body))
                metadata_sizes.append(len(metadata))
            self.assertNotEqual(name, "ParrotStart", "wrapper fell back to the old encoding path")
            return original_call(name, *args)
        with patch.object(client, "_call", checked_call), patch.object(wrapper.base64, "b64encode", checked_encode):
            response = client.post(self.info["url"] + "/body-digest", body=body,
                                   headers=[(b"x-fixture", b"binary")], timeout=5)
        self.saw("/body-digest")
        self.assertEqual(response.json(), {"length": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        self.assertEqual(len(metadata_sizes), 1)
        print(f"16MiB raw upload verified; metadata={metadata_sizes[0]} bytes; original Python pointer retained", flush=True)

    def test_native_owns_copy_and_python_buffer_export_is_released(self):
        body = bytearray(self.body())
        expected = {"length": len(body), "sha256": hashlib.sha256(body).hexdigest()}
        client = self.client()
        with client.start("POST", self.info["url"] + "/body-gated", body=body, timeout=5) as stream:
            self.saw("/body-gated")  # Server has not begun consuming the request body.
            body[:] = b"z" * len(body)
            body.extend(b"resizing proves no outstanding buffer export")
            self.assertEqual(client.get(self.info["url"] + "/release-body").status, 204)
            self.saw("/release-body")
            self.assertEqual(self.response_json(stream), expected)
        # Read-only and writable contiguous memoryview offsets keep their bytes.
        for value in (b"", None, memoryview(b"prefix\0\xffsuffix")[6:8], memoryview(bytearray(b"x\0y"))):
            expected_body = b"" if value is None else bytes(value)
            response = client.post(self.info["url"] + "/body-digest", body=value)
            self.saw("/body-digest")
            self.assertEqual(response.json(), {"length": len(expected_body), "sha256": hashlib.sha256(expected_body).hexdigest()})
        bad_buffer = memoryview(b"abcdef")[::2]
        with self.assertRaises(BufferError):
            client.start("POST", self.info["url"], body=bad_buffer)

    def test_raw_validation_and_legacy_body_entry_point(self):
        client = self.client()
        metadata = json.dumps({"method": "POST", "url": self.info["url"] + "/body-digest"}).encode()
        for pointer, size in ((None, 1), (None, ctypes.c_size_t(-1).value)):
            result = client._call("ParrotStartRaw", client._handle, metadata, pointer, size)
            self.assertEqual(result["code"], "invalid_request")
            self.assertFalse(result["dispatched"])
        conflict = client._call("ParrotStartRaw", client._handle, b'{"body_b64":""}', None, 0)
        self.assertEqual(conflict["code"], "invalid_request")
        body = b"old\0entry\xffstill works"
        old_metadata = json.dumps({"method": "POST", "url": self.info["url"] + "/body-digest",
                                   "body_b64": base64.b64encode(body).decode()}).encode()
        handle = client._call("ParrotStart", client._handle, old_metadata)["id"]
        try:
            chunks = []
            while True:
                event = client._call("ParrotPoll", handle, 1000)
                self.assertNotEqual(event["kind"], "error", event)
                if event["kind"] == "data":
                    chunks.append(base64.b64decode(event["data"]))
                if event["kind"] == "eof":
                    break
            self.assertEqual(json.loads(b"".join(chunks)), {"length": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        finally:
            client._call("ParrotCloseRequest", handle)
        self.saw("/body-digest")

    def test_close_racing_raw_start_collects_the_created_request(self):
        client, body = self.client(), bytearray(self.body())
        native = client._lib.ParrotStartRaw
        entered, release = threading.Event(), threading.Event()
        streams, failures = [], []
        def held_native(*args):
            entered.set()
            if not release.wait(3):
                raise AssertionError("test raw start was never released")
            return native(*args)
        def start():
            try:
                streams.append(client.start("POST", self.info["url"] + "/hold", body=body))
            except BaseException as exc:
                failures.append(exc)
        with patch.object(client._lib, "ParrotStartRaw", held_native):
            worker = threading.Thread(target=start)
            worker.start()
            self.assertTrue(entered.wait(2))
            closer = threading.Thread(target=client.close)
            closer.start()
            release.set()
            worker.join(3)
            closer.join(3)
            self.assertFalse(worker.is_alive() or closer.is_alive())
        self.assertFalse(failures)
        self.assertEqual(len(streams), 1)
        self.assertFalse(client._requests)
        self.assertEqual(streams[0].poll(), streams[0].close())
        body.extend(b"buffer export was released even when parent closed")
        with self.assertRaisesRegex(RuntimeError, "closed"):
            client.start("POST", self.info["url"], body=body)
        body.extend(b"also released on setup failure")

    def test_raw_ffi_releases_gil_not_just_periodic_python_switching(self):
        body, client = self.body(), self.client()
        native = client._lib.ParrotStartRaw
        original_interval = sys.getswitchinterval()
        def observe(call):
            ready, gate, done = threading.Event(), threading.Event(), threading.Event()
            progressed = []
            def other_python_thread():
                ready.set()
                gate.wait()
                progressed.append(not done.is_set())
            thread = threading.Thread(target=other_python_thread)
            thread.start()
            self.assertTrue(ready.wait(2))
            sys.setswitchinterval(1.0)  # Longer than the call: periodic switching cannot prove release.
            try:
                gate.set()
                result = call()
                done.set()
            finally:
                sys.setswitchinterval(original_interval)
            thread.join(2)
            self.assertFalse(thread.is_alive())
            return progressed[0], result
        def gil_holding_control():
            until = time.perf_counter() + .02
            while time.perf_counter() < until:
                pass
        progressed, _ = observe(gil_holding_control)
        self.assertFalse(progressed, "GIL control is invalid on this interpreter")
        observed = []
        def checked_native(*args):
            progressed, result = observe(lambda: native(*args))
            observed.append(progressed)
            return result
        with patch.object(client._lib, "ParrotStartRaw", checked_native):
            for _ in range(5):
                with client.start("POST", "http://never-connect.invalid/", body=body) as stream:
                    self.assertEqual(self.drain(stream)[-1]["code"], "invalid_request")
        self.assertTrue(any(observed), "another Python thread never ran inside the raw native call")
        print(f"GIL test: Python control held GIL; real raw calls allowed concurrent Python progress {sum(observed)}/5", flush=True)

    def test_worker_heartbeat_relative_to_original_python_encoding(self):
        body, client = self.body(), self.client()
        def raw_start():
            with client.start("POST", "http://never-connect.invalid/", body=body):
                pass
        def old_python_encoding():
            # Exactly the eliminated GIL-heavy body operation, without network.
            return len(json.dumps({"body_b64": base64.b64encode(body).decode()}).encode())
        async def measure(work):
            running, ticks = True, []
            async def heartbeat():
                while running:
                    ticks.append(time.perf_counter())
                    await asyncio.sleep(.001)
            await asyncio.to_thread(lambda: None)  # Exclude executor initialization.
            beat = asyncio.create_task(heartbeat())
            try:
                await asyncio.sleep(.01)
                for _ in range(6):
                    await asyncio.to_thread(work)
            finally:
                running = False
                await beat
            self.assertGreater(len(ticks), 6)
            return max(b-a for a, b in zip(ticks, ticks[1:]))
        async def rounds():
            raw, legacy = [], []
            for _ in range(3):
                legacy.append(await measure(old_python_encoding))
                raw.append(await measure(raw_start))
            return raw, legacy
        raw, legacy = asyncio.run(rounds())
        print(f"16MiB heartbeat max gaps (ms), raw={[round(x*1000, 2) for x in raw]}, "
              f"old Python encoding={[round(x*1000, 2) for x in legacy]}", flush=True)
        # Same-process relative measurement, robust across machine speed and an
        # occasional scheduler outlier; structural + GIL tests above are mandatory.
        self.assertLess(statistics.median(raw), statistics.median(legacy) / 2)


if __name__ == "__main__":
    unittest.main()
