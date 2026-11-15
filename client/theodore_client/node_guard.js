// Theodore's guard for the Node processes its engine starts on behalf of a chat (`mcp --for claude-desktop`).
//
// Loaded first by NODE_OPTIONS (--require), beside Node's permission model (--permission), which already limits the
// file system to Theodore's folders and the deck's folder and refuses child processes, worker threads, the inspector,
// WASI and process.binding. What the permission model of Node 24 does not limit is the network, and native addons
// once allowed; this file closes both for the script a model wrote (deck.js):
//   - no network: the network modules answer with a refusal, and fetch, WebSocket and EventSource are gone;
//   - native addons only from the runtime's own node_modules (sharp, skia-canvas), never from a file a script wrote.
// Everything is fixed before any other code runs and cannot be undone from inside the process: the originals are kept
// only in this closure, and the replaced properties are neither writable nor configurable.
"use strict";
(() => {
  const Module = require("module");
  const path = require("path");
  const fs = require("fs");

  const refusal = (what) => {
    const error = new Error(`${what} is not available to deck scripts that Theodore runs for a chat: no network, no other programs. ` +
                            "Use pictures from the user's files or Theodore's packs.");
    error.code = "ERR_THEODORE_SANDBOX";
    return error;
  };

  const BLOCKED = new Set([
    "net", "tls", "http", "https", "http2", "dgram", "dns", "dns/promises", "cluster", "child_process", "worker_threads",
    "inspector", "inspector/promises", "wasi", "trace_events", "repl", "sqlite", "_http_agent", "_http_client",
    "_http_common", "_http_incoming", "_http_outgoing", "_http_server", "_tls_common", "_tls_wrap",
  ]);
  const bare = (request) => (typeof request === "string" && request.startsWith("node:") ? request.slice(5) : request);
  const blocked = (request) => typeof request === "string" && BLOCKED.has(bare(request)) &&
    (request.startsWith("node:") || Module.isBuiltin(request));

  // A blocked module loads as a stand-in: requiring it, reading or setting its properties succeeds (a library that
  // only names it, or patches it as the engine's own no_web_media.js does, keeps working); calling or constructing
  // anything from it throws. Nothing in it reaches the real module.
  const standIn = (name) => {
    const refuse = function () { throw refusal(`node:${name}`); };
    return new Proxy({}, {
      get: (target, key) => (key in target ? target[key] : typeof key === "symbol" || key === "__esModule" ? undefined : refuse),
    });
  };
  const standIns = new Map();
  const standInFor = (request) => {
    const name = bare(request);
    if (!standIns.has(name)) standIns.set(name, standIn(name));
    return standIns.get(name);
  };

  const fix = (object, key, value) => Object.defineProperty(object, key, { value, writable: false, configurable: false, enumerable: false });

  const load = Module._load;
  const inside = (file, root) => (process.platform === "win32" ? file.toLowerCase().startsWith(root.toLowerCase()) : file.startsWith(root));
  const realOrNull = (file) => { try { return fs.realpathSync(String(file)); } catch (_e) { return null; } };

  // child_process: the engine starts two kinds of programs from a deck script's process: Node itself on one of its own
  // scripts (icon and SVG rasterising), and fc-match (a font lookup with a file-system fallback). The stand-in runs
  // only the first: this Node binary (never one found on PATH), a script inside the engine, no Node options before
  // it, the environment as it was when this process started (so NODE_OPTIONS, the permission model and this guard
  // apply to the child too), no shell. Everything else fails as a missing program would (spawnSync) or throws.
  const NODE = process.execPath;
  const ENV = Object.freeze({ ...process.env });
  const scriptRoot = realOrNull(process.env.THEODORE_NODE_SCRIPT_ROOT || "");
  const childProcess = load.call(Module, "child_process", null, false);
  const KEPT = ["encoding", "timeout", "maxBuffer", "windowsHide", "stdio", "input", "killSignal", "cwd"];
  const checked = (file, args) => {
    if (!(file === "node" || file === NODE) || !Array.isArray(args) || args.length === 0) throw refusal(`running ${String(file)}`);
    const script = realOrNull(args[0]);
    if (!scriptRoot || !script || !inside(script, scriptRoot + path.sep) || !script.endsWith(".js")) throw refusal(`running ${String(file)} ${String(args[0])}`);
    return [NODE, [script, ...args.slice(1).map(String)]];
  };
  const options = (given) => {
    const out = { env: { ...ENV }, shell: false };
    for (const key of KEPT) if (given && Object.prototype.hasOwnProperty.call(given, key)) out[key] = given[key];
    return out;
  };
  const cpStandIn = standIn("child_process");
  const cpFacade = new Proxy({}, {
    get: (_t, key) => {
      if (key === "execFileSync") return (file, args, given) => { const [f, a] = checked(file, args); return childProcess.execFileSync(f, a, options(given)); };
      if (key === "spawnSync") return (file, args, given) => {
        try {
          const [f, a] = checked(file, args);
          return childProcess.spawnSync(f, a, options(given));
        } catch (error) {
          const empty = given && given.encoding && given.encoding !== "buffer" ? "" : Buffer.alloc(0);
          return { pid: 0, output: [null, empty, empty], stdout: empty, stderr: empty, status: null, signal: null, error };
        }
      };
      return cpStandIn[key];
    },
  });
  const standInOf = (request) => (bare(request) === "child_process" ? cpFacade : standInFor(request));

  fix(Module, "_load", function (request, parent, isMain) {
    if (blocked(request)) return standInOf(request);
    return load.call(this, request, parent, isMain);
  });
  if (typeof process.getBuiltinModule === "function") {
    const builtin = process.getBuiltinModule;
    fix(process, "getBuiltinModule", (id) => (blocked(id) ? standInOf(id) : builtin(id)));
  }
  // import() and ESM `import` do not go through Module._load: a blocked module imported that way resolves to a module
  // whose default export is the same stand-in (pptxgenjs imports node:https's default for media given by URL).
  if (typeof Module.registerHooks !== "function") throw refusal("this Node version (no module.registerHooks)");
  const STAND_IN = Symbol.for("theodore.sandbox.standIn");
  fix(globalThis, STAND_IN, (name) => standInOf(name));
  const SCHEME = "theodore-blocked:";
  Module.registerHooks({
    resolve(specifier, context, next) {
      if (blocked(specifier) && !(context.conditions || []).includes("require")) return { url: SCHEME + bare(specifier), shortCircuit: true };
      return next(specifier, context);
    },
    load(url, context, next) {
      if (typeof url === "string" && url.startsWith(SCHEME)) {
        const name = JSON.stringify(url.slice(SCHEME.length));
        return { format: "module", shortCircuit: true, source: `export default globalThis[Symbol.for("theodore.sandbox.standIn")](${name});` };
      }
      return next(url, context);
    },
  });

  for (const name of ["fetch", "WebSocket", "EventSource"]) {
    const gone = () => { throw refusal(name); };
    try { Object.defineProperty(globalThis, name, { get: () => gone, set: () => {}, configurable: false }); } catch (_e) { /* not defined */ }
  }

  // Native addons: only files under the folders named in THEODORE_NODE_ADDON_ROOTS (the runtime's node_modules).
  const roots = String(process.env.THEODORE_NODE_ADDON_ROOTS || "").split(path.delimiter).filter(Boolean).map((root) => {
    try { return fs.realpathSync(root) + path.sep; } catch (_e) { return null; }
  }).filter(Boolean);
  const dlopen = process.dlopen;
  fix(process, "dlopen", function (module, filename, flags) {
    let real = null;
    try { real = fs.realpathSync(String(filename).replace(/^\\\\\?\\/, "")); } catch (_e) { real = null; }
    const ok = real && roots.some((root) => (process.platform === "win32" ? real.toLowerCase().startsWith(root.toLowerCase()) : real.startsWith(root)));
    if (!ok) throw refusal("a native addon outside Theodore's runtime");
    return flags === undefined ? dlopen.call(this, module, filename) : dlopen.call(this, module, filename, flags);
  });
})();
