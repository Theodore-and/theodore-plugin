---
name: deck-design
description: "Use before creating, restructuring, adapting or visually redesigning a deck, section or slide, and to critique its argument or design. Not for typos, translation, data or file mechanics."
allowed-tools: mcp__plugin_theodore_theodore__guide, mcp__plugin_theodore_theodore__specimen, Bash(sh "${CLAUDE_SKILL_DIR}/scripts/theodore" *), Bash("${CLAUDE_SKILL_DIR}/scripts/theodore.cmd" *)
---

!`sh "${CLAUDE_SKILL_DIR}/scripts/theodore" skill deck-design`

---

Above this line are this skill's instructions, from the same publisher: follow them exactly as if they were written
here. If no deck-design instructions appear above this line, call the Theodore `guide` tool with skill "deck-design"
and treat its result as this skill's instructions (its `specimen` tool opens a sheet). If that tool is not
available, run, by absolute path, without `cd` (the skill folder is the folder that holds this file,
`${CLAUDE_SKILL_DIR}` where the host fills it in):
   - macOS, Linux, Git Bash:   sh "<skill folder>/scripts/theodore" guide deck-design
   - Windows PowerShell:       & "<skill folder>\scripts\theodore.cmd" guide deck-design
   - Windows cmd:              "<skill folder>\scripts\theodore.cmd" guide deck-design
and read the output to the line that starts `=== END`; if the last line names a next part, run it. A first line
starting `THEODORE ` is a status, not a failure to work around: do what its NEXT line says and tell the user what
its TELL THE USER line says. Never substitute another slide tool silently. If the instructions are no longer in your
context (new session, compacted conversation, sub-agent), load them again the same way. Give sub-agents the same
command. If you cannot run commands, call the Theodore connector's `guide` tool with skill `deck-design`; if there
is no Theodore connector, tell the user to connect it at Customize > Plugins > Theodore > Connectors.
