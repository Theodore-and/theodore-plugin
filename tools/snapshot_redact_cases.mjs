#!/usr/bin/env node
// Cut the case table of the server's redactor test out of the backend repository.
//
// The free-text redactor lives in the private backend (apps/worker/src/lib/redact.ts) and is ported to
// client/theodore_client/redact.py. This tool reads the `cases` array of apps/worker/test/integration/redact.test.ts,
// evaluates it (template-literal repeats expanded) and writes tests/fixtures/redact-cases.json, which
// tests/test_redact.py runs against the port. Strings that look like secrets are stored as placeholders
// ({SK_LIVE}, {JWT}) so no committed literal trips the secret scanner; the Python test expands them from parts.
//
// Usage: node tools/snapshot_redact_cases.mjs [<backend repo>] [--check]
//   The backend defaults to $THEODORE_BACKEND, then ../theodore beside this repository.
//   --check writes nothing and exits 1 when the fixture differs from what the backend's test holds.

import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const TARGET = join(ROOT, "tests", "fixtures", "redact-cases.json");
const TEST = join("apps", "worker", "test", "integration", "redact.test.ts");

// Built from parts so that no literal in this file matches a secret scanner rule; tests/test_redact.py has the same.
const PLACEHOLDERS = {
  "{SK_LIVE}": "sk_" + "live_" + "0123456789abcdef".repeat(2),
  "{JWT}": ["eyJ" + "hbGciOiJFZERTQSJ9", "eyJ" + "zdWIiOiJ4In0", "c2lnbmF0dXJl" + "LWJ5dGVz"].join("."),
};

const args = process.argv.slice(2);
const check = args.includes("--check");
const backend = args.find((arg) => arg !== "--check") ?? process.env.THEODORE_BACKEND ?? join(ROOT, "..", "theodore");
const source = readFileSync(join(backend, TEST), "utf8");

const declared = source.indexOf("const cases");
if (declared === -1) throw new Error(`no \`const cases\` in ${TEST}`);
const open = source.indexOf("= [", declared);
const close = source.indexOf("\n  ];", open);
if (open === -1 || close === -1) throw new Error(`cannot find the cases array in ${TEST}`);
const literal = source.slice(open + 2, close + 4); // `[` ... `]`, without the type annotation before `=`
const cases = vm.runInNewContext(`(${literal})`, {}, { timeout: 5000 });

const hide = (text) => {
  let out = text;
  for (const [placeholder, value] of Object.entries(PLACEHOLDERS)) out = out.split(value).join(placeholder);
  return out;
};
const rows = cases.map((c) => ({ expected: hide(c.expected), kinds: [...c.kinds], name: c.name, text: hide(c.text) }));
const json = `${JSON.stringify(rows, null, 1)}\n`;

if (check) {
  let current = "";
  try {
    current = readFileSync(TARGET, "utf8");
  } catch {
    // missing: differs
  }
  if (current !== json) {
    process.stderr.write(`${TARGET} differs from ${join(backend, TEST)}: run node tools/snapshot_redact_cases.mjs\n`);
    process.exit(1);
  }
  process.stdout.write(`${rows.length} cases match\n`);
} else {
  writeFileSync(TARGET, json);
  process.stdout.write(`wrote ${rows.length} cases to ${TARGET}\n`);
}
