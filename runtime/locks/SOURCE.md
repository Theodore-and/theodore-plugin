# Where these locks come from

`requirements.txt`, `package.json` and `package-lock.json` are byte-identical copies of the engine's open-source
dependency locks. They list only open-source packages. They are copied, never edited here: a change is made in
the engine's source repository and copied again, and the runtime id (see `../build/README.md`) changes with it.

| File | sha256 |
| --- | --- |
| `requirements.txt` | `95b65682e2560cc7a489c2b58fbc723456576cdfbeb1dfedc717f3b695905285` |
| `package.json` | `873417ccba2b5096170e053cdc64292a297bcc6c1653301dd0f440890791e1b8` |
| `package-lock.json` | `3961acfd10795933d7b2f001e78532dcbd83f2c317a94386b4d25ae9fc461f4e` |

Source commit: `d7da0a9f23891bbba8305e7363d2ff723b356255` (2026-11-25, engine 2.0: pptx-automizer and npm pptxgenjs are no
longer dependencies; jszip and pako, what the engine's own pptxgenjs needs, are pinned). The commit that lands on the
engine's main line may carry another id; the three files' bytes are what counts.

`runtime-extra.txt` (packages the runtime adds: the PDF rasteriser and the system-certificate TLS helper) and
`versions.json` (Python and Node pins, recipe version) are maintained in this repository.

`tests/test_repo.py` checks the three hashes above against the files, so a drift is visible.
