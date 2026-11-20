"""Windows: the engine's cache is %LOCALAPPDATA%\\Theodore\\Cache, and since Theodore's home moved to the user's profile
(0.4.15) an uninstall left that Theodore folder behind, empty. It goes with the cache when nothing else is in it."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))
from theodore_client import commands, core


class EmptyCacheParent(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theodore-cache-parent-"))
        self.addCleanup(core.remove_tree, self.tmp)
        self.local = self.tmp / "AppData" / "Local"
        self.cache = self.local / "Theodore" / "Cache"
        self.cache.mkdir(parents=True)
        (self.cache / "renders.bin").write_bytes(b"x")
        self.home = self.tmp / "profile" / ".theodore"
        self.home.mkdir(parents=True)
        for patch in (mock.patch.dict(os.environ, {"THEODORE_HOME": str(self.home), "LOCALAPPDATA": str(self.local)}),
                      mock.patch.object(core, "_HOME", None)):
            patch.start()
            self.addCleanup(patch.stop)

    def windows(self, value: bool = True):
        patch = mock.patch.object(core, "IS_WINDOWS", value)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_empty_folder_goes_with_the_cache(self):
        self.windows()
        core.remove_tree(self.cache)
        commands._drop_empty_cache_parent(self.cache)
        self.assertFalse((self.local / "Theodore").exists())
        self.assertTrue(self.local.is_dir(), "nothing above it is touched")

    def test_a_folder_that_holds_anything_else_stays(self):
        self.windows()
        (self.local / "Theodore" / "notes.txt").write_text("the user's own")
        core.remove_tree(self.cache)
        commands._drop_empty_cache_parent(self.cache)
        self.assertTrue((self.local / "Theodore" / "notes.txt").exists())

    def test_the_home_itself_is_never_removed_this_way(self):
        self.windows()
        old_home = self.local / "Theodore"
        with mock.patch.dict(os.environ, {"THEODORE_HOME": str(old_home)}), mock.patch.object(core, "_HOME", None):
            core.remove_tree(self.cache)
            commands._drop_empty_cache_parent(self.cache)
        self.assertTrue(old_home.is_dir(), "an install that still lives there keeps its folder")

    def test_other_systems_and_other_folders_are_left_alone(self):
        self.windows(False)
        core.remove_tree(self.cache)
        commands._drop_empty_cache_parent(self.cache)
        self.assertTrue((self.local / "Theodore").is_dir())
        self.windows(True)
        other = self.local / "pptx-runtime"
        other.mkdir()
        commands._drop_empty_cache_parent(other)
        self.assertTrue(other.parent.is_dir() and (self.local / "Theodore").is_dir())


if __name__ == "__main__":
    unittest.main()
