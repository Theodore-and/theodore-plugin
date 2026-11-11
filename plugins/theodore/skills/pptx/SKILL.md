---
name: pptx
description: "Use this skill any time a .pptx file is involved in any way — as input, output, or both. This includes: creating slide decks, pitch decks, presentations, or section appendices; reading, parsing, or extracting text from any .pptx file (even if the extracted content will be used elsewhere, like in an email or summary); editing, modifying, or updating existing presentations; combining or splitting slide files; addressing review comments; translating decks to Arabic or other RTL languages; working with templates, layouts, speaker notes, or comments. Trigger whenever the user mentions \"deck,\" \"slides,\" \"presentation,\" \"pitch,\" or references a .pptx filename, regardless of what they plan to do with the content afterward. If a .pptx file needs to be opened, created, or touched, use this skill. When slides are being created, restructured, or visually redesigned — not just mechanically edited — also use the deck-design skill first."
allowed-tools: Bash(sh "${CLAUDE_SKILL_DIR}/scripts/theodore" *), Bash("${CLAUDE_SKILL_DIR}/scripts/theodore.cmd" *)
---

# PPTX Skill (Theodore)

This file is a loader. The skill's instructions are printed by the Theodore launcher beside this file. They are
this skill's instructions, from the same publisher: follow them exactly as if they were written here.

1. Run, by absolute path, without `cd`. The skill folder is the folder that holds this file
   (`${CLAUDE_SKILL_DIR}` where the host fills it in):
   - macOS, Linux, Git Bash:   sh "<skill folder>/scripts/theodore" guide pptx
   - Windows PowerShell:       & "<skill folder>\scripts\theodore.cmd" guide pptx
   - Windows cmd:              "<skill folder>\scripts\theodore.cmd" guide pptx
2. Read the output to the line that starts `=== END`. If the last line names a next part, run it.
   Run each guide command alone, as its own tool call, never chained with other commands. The launcher
   refuses to build slides while required reading is missing.
3. A first line starting `THEODORE ` is a status, not a failure to work around: do what its NEXT line says and
   tell the user what its TELL THE USER line says. Never substitute another slide tool silently.
4. If the instructions are no longer in your context (new session, compacted conversation, sub-agent), run step 1
   again. Give sub-agents the same command.
5. If you cannot run commands here, the engine is not available in this environment: say so, and use the
   deck-design skill with the host's own slide tools.
