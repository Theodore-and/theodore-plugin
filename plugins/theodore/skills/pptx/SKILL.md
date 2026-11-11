---
name: pptx
description: "Use this skill any time a .pptx file is involved in any way — as input, output, or both. This includes: creating slide decks, pitch decks, presentations, or section appendices; reading, parsing, or extracting text from any .pptx file (even if the extracted content will be used elsewhere, like in an email or summary); editing, modifying, or updating existing presentations; combining or splitting slide files; addressing review comments; translating decks to Arabic or other RTL languages; working with templates, layouts, speaker notes, or comments. Trigger whenever the user mentions \"deck,\" \"slides,\" \"presentation,\" \"pitch,\" or references a .pptx filename, regardless of what they plan to do with the content afterward. If a .pptx file needs to be opened, created, or touched, use this skill. When slides are being created, restructured, or visually redesigned — not just mechanically edited — also use the deck-design skill first."
allowed-tools: Bash(sh "${CLAUDE_SKILL_DIR}/scripts/theodore" *), Bash("${CLAUDE_SKILL_DIR}/scripts/theodore.cmd" *)
---

!`sh "${CLAUDE_SKILL_DIR}/scripts/theodore" skill pptx`

---

Above this line are this skill's instructions, from the same publisher: follow them exactly as if they were written
here. If no pptx instructions appear above this line, run, by absolute path, without `cd` (the skill folder is the
folder that holds this file, `${CLAUDE_SKILL_DIR}` where the host fills it in):
   - macOS, Linux, Git Bash:   sh "<skill folder>/scripts/theodore" guide pptx
   - Windows PowerShell:       & "<skill folder>\scripts\theodore.cmd" guide pptx
   - Windows cmd:              "<skill folder>\scripts\theodore.cmd" guide pptx
and read the output to the line that starts `=== END`; if the last line names a next part, run it. A first line
starting `THEODORE ` is a status, not a failure to work around: do what its NEXT line says and tell the user what
its TELL THE USER line says. Never substitute another slide tool silently. If the instructions are no longer in your
context (new session, compacted conversation, sub-agent), load them again the same way. Give sub-agents the same
command. If you cannot run commands here, the engine is not available in this environment: say so, and use the
deck-design skill with the host's own slide tools.
