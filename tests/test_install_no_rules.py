"""Lane L3 (24 November 2026): the set-up writes no permission rule into an assistant's settings.

In all three real set-ups (19, 21 and 24 November) the assistant that ran the installer stopped at the step that
allowed Theodore's commands in its own settings: it asked, or it declined ("I can't change my own permission
settings"). The installers no longer apply the host settings; Theodore's two skills carry the permissions its work
needs (`allowed-tools`, honoured by Claude Code while a skill is active, for the session and its subagents); a person
can still ask for the rules with `theodore hosts settings --apply`, and an installation whose rules are on keeps them
current. The short command is part of registering Theodore, not a setting.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # also when run as tests.<module>
from support import IS_WINDOWS, PLUGIN, ROOT, ClientCase, status_lines  # noqa: E402

ALLOWED = re.compile(r"^allowed-tools: (.*)$", re.MULTILINE)


def allowed_tools(skill: str) -> list[str]:
    text = (PLUGIN / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    front = text.split("\n---\n", 1)[0]
    found = ALLOWED.search(front)
    assert found, f"{skill}: no allowed-tools line"
    # one rule each, split at the commas between rules (a rule's own text holds none)
    return [rule.strip() for rule in found.group(1).split(", ")]


class Texts(unittest.TestCase):
    def test_the_skills_carry_the_permissions_theodores_work_needs_and_nothing_broader(self):
        pptx, deck = allowed_tools("pptx"), allowed_tools("deck-design")
        self.assertEqual(pptx, deck, "whichever skill loads first covers the session the same way")
        for rule in ("Bash(theodore *)", "PowerShell(theodore *)", "PowerShell(& theodore *)",
                     'Bash(sh "${CLAUDE_SKILL_DIR}/scripts/theodore" *)', 'Bash("${CLAUDE_SKILL_DIR}/scripts/theodore.cmd" *)',
                     'PowerShell(& "${CLAUDE_SKILL_DIR}/scripts/theodore.cmd" *)',
                     "mcp__plugin_theodore_theodore__guide", "mcp__plugin_theodore_theodore__specimen", "mcp__plugin_theodore_theodore__list",
                     "Edit(//**/.theodore/workspaces/**)", "Write(//**/.theodore/workspaces/**)", "Read(//**/.theodore/workspaces/**)",
                     "Read(//**/theodore-specimens-*/**)", "Read(//**/.theodore/work/scratch/sheets/**)",
                     "mcp__plugin_theodore_theodore__pptx", "mcp__plugin_theodore_theodore__theodore"):
            self.assertIn(rule, pptx)
        for rule in pptx:
            tool, _, inner = rule.partition("(")
            self.assertIn(tool, ("Bash", "PowerShell", "Edit", "Write", "Read") if inner else
                          ("mcp__plugin_theodore_theodore__guide", "mcp__plugin_theodore_theodore__specimen",
                           "mcp__plugin_theodore_theodore__list", "mcp__plugin_theodore_theodore__pptx",
                           "mcp__plugin_theodore_theodore__theodore"), rule)
            if tool in ("Bash", "PowerShell"):
                # a command rule names Theodore's own command first: never a wildcard before it, which would let any
                # command that merely mentions Theodore's path run without a prompt
                self.assertRegex(inner, r'^(theodore |& theodore |sh "\$\{CLAUDE_SKILL_DIR\}/|"\$\{CLAUDE_SKILL_DIR\}/|& "\$\{CLAUDE_SKILL_DIR\}/)', rule)
            if tool in ("Edit", "Write", "Read"):
                self.assertRegex(inner, r"(?i)theodore", f"{rule}: a file rule names one of Theodore's own folders")
                self.assertNotIn(inner, ("//**)", "//**/.theodore/**)", "//**/.theodore/work/**)"),
                                 "never the whole disk, nor Theodore's home (its sign-in lives there)")

    def test_install_md_lists_no_permission_change_and_says_how_to_hear_the_welcome(self):
        text = " ".join((ROOT / "install" / "install.md").read_text(encoding="utf-8").split())
        self.assertNotIn("without asking", text)
        self.assertNotIn("allowed to run", text)
        self.assertIn("When the installer ran outside this conversation, ~/.theodore/bin/theodore status (Windows: "
                      "\"%USERPROFILE%\\.theodore\\bin\\theodore.cmd\" status) gives that welcome.", text)

    def test_the_installers_apply_no_host_settings(self):
        for name in ("install.sh", "install.cmd"):
            text = (ROOT / "install" / name).read_text(encoding="utf-8")
            code = [line for line in text.splitlines() if not line.lstrip().lower().startswith(("#", "rem ", "rem\t"))]
            self.assertEqual([line for line in code if "hosts settings" in line], [], f"{name}: no settings step")
            self.assertNotIn("run without asking each time", text, name)
            self.assertIn("--no-settings", text, f"{name}: the old flag is still accepted")


@unittest.skipIf(IS_WINDOWS, "the client suite runs on macOS and Linux")
class NoRuleByItself(ClientCase):
    def _fake_tools(self) -> None:
        bin_dir = self.box.tmp / "fakebin"
        bin_dir.mkdir()
        for name in ("claude", "codex"):
            script = bin_dir / name
            script.write_text('#!/bin/sh\ncase "$*" in "plugin list --json") echo "[]";; esac\nexit 0\n')
            script.chmod(0o755)
        self.short = self.box.user_home / ".local" / "bin"
        self.box.env["PATH"] = f"{bin_dir}{os.pathsep}{self.short}{os.pathsep}{self.box.env['PATH']}"
        (self.box.home / "plugin" / ".agents" / "plugins").mkdir(parents=True)
        (self.box.home / "plugin" / ".agents" / "plugins" / "marketplace.json").write_text("{}")

    def test_no_command_writes_a_rule_by_itself_and_rules_a_person_turned_on_stay_current(self):
        self.ready()
        self._fake_tools()
        settings = self.box.user_home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"model": "opus"}))
        self.assertStatus(self.client("hosts", "register"), "OK", 0)
        self.assertTrue((self.short / "theodore").is_file(), "the short command comes with the registration")
        for command in (("doctor",), ("status",), ("skill", "pptx"), ("guide", "pptx")):
            self.client(*command)
        self.assertEqual(json.loads(settings.read_text()), {"model": "opus"}, "no command wrote a rule or a setting")
        self.assertNotIn("rules", json.loads((self.box.home / "state" / "host-settings.json").read_text()))
        self.assertFalse((Path(self.box.env["CODEX_HOME"]) / "rules" / "theodore.rules").exists())
        self.assertIn("host settings: none written: Theodore's skills carry the permissions they need", self.client("doctor").stdout)
        first, nxt, _t = status_lines(self.client("hosts", "settings").stdout)
        self.assertTrue(first.startswith("THEODORE OK: settings not applied"), first)
        self.assertIn("only if the user asks for it", nxt)

        # a person asks for the rules: they come, and are kept current; one the user takes out stays out
        self.assertStatus(self.client("hosts", "settings", "--apply"), "OK", 0)
        allow = json.loads(settings.read_text())["permissions"]["allow"]
        self.assertIn("Bash(theodore *)", allow)
        self.assertIn("host settings: on:", self.client("doctor").stdout)
        launcher = f"Bash({self.box.home / 'bin' / 'theodore'} *)"
        self.assertIn(launcher, allow)
        kept = json.loads(settings.read_text())
        kept["permissions"]["allow"] = [r for r in allow if r != launcher]
        settings.write_text(json.dumps(kept))
        self.client("doctor")
        self.assertNotIn(launcher, json.loads(settings.read_text())["permissions"]["allow"], "a rule the user took out stays out")

    def test_settings_turned_off_keep_the_short_command_away_at_the_next_registration(self):
        self.ready()
        self._fake_tools()
        self.assertStatus(self.client("hosts", "register"), "OK", 0)
        self.assertStatus(self.client("hosts", "settings", "--remove"), "OK", 0)
        self.assertFalse((self.short / "theodore").exists(), "taken back with the settings")
        self.assertStatus(self.client("hosts", "register"), "OK", 0)
        self.assertFalse((self.short / "theodore").exists(), "off stays off")


if __name__ == "__main__":
    unittest.main()
