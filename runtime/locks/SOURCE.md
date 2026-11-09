# Where these locks come from

`requirements.txt`, `package.json` and `package-lock.json` are byte-identical copies of the engine's open-source
dependency locks. They list only open-source packages. They are copied, never edited here: a change is made in
the engine's source repository and copied again, and the runtime id (see `../build/README.md`) changes with it.

| File | sha256 |
| --- | --- |
| `requirements.txt` | `95b65682e2560cc7a489c2b58fbc723456576cdfbeb1dfedc717f3b695905285` |
| `package.json` | `40eb4b683906f538168719515c83cbbf67169c8d7ab940432c75cec1c07d4294` |
| `package-lock.json` | `c99d471adf2bf4ecaee76b999f75135211f114ae8a06971ee42ceda4de9a64ec` |

Source commit: `dc668fd74b66b2c111ea79e28cfc85755fb18aff` (2026-11-09).

`runtime-extra.txt` (packages the runtime adds: the PDF rasteriser and the system-certificate TLS helper) and
`versions.json` (Python and Node pins, recipe version) are maintained in this repository.

`tests/test_repo.py` checks the three hashes above against the files, so a drift is visible.
