"""runtime/build/vc_redist.py: the cabinet reader (stored and MSZIP folders) and the WiX Burn container walk, on
small archives made here; the real redistributable is read by build_bundle.py on Windows and checked by sha256."""

from __future__ import annotations

import struct
import sys
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime" / "build"))
import vc_redist  # noqa: E402


def cabinet(files: dict[str, bytes], mszip: bool, block: int = 32768) -> bytes:
    data = b"".join(files.values())
    blocks, window = [], b""
    for i in range(0, max(len(data), 1), block):
        chunk = data[i:i + block]
        if mszip:
            co = zlib.compressobj(9, zlib.DEFLATED, -15, zdict=window) if window else zlib.compressobj(9, zlib.DEFLATED, -15)
            payload = b"CK" + co.compress(chunk) + co.flush()
            window = (window + chunk)[-32768:]
        else:
            payload = chunk
        blocks.append(struct.pack("<IHH", 0, len(payload), len(chunk)) + payload)
    entries, offset = b"", 0
    for name, content in files.items():
        entries += struct.pack("<IIHHHH", len(content), offset, 0, 0, 0, 0) + name.encode() + b"\0"
        offset += len(content)
    files_at = 36 + 8
    data_at = files_at + len(entries)
    folder = struct.pack("<IHH", data_at, len(blocks), 1 if mszip else 0)
    body = folder + entries + b"".join(blocks)
    header = b"MSCF" + struct.pack("<IIIIIBBHHHHH", 0, 36 + len(body), 0, files_at, 0, 3, 1, 1, len(files), 0, 0, 0)
    return header + body


class Cabinets(unittest.TestCase):
    def test_stored_and_mszip_folders_give_the_files_back(self):
        files = {"a.dll_amd64": bytes(range(256)) * 300, "b.txt": b"hello\n" * 20000, "empty": b""}
        for mszip in (False, True):
            self.assertEqual(vc_redist.cab_files(cabinet(files, mszip)), files)

    def test_other_compression_is_refused(self):
        cab = bytearray(cabinet({"x": b"y"}, False))
        struct.pack_into("<H", cab, 36 + 6, 3)           # LZX
        with self.assertRaises(ValueError):
            vc_redist.cab_files(bytes(cab))

    def test_the_runtime_files_and_licence_come_out_of_a_burn_bundle(self):
        ui = cabinet({"0": b'<Payload Id="lic" FilePath="license.rtf" SourcePath="u4" />', "u4": b"{\\rtf1 terms}"}, True)
        inner = cabinet({"msvcp140.dll_amd64": b"MZ p", "vcruntime140.dll_amd64": b"MZ v", "vcruntime140.dll_arm64": b"MZ a"}, True)
        packages = cabinet({"a12": inner, "a0": b"not a cabinet"}, False)
        # A minimal PE: the DOS header points at "PE", one section .wixburn naming two containers after the stub.
        stub = 0x400
        section = b".wixburn" + struct.pack("<IIII", 0, 0, 0, 0x200) + b"\0" * 16
        pe = b"PE\0\0" + struct.pack("<HH", 0x8664, 1) + b"\0" * 12 + struct.pack("<HH", 0, 0) + section
        exe = bytearray(b"MZ" + b"\0" * 0x3A + struct.pack("<I", 0x40) + pe)
        exe += b"\0" * (0x200 - len(exe))
        exe += struct.pack("<IIIIIIIIIIIIII", 0x00F14300, 2, 0, 0, 0, 0, stub, 0, 0, 0, 1, 2, len(ui), len(packages))
        exe += b"\0" * (stub - len(exe)) + ui + packages
        dlls, licence = vc_redist.runtime_files(bytes(exe), ["msvcp140.dll", "vcruntime140.dll"])
        self.assertEqual(dlls, {"msvcp140.dll": b"MZ p", "vcruntime140.dll": b"MZ v"})
        self.assertEqual(licence, b"{\\rtf1 terms}")
        with self.assertRaises(ValueError):
            vc_redist.runtime_files(bytes(exe), ["vcruntime140_1.dll"])


if __name__ == "__main__":
    unittest.main()
