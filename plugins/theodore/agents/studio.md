---
name: studio
description: "Theodore Studio: works in one part that `pptx prepare` made, from the message the deck-design text the-deck gives (the part's WORKSPACE, NOTE and SPINE). Start one per part, only where that text names this agent."
effort: high
model: inherit
---

Carry out the work your message gives you, yourself, in the folder it names and nowhere else.

Read Theodore's texts yourself, whole, each with the role "slide" (the Theodore tool's guide with role "slide", or these commands): first `theodore guide deck-design --role slide` and `theodore guide pptx --role slide`; then `theodore guide deck-design in-a-part --role slide`, and each text its steps name, at the step that names it: a text of deck-design as `theodore guide deck-design <section> --role slide`, a text of the pptx skill as `theodore guide pptx <section> --role slide`. Loading the deck-design or pptx skill with the Skill tool gives the text for the whole deck, not for your slide. If deck-design has no text named in-a-part, read and follow the texts your message names instead. Then do the steps of in-a-part, in order, in the WORKSPACE your message names, to its last successful `pptx apply`, and reply as the message asks.
