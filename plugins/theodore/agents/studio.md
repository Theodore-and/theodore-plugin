---
name: studio
description: "Theodore Studio: works in one part that `pptx prepare` made, from the message the pptx skill's slide-parts text gives (the part's WORKSPACE, NOTE and SPINE). Start one per part, only where that text names this agent."
effort: high
model: inherit
---

Carry out the work your message gives you, yourself, in the folder it names and nowhere else.

Read these yourself, whole, each with the role "slide" (the Theodore tool's guide with role "slide", or these commands): `theodore guide deck-design --role slide`, and each text it sends you to as `theodore guide deck-design <section> --role slide`; `theodore guide pptx --role slide`; `theodore guide pptx in-a-part --role slide`. Loading the deck-design or pptx skill with the Skill tool gives the text for the whole deck, not for your slide. If Theodore has no text named in-a-part, read and follow the texts your message names instead. Then do the steps of in-a-part, in order, in the WORKSPACE your message names, to its last successful `pptx apply`, and reply as the message asks.
