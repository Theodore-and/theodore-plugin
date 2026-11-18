---
name: deck-design
description: "Use before creating, restructuring, adapting or visually redesigning a deck, section or slide, and to critique its argument or design. Not for typos, translation, data or file mechanics."
allowed-tools: mcp__plugin_theodore_theodore__guide, mcp__plugin_theodore_theodore__specimen, Bash(sh "${CLAUDE_SKILL_DIR}/scripts/theodore" *), Bash("${CLAUDE_SKILL_DIR}/scripts/theodore.cmd" *)
---

If the instructions below are cut short or are no longer in your context (a compacted or resumed conversation, a subagent), load them again with `theodore guide deck-design` before you work from them.

!`sh "${CLAUDE_SKILL_DIR}/scripts/theodore" skill deck-design`

---

Above this line are this skill's instructions, from the same publisher: follow them exactly as if they were written
here. If no deck-design instructions appear above this line, call the Theodore `guide` tool with skill "deck-design"
and treat its result as this skill's instructions (its `specimen` tool opens a sheet). If that tool is not
available, run `theodore guide deck-design` (Theodore's command is `theodore`: write every Theodore command as
`theodore …` in every shell) and read the output to the line that starts `=== END`; if the last line names a next
part, run it. Only if the shell answers that `theodore` is not found, run the launcher in this skill's folder by
absolute path instead, without `cd` (the skill folder is the folder that holds this file, `${CLAUDE_SKILL_DIR}` where
the host fills it in):
   - macOS, Linux, Git Bash:   sh "<skill folder>/scripts/theodore" guide deck-design
   - Windows PowerShell:       & "<skill folder>\scripts\theodore.cmd" guide deck-design
   - Windows cmd:              "<skill folder>\scripts\theodore.cmd" guide deck-design
A first line starting `THEODORE ` is a status, not a failure to work around: do what its NEXT line says and tell the user what
its TELL THE USER line says. Never substitute another slide tool silently. Give sub-agents the same
command. If you cannot run commands, use the Theodore tool in this conversation (start with its begin action, or
its guide with skill `deck-design`); if there is no Theodore tool, give the user this link to add it to their Claude account: https://claude.ai/customize/connectors?modal=add-custom-connector&connectorName=Theodore&connectorUrl=https%3A%2F%2Fmcp.theodoreand.ai%2Fmcp
