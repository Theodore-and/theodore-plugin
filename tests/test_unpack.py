"""Unpacking with several writers (client/theodore_client/install.py: extract_tar, extract_zip) gives the tree that
tarfile's own "data" extraction gives, entry for entry: names, bytes, modes, times and links; an entry that must not
be unpacked is still refused; whatever the several-writers path does not decide, or fails at, goes to tarfile's own
extraction."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))
from theodore_client import core, install

WINDOWS = os.name == "nt"


def tree(root: Path) -> dict:
    """Every entry under root: kind, bytes' hash, mode (POSIX), time of a regular file, target of a link."""
    found = {}
    for folder, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(folder) / name
            rel = path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                found[rel] = ("link", os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                found[rel] = ("dir",)
            else:
                found[rel] = ("file", hashlib.sha256(path.read_bytes()).hexdigest(),
                              None if WINDOWS else stat.S_IMODE(info.st_mode), int(info.st_mtime), info.st_nlink > 1)
    return found


def add(tar: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644, mtime: int = 1_700_000_000) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), mode, mtime
    tar.addfile(info, io.BytesIO(data))


class UnpackCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theodore-unpack-"))
        self.addCleanup(core.remove_tree, self.tmp)
        home = mock.patch.dict(os.environ, {"THEODORE_HOME": str(self.tmp / "home")})
        home.start()
        self.addCleanup(home.stop)
        cached = mock.patch.object(core, "_HOME", None)
        cached.start()
        self.addCleanup(cached.stop)

    def archive(self, links: bool = True) -> Path:
        path = self.tmp / "sample.tar"
        with tarfile.open(path, "w") as tar:
            folder = tarfile.TarInfo("pkg")
            folder.type, folder.mode, folder.mtime = tarfile.DIRTYPE, 0o755, 1_700_000_000
            tar.addfile(folder)
            for n in range(300):
                add(tar, f"pkg/lib/m{n % 7}/file{n}.js", f"module {n}\n".encode() * (n % 5 + 1), mtime=1_700_000_000 + n)
            add(tar, "pkg/bin/tool", b"#!/bin/sh\necho ok\n", mode=0o755)
            add(tar, "pkg/setuid", b"x", mode=0o4755)                      # the data rules drop the set-id bit
            add(tar, "pkg/world-writable", b"y", mode=0o666)               # and the write bits of group and others
            add(tar, "pkg/no dir entry/deep/er/leaf.txt", b"leaf")        # parents that have no entry of their own
            add(tar, "pkg/ünï/cödé.txt", "é".encode())
            add(tar, "pkg/big.bin", os.urandom(1024) * (9 * 1024), mode=0o644)    # above the inline size
            add(tar, "pkg/twice.txt", b"first")
            add(tar, "pkg/twice.txt", b"second and last")
            add(tar, "pkg/empty", b"")
            if links:
                soft = tarfile.TarInfo("pkg/bin/tool-link")
                soft.type, soft.linkname, soft.mtime = tarfile.SYMTYPE, "tool", 1_700_000_000
                tar.addfile(soft)
                hard = tarfile.TarInfo("pkg/lib/hard.js")
                hard.type, hard.linkname, hard.mtime = tarfile.LNKTYPE, "pkg/lib/m1/file1.js", 1_700_000_000
                tar.addfile(hard)
        return path

    def reference(self, archive: Path) -> Path:
        dest = self.tmp / "reference"
        with tarfile.open(archive) as tar:
            tar.extractall(dest, filter="data")
        return dest

    def as_windows(self):
        """The several-writers path is taken on Windows only; here it is taken on every system (modes are then not
        set, as on Windows, so they are not compared)."""
        patch = mock.patch.object(install, "IS_WINDOWS", True)
        patch.start()
        self.addCleanup(patch.stop)

    def same(self, want: dict, got: dict, modes: bool = True) -> None:
        self.assertEqual(sorted(want), sorted(got))
        for name in want:
            a, b = want[name], got[name]
            if not modes and a[0] == "file":
                a, b = a[:2] + a[3:], b[:2] + b[3:]
            self.assertEqual(a, b, name)

    def test_the_names_the_several_writers_path_leaves_to_tarfile(self):
        for name in ("../x", "a/../x", "a/..", "/abs", "C:/x", "a//b", "a\\..\\b", "./a", "a/./b", "a/file.txt:stream",
                     "a\\b", "\\\\server\\share", ""):
            with self.subTest(name=name), self.assertRaises(install._NotPlain):
                install._plain_target("/root", name)
        for name in ("a/b/c.txt", "a/.hidden/b..c", "a/b...", "..a/b", "pkg/"):
            with self.subTest(name=name):
                install._plain_target("/root", name)

    def test_several_writers_give_the_tree_tarfile_itself_gives(self):
        archive = self.archive(links=False)
        dest = self.tmp / "plain"
        dest.mkdir()
        self.assertGreater(install.UNPACK_WRITERS, 1)
        install._unpack_tar_plain(archive, dest)
        self.same(tree(self.reference(archive)), tree(dest))
        self.assertEqual(b"second and last", (dest / "pkg" / "twice.txt").read_bytes())
        if not WINDOWS:
            self.assertEqual(0o755, stat.S_IMODE((dest / "pkg" / "bin" / "tool").stat().st_mode))
            self.assertEqual(0o755, stat.S_IMODE((dest / "pkg" / "setuid").stat().st_mode))
            self.assertEqual(0o644, stat.S_IMODE((dest / "pkg" / "world-writable").stat().st_mode))

    def test_extract_tar_gives_that_tree_on_this_system_and_as_windows(self):
        archive = self.archive(links=not WINDOWS)
        install.extract_tar(archive, self.tmp / "here")
        self.same(tree(self.reference(archive)), tree(self.tmp / "here"))
        self.as_windows()
        with mock.patch.object(install, "_unpack_tar_plain", wraps=install._unpack_tar_plain) as plain:
            install.extract_tar(archive, self.tmp / "as-windows")       # links: tarfile's own extraction takes over
            self.assertEqual(1, plain.call_count)
            self.same(tree(self.reference(archive)), tree(self.tmp / "as-windows"))
            flat = self.archive(links=False)
            install.extract_tar(flat, self.tmp / "as-windows-flat")
        core.remove_tree(self.tmp / "reference")
        self.same(tree(self.reference(flat)), tree(self.tmp / "as-windows-flat"), modes=False)

    def test_an_entry_that_leaves_the_folder_is_refused_and_nothing_lands_outside(self):
        for windows in (False, True):
            if windows:
                self.as_windows()
            for name, kind, link in (("../outside.txt", None, None), ("pkg/../../outside.txt", None, None),
                                     ("pkg/escape", tarfile.SYMTYPE, "../../outside-target"), ("pkg/device", tarfile.CHRTYPE, None)):
                with self.subTest(name=name, windows=windows):
                    path = self.tmp / "bad.tar"
                    with tarfile.open(path, "w") as tar:
                        add(tar, "pkg/ok.txt", b"ok")
                        if kind is None:
                            add(tar, name, b"must not be written")
                        else:
                            entry = tarfile.TarInfo(name)
                            entry.type = kind
                            entry.linkname = link or ""
                            tar.addfile(entry)
                    dest = self.tmp / "bad" / "dest"
                    with self.assertRaises(core.Status) as caught:
                        install.extract_tar(path, dest)
                    self.assertIn("could not unpack bad.tar", str(caught.exception.summary))
                    self.assertFalse((self.tmp / "bad" / "outside.txt").exists())
                    self.assertFalse((self.tmp / "outside.txt").exists())
                    core.remove_tree(self.tmp / "bad")

    @unittest.skipIf(WINDOWS, "making a symbolic link needs a privilege on Windows")
    def test_a_link_on_the_way_out_of_the_folder_is_never_written_through(self):
        self.as_windows()
        archive = self.archive(links=False)
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        dest = self.tmp / "linked"
        dest.mkdir()
        os.symlink(outside, dest / "pkg")
        with self.assertRaises(core.Status):
            install.extract_tar(archive, dest)
        self.assertEqual([], list(outside.iterdir()), "nothing went through the link")

    def test_a_writer_that_fails_is_covered_by_tarfiles_own_extraction(self):
        self.as_windows()
        archive = self.archive(links=False)
        for label, effect in (("once", None), ("always", OSError(13, "the file is in use")), ("odd", ValueError("Paths don't have the same drive"))):
            with self.subTest(label):
                real, calls = install._write_file, {"n": 0}

                def flaky(target, data, mode, mtime):
                    calls["n"] += 1
                    if effect is not None:
                        raise effect
                    if calls["n"] == 40:
                        raise PermissionError(13, "the file is in use", target)
                    return real(target, data, mode, mtime)

                dest = self.tmp / f"retry-{label}"
                with mock.patch.object(install, "_write_file", flaky):
                    install.extract_tar(archive, dest)
                self.assertGreater(calls["n"], 0)
                core.remove_tree(self.tmp / "reference")
                self.same(tree(self.reference(archive)), tree(dest), modes=WINDOWS)

    def test_zip_gives_the_same_files_with_several_writers_and_refuses_the_same_entries(self):
        path = self.tmp / "sample.zip"
        files = {f"engine/mod{n % 9}/part{n}.py": f"x = {n}\n".encode() * (n % 4 + 1) for n in range(250)}
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("engine/", b"")
            bundle.writestr("engine/empty-folder/", b"")
            for name, data in files.items():
                bundle.writestr(name, data)
            tool = zipfile.ZipInfo("engine/bin/run")
            tool.external_attr = 0o755 << 16
            bundle.writestr(tool, b"#!/bin/sh\n")
            bundle.writestr("engine/big.bin", b"\0" * (9 << 20))
        here = self.tmp / "zip-here"
        install.extract_zip(path, here)
        if not WINDOWS:
            self.assertEqual(0o755, stat.S_IMODE((here / "engine" / "bin" / "run").stat().st_mode))
        plain = self.tmp / "zip-plain"
        plain.mkdir()
        install._unpack_zip_plain(path, plain)
        for dest in (here, plain):
            for name, data in files.items():
                self.assertEqual(data, (dest / name).read_bytes(), name)
            self.assertEqual(9 << 20, (dest / "engine" / "big.bin").stat().st_size)
            self.assertTrue((dest / "engine" / "empty-folder").is_dir())
        self.assertEqual(sorted(tree(here)), sorted(tree(plain)))
        bad = self.tmp / "bad.zip"
        with zipfile.ZipFile(bad, "w") as bundle:
            bundle.writestr("ok.txt", b"ok")
            bundle.writestr("../outside.txt", b"no")
        for windows in (False, True):
            if windows:
                self.as_windows()
            with self.subTest(windows=windows), self.assertRaises(core.Status):
                install.extract_zip(bad, self.tmp / "zipbad" / "dest")
            self.assertFalse((self.tmp / "zipbad" / "outside.txt").exists())
            core.remove_tree(self.tmp / "zipbad")


if __name__ == "__main__":
    unittest.main()
