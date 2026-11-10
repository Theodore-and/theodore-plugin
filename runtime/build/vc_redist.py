"""The Microsoft Visual C++ runtime DLLs, taken from Microsoft's own redistributable installer, for app-local
deployment next to the Windows binaries that import them (skia-canvas's skia.node imports MSVCP140.dll and
VCRUNTIME140.dll; MSVCP140.dll imports VCRUNTIME140_1.dll). A stock Windows without the redistributable installed
would otherwise fail to load skia.node ("The specified module could not be found").

VC_redist.x64.exe is a WiX Burn bundle: its `.wixburn` section lists cabinets attached after the stub. The first is
the bootstrapper's UI (it holds license.rtf), the second holds the packages; the X64 Minimum Runtime's files are
in an inner cabinet. Cabinets here are stored or MSZIP-compressed; both are read with the standard library (MSZIP is
raw deflate per block, each block using the previous 32 KB as its dictionary), so this runs on any build host.
"""

from __future__ import annotations

import struct
import zlib


def burn_containers(data: bytes) -> list[bytes]:
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    sections, optional = struct.unpack_from("<H", data, pe + 6)[0], struct.unpack_from("<H", data, pe + 20)[0]
    at = pe + 24 + optional
    for _ in range(sections):
        if data[at:at + 8].rstrip(b"\0") == b".wixburn":
            raw = struct.unpack_from("<I", data, at + 20)[0]
            if struct.unpack_from("<I", data, raw)[0] != 0x00F14300:
                raise ValueError("not a WiX Burn bundle")
            stub, count = struct.unpack_from("<I", data, raw + 24)[0], struct.unpack_from("<I", data, raw + 44)[0]
            out, offset = [], stub
            for size in struct.unpack_from(f"<{count}I", data, raw + 48):
                offset = data.index(b"MSCF\0\0\0\0", offset)        # each container is a cabinet, in order
                out.append(data[offset:offset + size])
                offset += size
            return out
        at += 40
    raise ValueError("no .wixburn section")


def cab_files(cab: bytes) -> dict[str, bytes]:
    """Every file of a single-volume cabinet whose folders are stored or MSZIP-compressed."""
    if cab[:4] != b"MSCF":
        raise ValueError("not a cabinet")
    files_at = struct.unpack_from("<I", cab, 16)[0]
    folder_count, file_count, flags = struct.unpack_from("<HHH", cab, 26)
    if flags & 3:
        raise ValueError("a cabinet set is not supported")
    at, folder_reserve, data_reserve = 36, 0, 0
    if flags & 4:
        header_reserve, folder_reserve, data_reserve = struct.unpack_from("<HBB", cab, 36)
        at = 40 + header_reserve
    folders = []
    for _ in range(folder_count):
        folders.append(struct.unpack_from("<IHH", cab, at))
        at += 8 + folder_reserve

    def unpack(index: int) -> bytes:
        start, blocks, kind = folders[index]
        if kind & 0xF not in (0, 1):
            raise ValueError(f"cabinet compression {kind & 0xF} is not supported")
        out, at, window = bytearray(), start, b""
        for _ in range(blocks):
            size = struct.unpack_from("<H", cab, at + 4)[0]
            at += 8 + data_reserve
            block = cab[at:at + size]
            at += size
            if kind & 0xF == 1:
                if block[:2] != b"CK":
                    raise ValueError("bad MSZIP block")
                inflater = zlib.decompressobj(-15, zdict=window) if window else zlib.decompressobj(-15)
                block = inflater.decompress(block[2:]) + inflater.flush()
            out += block
            window = bytes(out[-32768:])
        return bytes(out)

    unpacked: dict[int, bytes] = {}
    files, at = {}, files_at
    for _ in range(file_count):
        size, offset, folder = struct.unpack_from("<IIH", cab, at)
        end = cab.index(b"\0", at + 16)
        name = cab[at + 16:end].decode("utf-8", "replace")
        at = end + 1
        if folder not in unpacked:
            unpacked[folder] = unpack(folder)
        files[name] = unpacked[folder][offset:offset + size]
    return files


def runtime_files(installer: bytes, wanted: list[str], licence: str = "license.rtf") -> tuple[dict[str, bytes], bytes]:
    """{dll name: bytes} for the x64 files named in `wanted` (as vcruntime140.dll), and the licence text."""
    ui, packages = burn_containers(installer)[:2]
    ui_files = cab_files(ui)
    manifest = ui_files.get("0", b"").decode("utf-8", "replace")
    marker = f'FilePath="{licence}"'
    if marker not in manifest:
        raise ValueError(f"the bundle manifest names no {licence}")
    source = manifest.split(marker, 1)[1].split('SourcePath="', 1)[1].split('"', 1)[0]
    found: dict[str, bytes] = {}
    for blob in cab_files(packages).values():
        if blob[:4] != b"MSCF":
            continue
        for name, data in cab_files(blob).items():
            if name.endswith("_amd64") and name[:-len("_amd64")] in wanted:
                found[name[:-len("_amd64")]] = data
    missing = sorted(set(wanted) - set(found))
    if missing:
        raise ValueError(f"the redistributable holds no x64 {', '.join(missing)}")
    return found, ui_files[source]
