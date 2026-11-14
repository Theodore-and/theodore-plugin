"""runtime/build/build_bundle.py: a node_modules tree that lacks a package the lockfile lists for the target platform
(an optional dependency npm skipped on a dropped connection) is refused, so such a bundle is never made."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime" / "build"))
import build_bundle  # noqa: E402

LOCK = {
    "lockfileVersion": 3,
    "packages": {
        "": {"name": "x", "dependencies": {"needed": "1", "sharp": "1"}, "optionalDependencies": {"optional-pkg": "3"}},
        "node_modules/sharp": {"version": "1", "optionalDependencies": {"@img/sharp-darwin-x64": "1", "@img/sharp-linux-x64": "1",
                               "@img/sharp-wasm32": "1", "not-on-windows": "1"}},
        "node_modules/orphan": {"version": "1", "optional": True},
        "node_modules/needed": {"version": "1.0.0", "dependencies": {"nested": "2"}},
        "node_modules/optional-pkg": {"version": "3.2.2", "optional": True},
        "node_modules/needed/node_modules/nested": {"version": "2.0.0"},
        "node_modules/@img/sharp-darwin-x64": {"version": "1", "optional": True, "os": ["darwin"], "cpu": ["x64"]},
        "node_modules/@img/sharp-linux-x64": {"version": "1", "optional": True, "os": ["linux"], "cpu": ["x64"]},
        "node_modules/@img/sharp-wasm32": {"version": "1", "optional": True, "cpu": ["wasm32"]},
        "node_modules/not-on-windows": {"version": "1", "optional": True, "os": ["!win32"]},
        "node_modules/dev-tool": {"version": "1", "dev": True},
    },
}


class LockComplete(unittest.TestCase):
    # "orphan" is in the lockfile but nothing installed needs it (as @img/sharp-wasm32 is): never required.
    def tree(self, root: Path, names: list[str]) -> None:
        (root / "package-lock.json").write_text(json.dumps(LOCK), encoding="utf-8")
        for name in names:
            (root / name).mkdir(parents=True)
            (root / name / "package.json").write_text("{}", encoding="utf-8")

    def test_every_package_for_the_platform_present_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.tree(root, ["node_modules/needed", "node_modules/optional-pkg", "node_modules/needed/node_modules/nested",
                             "node_modules/sharp", "node_modules/@img/sharp-darwin-x64", "node_modules/not-on-windows"])
            self.assertEqual(build_bundle.missing_lock_packages(root, root / "package-lock.json", "darwin-x64"), [])

    def test_a_skipped_optional_dependency_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.tree(root, ["node_modules/needed", "node_modules/needed/node_modules/nested", "node_modules/sharp",
                             "node_modules/@img/sharp-darwin-x64", "node_modules/not-on-windows"])
            self.assertEqual(build_bundle.missing_lock_packages(root, root / "package-lock.json", "darwin-x64"),
                             ["node_modules/optional-pkg"])

    def test_platform_fields_follow_npm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.tree(root, ["node_modules/needed", "node_modules/optional-pkg", "node_modules/needed/node_modules/nested",
                             "node_modules/sharp"])
            self.assertEqual(build_bundle.missing_lock_packages(root, root / "package-lock.json", "win32-x64"), [])
            self.assertEqual(build_bundle.missing_lock_packages(root, root / "package-lock.json", "linux-x64"),
                             ["node_modules/@img/sharp-linux-x64", "node_modules/not-on-windows"])


if __name__ == "__main__":
    unittest.main()
