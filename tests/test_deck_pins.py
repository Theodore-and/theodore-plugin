"""A deck's engine pin across updates: a slide's part runs on its deck's engine, an engine open decks still need is
never collected, and `repin` moves a whole deck to the current engine in one step."""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import ClientCase, engine_zip, status_lines  # noqa: E402
from test_hardening import client_home  # noqa: E402

from theodore_client import core  # noqa: E402

DAY = 86400
PIN = Path(".pptx-state") / "theodore-engine.json"


def make_deck(folder: Path, part_ids=()) -> Path:
    """A deck workspace laid out as the engine's prepare leaves it: task.json, .pptx-state/parts.json, parts/<id>/."""
    (folder / ".pptx-state").mkdir(parents=True, exist_ok=True)
    (folder / "task.json").write_text("{}")
    (folder / ".pptx-state" / "parts.json").write_text(json.dumps({"parts": list(part_ids)}))
    for pid in part_ids:
        make_part(folder, pid)
    return folder


def make_part(deck: Path, pid: str) -> Path:
    part = deck / "parts" / pid
    (part / ".pptx-state").mkdir(parents=True, exist_ok=True)
    (part / "part.json").write_text(json.dumps({"schema": "pptx-slide-part-v1", "id": pid}))
    return part


def pin_of(workspace: Path) -> dict:
    path = workspace / PIN
    return json.loads(path.read_text()) if path.exists() else {}


class PartRecognition(unittest.TestCase):
    def test_a_part_is_known_by_what_is_on_disk(self):
        from theodore_client import install
        with client_home() as tmp:
            deck = make_deck(tmp / "deck", ["s01"])
            self.assertEqual(install.deck_of(deck / "parts" / "s01"), deck.resolve())
            self.assertIsNone(install.deck_of(deck))
            stray = tmp / "loose" / "parts" / "s02"                     # no deck above it
            (stray / ".pptx-state").mkdir(parents=True)
            (stray / "part.json").write_text(json.dumps({"schema": "pptx-slide-part-v1", "id": "s02"}))
            self.assertIsNone(install.deck_of(stray))
            wrong = make_part(deck, "s03")
            (wrong / "part.json").write_text(json.dumps({"schema": "pptx-slide-part-v1", "id": "s09"}))
            self.assertIsNone(install.deck_of(wrong), "the id must be the folder's name")
            self.assertEqual(install.deck_parts(deck), [(deck / "parts" / "s01").resolve()])


class EngineCollection(unittest.TestCase):
    """collect_garbage never removes an engine a recently used pinned workspace needs; the cap is for the others."""

    def engine(self, version: str, used_days_ago: float) -> None:
        folder = core.home().engines / version
        folder.mkdir(parents=True, exist_ok=True)
        (folder / ".theodore-engine.json").write_text(json.dumps({"version": version, "sha256": version, "installed_at": 0}))
        marker = folder / ".last-used"
        marker.write_text("1")
        stamp = time.time() - used_days_ago * DAY
        os.utime(marker, (stamp, stamp))

    def setup_engines(self, used: dict, pinned: dict, current: str = "9.0.0", previous: str = "8.0.0"):
        for version, days in used.items():
            self.engine(version, days)
        core.write_json(core.home().engines / "current.json", {"version": current, "previous": previous})
        now = time.time()
        core.write_json(core.home().state / "pins.json",
                        {"workspaces": {f"w{i}": {"version": v, "at": int(now - days * DAY)} for i, (v, days) in enumerate(pinned)}})

    def kept(self) -> set:
        return {c.name for c in core.home().engines.iterdir() if c.is_dir()}

    def test_more_than_four_engines_needed_by_recent_decks_are_all_kept(self):
        from theodore_client import install
        with client_home():
            needed = ["1.0.0", "2.0.0", "3.0.0", "4.0.0", "5.0.0", "6.0.0"]
            self.setup_engines({**{v: 0 for v in needed}, "9.0.0": 0, "8.0.0": 0},
                               [(v, 1) for v in needed] + [("3.0.0", 0)] * 25)
            install.collect_garbage()
            self.assertEqual(self.kept(), set(needed) | {"9.0.0", "8.0.0"})

    def test_engines_no_deck_needs_are_still_collected_down_to_the_cap(self):
        from theodore_client import install
        with client_home():
            self.setup_engines({"9.0.0": 0, "8.0.0": 0, "1.0.0": 0, "2.0.0": 1, "3.0.0": 2, "4.0.0": 3, "5.0.0": 20},
                               [("1.0.0", 2)])
            install.collect_garbage()
            # current, previous and the needed 1.0.0 take three places; the most recently used of the rest takes the fourth
            self.assertEqual(self.kept(), {"9.0.0", "8.0.0", "1.0.0", "2.0.0"})

    def test_a_pin_older_than_the_window_does_not_keep_an_engine(self):
        from theodore_client import install
        with client_home():
            self.setup_engines({"9.0.0": 0, "8.0.0": 0, "1.0.0": 40}, [("1.0.0", 31)])
            install.collect_garbage()
            self.assertEqual(self.kept(), {"9.0.0", "8.0.0"})


class DeckPins(ClientCase):
    NEW = "2.0.0"

    def update_to_new(self):
        self.api.publish_engine(self.NEW, engine_zip(self.NEW))
        self.assertStatus(self.client("update", "--apply"), "OK", 0)

    def used(self, workspace: Path) -> str:
        return (workspace / ".pptx-state" / "engine-used.txt").read_text()

    # --- defect 1: one deck, one engine ---

    def test_a_part_built_after_an_update_runs_on_its_decks_engine(self):
        self.ready()
        deck = make_deck(self.box.tmp / "my deck", ["s01", "s02"])
        self.assertEqual(self.client("pptx", "apply", str(deck)).returncode, 0)
        self.assertEqual(self.client("pptx", "apply", str(deck / "parts" / "s01")).returncode, 0)
        self.update_to_new()
        self.assertEqual(self.client("pptx", "apply", str(deck / "parts" / "s02")).returncode, 0)
        self.assertEqual(self.used(deck / "parts" / "s02"), self.ENGINE_VERSION)
        self.assertEqual(pin_of(deck / "parts" / "s02"), pin_of(deck), "the part's pin is a copy of the deck's")
        versions = {r["version"] for r in self.state("state/pins.json")["workspaces"].values()}
        self.assertEqual(versions, {self.ENGINE_VERSION})

    def test_a_part_pinned_to_another_engine_takes_its_decks(self):
        self.ready()
        deck = make_deck(self.box.tmp / "deck", ["s01"])
        self.client("pptx", "apply", str(deck))
        self.update_to_new()
        part = deck / "parts" / "s01"
        loose = self.box.tmp / "elsewhere"
        loose.mkdir()
        self.client("pptx", "start", str(loose))
        (part / PIN).write_text((loose / PIN).read_text())                 # pinned to the new engine, unlike its deck
        self.assertEqual(self.client("pptx", "apply", str(part)).returncode, 0)
        self.assertEqual(self.used(part), self.ENGINE_VERSION)
        self.assertEqual(pin_of(part)["version"], self.ENGINE_VERSION)
        self.assertIn("the deck's pin wins", (self.box.home / "logs" / "client.log").read_text())

    def test_a_part_of_an_unpinned_deck_behaves_as_before(self):
        self.ready()
        deck = make_deck(self.box.tmp / "deck", ["s01"])
        part = deck / "parts" / "s01"
        self.assertEqual(self.client("pptx", "apply", str(part)).returncode, 0)
        self.assertEqual(pin_of(part)["version"], self.ENGINE_VERSION)
        self.assertFalse((deck / PIN).exists(), "the deck is not pinned by its part")

    def test_a_plain_workspace_is_unchanged(self):
        self.ready()
        ws = self.box.tmp / "plain"
        ws.mkdir()
        self.client("pptx", "start", str(ws))
        self.update_to_new()
        self.client("pptx", "apply", str(ws))
        self.assertEqual(self.used(ws), self.ENGINE_VERSION)
        new = self.box.tmp / "new plain"
        new.mkdir()
        self.client("pptx", "start", str(new))
        self.assertEqual(self.used(new), self.NEW)

    # --- defect 3: repin ---

    def test_repin_moves_a_deck_and_its_parts_in_one_call_and_keeps_the_previous_pins(self):
        self.ready()
        deck = make_deck(self.box.tmp / "deck", ["s01", "s02"])
        for ws in (deck, deck / "parts" / "s01", deck / "parts" / "s02"):
            self.client("pptx", "apply", str(ws))
        self.update_to_new()
        first, nxt, tell = self.assertStatus(self.client("repin", str(deck)), "OK", 0)
        self.assertIn(f"the deck and its 2 parts from engine {self.ENGINE_VERSION} to {self.NEW}", first)
        self.assertEqual(nxt, "NEXT: run the same build command again.")
        self.assertIn(f"Theodore {self.NEW}", tell)
        self.assertIn("redraws every slide once", tell)
        for ws in (deck, deck / "parts" / "s01", deck / "parts" / "s02"):
            self.assertEqual(pin_of(ws)["version"], self.NEW)
            previous = json.loads((ws / ".pptx-state" / "theodore-engine.json.previous").read_text())
            self.assertEqual(previous["version"], self.ENGINE_VERSION)
        self.assertEqual({r["version"] for r in self.state("state/pins.json")["workspaces"].values()}, {self.NEW})
        self.client("pptx", "apply", str(deck / "parts" / "s01"))
        self.assertEqual(self.used(deck / "parts" / "s01"), self.NEW)

    def test_repin_given_a_part_moves_the_whole_deck(self):
        self.ready()
        deck = make_deck(self.box.tmp / "deck", ["s01", "s02"])
        for ws in (deck, deck / "parts" / "s01"):
            self.client("pptx", "apply", str(ws))
        self.update_to_new()
        first, _nxt, _tell = self.assertStatus(self.client("repin", str(deck / "parts" / "s02")), "OK", 0)
        self.assertIn("2 parts", first)
        for ws in (deck, deck / "parts" / "s01", deck / "parts" / "s02"):
            self.assertEqual(pin_of(ws)["version"], self.NEW)
        self.assertFalse((deck / "parts" / "s02" / ".pptx-state" / "theodore-engine.json.previous").exists(), "it had no pin")

    def test_repin_of_a_deck_already_on_the_current_engine_changes_nothing(self):
        self.ready()
        deck = make_deck(self.box.tmp / "deck", ["s01"])
        self.client("pptx", "apply", str(deck))
        before = (deck / PIN).read_text()
        first, _nxt, tell = self.assertStatus(self.client("repin", str(deck)), "OK", 0)
        self.assertIn("already", first)
        self.assertIn(f"already uses Theodore {self.ENGINE_VERSION}", tell)
        self.assertEqual((deck / PIN).read_text(), before)
        self.assertFalse((deck / ".pptx-state" / "theodore-engine.json.previous").exists())

    def test_repin_of_a_folder_that_is_not_a_workspace_is_usage(self):
        self.ready()
        folder = self.box.tmp / "just a folder"
        folder.mkdir()
        self.assertStatus(self.client("repin", str(folder)), "USAGE", 64)
        self.assertIn("repin <workspace>", self.client("help").stdout)

    def test_update_required_names_repin_and_no_file_to_delete(self):
        self.ready()
        deck = make_deck(self.box.tmp / "deck", ["s01"])
        self.client("pptx", "apply", str(deck))
        self.update_to_new()
        import shutil
        shutil.rmtree(self.box.home / "engines" / self.ENGINE_VERSION)
        del self.api.engine_files[self.ENGINE_VERSION]
        result = self.client("pptx", "apply", str(deck / "parts" / "s01"))
        first, nxt, tell = self.assertStatus(result, "UPDATE_REQUIRED", 72)
        self.assertIn("repin", first)
        self.assertIn(f'repin "{deck.resolve()}"', nxt)
        self.assertIn("ask the user", nxt)
        self.assertIn("Shall I?", tell)
        self.assertNotIn("theodore-engine.json", result.stdout)
        self.assertNotIn("delete", (first + nxt).lower())


if __name__ == "__main__":
    unittest.main()
