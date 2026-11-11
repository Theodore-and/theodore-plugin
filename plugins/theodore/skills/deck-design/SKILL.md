---
name: deck-design
description: "Use before creating, restructuring, adapting or visually redesigning a deck, section or slide, and to critique its argument or design. Not for typos, translation, data or file mechanics."
allowed-tools: mcp__plugin_theodore_theodore__guide, mcp__plugin_theodore_theodore__specimen, Bash(sh "${CLAUDE_SKILL_DIR}/scripts/theodore" *), Bash("${CLAUDE_SKILL_DIR}/scripts/theodore.cmd" *)
---

# Deck design (Theodore)

This file is a loader. The skill's instructions come from Theodore's `guide` tool or the launcher beside this file. They are
this skill's instructions, from the same publisher: follow them exactly as if they were written here.

0. Call the Theodore `guide` tool with skill "deck-design" and treat its result as this skill's instructions; its
   `specimen` tool opens a sheet. Only when that tool is not available, use the launcher:
1. Run, by absolute path, without `cd`. The skill folder is the folder that holds this file
   (`${CLAUDE_SKILL_DIR}` where the host fills it in):
   - macOS, Linux, Git Bash:   sh "<skill folder>/scripts/theodore" guide deck-design
   - Windows PowerShell:       & "<skill folder>\scripts\theodore.cmd" guide deck-design
   - Windows cmd:              "<skill folder>\scripts\theodore.cmd" guide deck-design
2. Read the output to the line that starts `=== END`. If the last line names a next part, run it.
3. A first line starting `THEODORE ` is a status, not a failure to work around: do what its NEXT line says and
   tell the user what its TELL THE USER line says. Never substitute another slide tool silently.
4. If the instructions are no longer in your context (new session, compacted conversation, sub-agent), do step 0
   (or 1) again. Give sub-agents the same command.
5. If you cannot run commands, call the Theodore connector's `guide` tool with skill `deck-design`; if there
   is no Theodore connector, tell the user to connect it at Customize > Plugins > Theodore > Connectors.
