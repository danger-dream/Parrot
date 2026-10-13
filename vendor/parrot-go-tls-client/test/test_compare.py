import importlib.util
from pathlib import Path
import unittest
import subprocess
import sys
import tempfile

spec = importlib.util.spec_from_file_location("compare", Path(__file__).with_name("final_compare.py"))
compare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compare)


class CompareTest(unittest.TestCase):
    def test_ec_point_formats_and_known_ls_ja3(self):
        formats = compare.formats_of({11: b"\x01\x00"})
        self.assertEqual(formats, [0])
        ciphers = [0xc02b, 0xc02f, 0xc02c, 0xc030, 0xcca9, 0xcca8,
                   0xc009, 0xc013, 0xc00a, 0xc014, 0x1301, 0x1302, 0x1303]
        order = [0, 11, 65281, 23, 18, 5, 10, 13, 50, 16, 43, 51]
        curves = [4588, 4587, 4589, 29, 23, 24, 25]
        self.assertEqual(compare.ja3(ciphers, order, curves, formats)[1],
                         "03117a8ed39ef02427ebbc39f121275c")

    def test_cli_mismatch_fails(self):
        # Minimal offline fixture exercises the executable exit status, without
        # any network or access to original LS evidence files.
        extensions = {10: b"\x00\x02\x00\x1d", 11: b"\x01\x00",
                      13: b"\x00\x02\x04\x03", 50: b"\x00\x02\x04\x03"}
        ext = b"".join(k.to_bytes(2, "big") + len(v).to_bytes(2, "big") + v
                       for k, v in extensions.items())
        body = b"\x03\x03" + bytes(32) + b"\x00\x00\x02\x13\x01\x01\x00" + len(ext).to_bytes(2, "big") + ext
        handshake = b"\x01" + len(body).to_bytes(3, "big") + body
        hello = b"\x16\x03\x01" + len(handshake).to_bytes(2, "big") + handshake
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "hello").write_bytes(hello)
            (root / "ls").write_bytes(b"\x00\x00\x04\x08\x00" + bytes(4) + b"\x00\x00\x00\x01")
            (root / "ours").write_bytes((root / "ls").read_bytes())
            cmd = [sys.executable, "-B", str(Path(__file__).with_name("final_compare.py")),
                   "--ls-ch", str(root / "hello"), "--pg-ch", str(root / "hello"),
                   "--ls-h2", str(root / "ls"), "--pg-h2", str(root / "ours")]
            self.assertEqual(subprocess.run(cmd, capture_output=True).returncode, 0)
            (root / "ours").write_bytes((root / "ls").read_bytes()[:-1] + b"\x02")
            self.assertEqual(subprocess.run(cmd, capture_output=True).returncode, 1)


if __name__ == "__main__":
    unittest.main()
