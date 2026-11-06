#!/usr/bin/env node
/**
 * axuse — CLI.
 *
 *   axuse mcp          run the MCP server on stdio (default)
 *   axuse print-config emit harness config for: codex | claude-code | opencode | agy | zed | generic
 *   axuse doctor       environment + smoke-test check
 *   axuse setup        guided native-host + extension setup
 *   axuse open-urls    bounded URL inspection report
 *   axuse help
 */
import { fileURLToPath } from "node:url";
import { spawn, spawnSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { locateBrowser } from "./core/cdp.js";
import { Controller } from "./core/controller.js";
import { runStdio } from "./mcp/server.js";
import { resolveDesktopDriver } from "./desktop/index.js";
import { extractUrls, readUrlSource, renderUrlReport } from "./workflows/url-batch.js";

const HERE = fileURLToPath(import.meta.url);

function usage() {
  console.log(`axuse — accessibility-first browser & computer use for agent harnesses.

Usage:
  axuse mcp                                   run the MCP server over stdio
  axuse print-config [--harness NAME]         emit config for a harness
  axuse doctor                                check the environment + smoke test
      [--request-tcc]                         macOS: explicitly open the TCC
                                              prompts (Accessibility + Screen
                                              Recording) for this binary
  axuse install-browser [--channel Stable|Beta] [--version NNN]
                                              provision a pinned Chrome for
                                              Testing under ~/.cache/axuse —
                                              no sudo, no apt repo (servers)
  axuse posture [--json] [--verify-egress]    audit the live browser posture
                                              (UA, webdriver, WebGL, persona
                                              coherence, egress geo) — exit 1
                                              on failing checks (CI-able)
  axuse setup [--browser chrome|edge|chromium|chrome-for-testing]
                                              register bridge + open extension setup
  axuse open-urls <file|url|->                 inspect a URL list and write Markdown
  axuse plugins                               list loaded solver plugins
  axuse solve <provider> [--url URL]          run a loaded solver (e.g. turnstile)
                                              [--headless] [--max-attempts N] [--deadline-ms N]
                                              [--proxy http://u:p@host:port] [--timezone IANA]
                                              [--lang en-GB]                              per-instance persona
  axuse bridge                                run the bridge hub standalone
  axuse bridge install [--browser chrome|edge|chromium|chrome-for-testing]
                                              register the native host for the extension
  axuse bridge verify                         verify the extension is connected
  axuse bridge uninstall [--browser chrome|edge|chromium|chrome-for-testing]
  axuse bridge status

Harnesses: codex, claude-code, opencode, agy, zed, generic`);
}

function commandVector() {
  return ["node", HERE.replaceAll("\\", "/"), "mcp"];
}

function printConfig(harness) {
  const [cmd, ...args] = commandVector();
  const vec = [...args];
  const vectJson = JSON.stringify([cmd, ...vec]);
  switch (harness) {
    case "codex":
      console.log(`# Codex — append to ~/.codex/config.toml
[mcp_servers.axuse]
command = "${cmd}"
args = ${JSON.stringify(vec)}`);
      break;
    case "claude-code":
      console.log(`# Claude Code — run once (user scope):
claude mcp add --scope user axuse -- ${cmd} ${vec.join(" ")}

# or add to a project's .mcp.json:
${JSON.stringify({ mcpServers: { axuse: { type: "stdio", command: cmd, args: vec } } }, null, 2)}`);
      break;
    case "opencode":
      console.log(`# OpenCode — merge into opencode.json
${JSON.stringify({ mcp: { axuse: { type: "local", command: [cmd, ...vec] } } }, null, 2)}`);
      break;
    case "zed":
      console.log(`# Zed — merge into settings.json under context_servers:
${JSON.stringify({ context_servers: { axuse: { command: { path: cmd, args: vec } } } }, null, 2)}`);
      break;
    case "agy":
      console.log(`# Antigravity — register this as an MCP server in the agent's MCP settings
# (standard mcpServers block; the CLI also accepts it for any MCP-capable host):
${JSON.stringify({ mcpServers: { axuse: { type: "stdio", command: cmd, args: vec } } }, null, 2)}`);
      break;
    default:
      console.log(`# Generic mcpServers block (any MCP host):
${JSON.stringify({ mcpServers: { axuse: { type: "stdio", command: cmd, args: vec } } }, null, 2)}`);
  }
  console.log(`\nCommand vector: ${vectJson}`);
}

function hasFlag(name) {
  return rest.includes(name);
}

function numberArg(name) {
  const value = arg(name);
  if (value === undefined) return undefined;
  const n = Number(value);
  if (!Number.isFinite(n)) throw new Error(`${name} must be a number`);
  return n;
}

function positionalArgs() {
  const valueFlags = new Set(["--browser", "--harness", "--backend", "--output", "--settle-ms", "--excerpt-chars", "--timeout-ms"]);
  const out = [];
  for (let i = 0; i < rest.length; i++) {
    const token = rest[i];
    if (valueFlags.has(token)) { i++; continue; }
    if (token !== "-" && token.startsWith("-")) continue;
    out.push(token);
  }
  return out;
}

/** macOS product version via sw_vers (evidence line for doctor); "unknown"
 *  when sw_vers is unavailable (rare embedded setups). */
async function macOsVersion() {
  const { execFile } = await import("node:child_process");
  return new Promise((resolve) => {
    execFile("sw_vers", ["-productVersion"], { timeout: 5000 }, (err, stdout) => {
      resolve(err ? "unknown" : String(stdout).trim());
    });
  });
}

/** `security` presence (the Keychain CLI plugins use for secret fallback). */
async function keychainAvailable() {
  const { execFile } = await import("node:child_process");
  return new Promise((resolve) => {
    execFile("security", ["help"], { timeout: 5000 }, (err) => resolve(!err));
  });
}

function openExtensionsPage(browser, browserPath) {  const url = "chrome://extensions";
  const launch = (command, args, label) => {
    const child = spawn(command, args, { detached: true, stdio: "ignore" });
    child.once("error", (e) => process.stderr.write(`[axuse] could not open ${label}: ${e.message}\n`));
    child.unref();
    return label;
  };
  if (process.platform === "darwin") {
    if (browserPath) return launch(browserPath, [url], browserPath);
    const apps = {
      edge: "Microsoft Edge",
      "edge-beta": "Microsoft Edge Beta",
      chromium: "Chromium",
      "chrome-for-testing": "Google Chrome for Testing",
      "chrome-beta": "Google Chrome Beta",
      "chrome-canary": "Google Chrome Canary",
    };
    const app = apps[browser] ?? "Google Chrome";
    return launch("open", ["-a", app, url], app);
  }
  const command = browserPath
    ?? (process.platform === "win32" ? "chrome.exe" : undefined)
    ?? (process.platform !== "darwin" ? locateBrowser() : undefined)
    ?? "google-chrome";
  return launch(command, [url], browserPath ?? command);
}

async function setupCmd() {
  const browser = arg("--browser") ?? "chrome";
  if (!["chrome", "edge", "edge-beta", "chromium", "chrome-for-testing", "chrome-beta", "chrome-canary"].includes(browser)) {
    throw new Error("--browser must be chrome, edge, edge-beta, chromium, chrome-for-testing, chrome-beta, or chrome-canary");
  }
  const { installBridge } = await import("./bridge/install.js");
  const browserPath = locateBrowser({ browser, exact: true });
  const inst = await installBridge({ browser });

  console.log(`✓ axuse bridge registered for ${browser}`);
  console.log(`  native host: ${inst.hostPath}`);
  console.log(`  extension id: ${inst.extensionId}`);
  console.log("\nFinish setup in Chrome:");
  console.log("  1. Turn on Developer mode.");
  console.log("  2. Click Load unpacked.");
  console.log(`  3. Select: ${inst.extensionDir}`);
  console.log("\nThen verify the connection:");
  console.log("  axuse bridge verify");

  if (!hasFlag("--no-open")) {
    try {
      const opened = openExtensionsPage(browser, browserPath);
      console.log(`\nOpened ${opened} at chrome://extensions. Use --no-open to skip this next time.`);
    } catch (e) {
      console.log(`\nCould not open the extensions page automatically: ${e.message}`);
    }
  }

  const harness = arg("--harness");
  if (harness) {
    console.log(`\nMCP configuration for ${harness}:`);
    printConfig(harness);
  }
}

async function openUrlsCmd() {
  const source = positionalArgs()[0];
  if (!source) throw new Error("open-urls requires a URL, a text/Markdown file, or - for stdin");
  const urls = extractUrls(readUrlSource(source));
  if (!urls.length) throw new Error(`No http(s) URLs found in ${source}`);
  if (urls.length > 100) throw new Error(`At most 100 URLs can be opened per batch (found ${urls.length})`);
  const backend = arg("--backend") ?? "cdp";
  if (!["cdp", "extension"].includes(backend)) throw new Error("--backend must be cdp or extension");
  const output = arg("--output") ?? "axuse-url-report.md";
  const controller = new Controller();
  let batch;
  try {
    batch = await controller.execute("open_many", {
      urls,
      backend,
      headless: hasFlag("--headless") ? true : undefined,
      settle_ms: numberArg("--settle-ms"),
      excerpt_chars: numberArg("--excerpt-chars"),
      close_after: true,
    });
  } finally {
    await controller.dispose();
  }
  writeFileSync(output, renderUrlReport({ results: batch.results, backend, source }) + "\n");
  console.log(`Wrote ${batch.opened}/${batch.count} opened URL results to ${resolve(output)}`);
  if (batch.failed) console.log(`${batch.failed} URL${batch.failed === 1 ? "" : "s"} failed; see the report for details.`);
}

async function pluginsCmd() {
  const { loadSolvers, pluginDirs } = await import("./plugins/index.js");
  const { solvers, errors } = await loadSolvers();
  const dirs = pluginDirs();
  console.log(`search: ${dirs.join("  ;  ")}`);
  if (!solvers.length && !errors.length) {
    console.log("solvers: none loaded (open core ships none — plugins are separate packages)");
    console.log("  install: drop a solver package into plugins/ or list dirs in AXUSE_PLUGINS");
    return;
  }
  for (const s of solvers) {
    console.log(`✓ ${s.id}@${s.version} — providers: ${s.providers.join(", ")}${s.tools?.length ? ` · tools: ${s.tools.map((t) => t.name).join(", ")}` : ""} (${s.dir})`);
  }
  for (const e of errors) {
    console.error(`✗ ${e.plugin}: [${e.code}] ${e.message}`);
  }
  if (errors.length) process.exitCode = 1;
}

async function solveCmd() {
  const provider = positionalArgs()[0];
  if (!provider) throw new Error("solve requires a provider, e.g. axuse solve turnstile --url <page>");
  const { loadSolvers, buildSolveContext, solveWith } = await import("./plugins/index.js");
  const { solvers } = await loadSolvers();
  const controller = new Controller();
  let result;
  try {
    if (arg("--url")) {
      await controller.execute("open", {
        url: arg("--url"),
        ...(hasFlag("--headless") ? { headless: true } : {}),
        ...(arg("--proxy") ? { proxy: arg("--proxy") } : {}),
        ...(arg("--proxy-bypass") ? { proxy_bypass: arg("--proxy-bypass") } : {}),
        ...(arg("--timezone") ? { timezone: arg("--timezone") } : {}),
        ...(arg("--lang") ? { lang: arg("--lang") } : {}),
        settle_ms: 4000,
      });
    }
    const opts = {};
    if (numberArg("--max-attempts")) opts.maxAttempts = numberArg("--max-attempts");
    if (numberArg("--deadline-ms")) opts.deadlineMs = numberArg("--deadline-ms");
    const ctx = buildSolveContext(controller);
    result = await solveWith(solvers, provider, ctx, opts);
    // Posture facts ride along (see ax_solve_captcha): which browser shape
    // fought the challenge, with the loud warning when it was headless.
    const posture = controller.browserPosture();
    result = { ...result, posture };
    if (posture.posture === "headless") {
      result.warning = "the browser is headless — challenge/anti-bot pages detect --headless first; rerun without --headless (on a display-less host, axuse server mode keeps Chrome headed)";
    }
  } finally {
    await Promise.race([controller.dispose(), new Promise((ok) => setTimeout(ok, 5000))]);
  }
  console.log(JSON.stringify(result, null, 2));
  if (!result?.solved) process.exitCode = 2;
}

async function verifyBridge() {
  const { getBridge } = await import("./bridge/hub.js");
  let bridge;
  try {
    bridge = await getBridge();
    await bridge.waitExt(numberArg("--timeout-ms") ?? 5000);
    const tabs = await bridge.rpc("list_tabs", {});
    if (!tabs.ok) throw new Error(String(tabs.error ?? "the extension rejected list_tabs"));
    console.log(`✓ axuse bridge connected (${tabs.result?.tabs?.length ?? 0} browser tabs visible).`);
  } catch (e) {
    console.error("✗ axuse bridge is not connected.");
    console.error(`  ${e.message}`);
    console.error("  Run `axuse setup`, load the unpacked extension, then try again.");
    process.exitCode = 1;
  } finally {
    if (bridge?.stop) bridge.stop();
    else if (bridge?.close) bridge.close();
  }
}

async function doctor() {
  const out = (s) => console.log(s);
  const requestTcc = process.argv.includes("--request-tcc");
  out(`node: ${process.version} ${process.platform}`);
  if (process.platform === "darwin") {
    out(`macos: ${await macOsVersion()} (${process.arch})`);
    out(`keychain: ${await keychainAvailable() ? "available (plugins can read secrets via service 'axuse')" : "unavailable (passphrases/keys must come from env vars)"}`);
  }
  const hasWs = typeof WebSocket !== "undefined";
  out(`native WebSocket: ${hasWs ? "yes" : "NO — need Node >= 22"}`);
  const browser = locateBrowser();
  if (browser) {
    // Version visibility: fleets drift; a 3-year-old persona is its own signal.
    let ver = "";
    try {
      const v = spawnSync(browser, ["--version"], { timeout: 15_000, encoding: "utf8" });
      ver = String(v.stdout ?? "").trim() ? ` (${String(v.stdout).trim()})` : "";
    } catch {}
    out(`browser: ${browser}${ver}`);
    if (process.platform === "linux") {
      const { managedVersion, MANAGED_ROOT_DEFAULT } = await import("./core/managed-browser.js");
      const mv = managedVersion();
      if (mv) out(`managed browser: ${mv} at ${MANAGED_ROOT_DEFAULT()} (axuse install-browser --version ${mv} pins a fleet)`);
    }
    // Best-effort drift visibility (network INFO, never a failure): what the
    // current CfT stable channel is vs what this box runs. A persona on a
    // browser several majors behind is its own signal; pinned fleets
    // (install-browser --version) upgrade deliberately.
    try {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), 3500);
      const res = await fetch("https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json", { signal: ctrl.signal });
      clearTimeout(t);
      const j = await res.json();
      const latest = j?.channels?.Stable?.version;
      if (latest && ver) {
        const major = (s) => Number(/(\d+)\./.exec(s)?.[1] ?? 0);
        const drift = major(ver) && major(latest) ? major(latest) - major(ver) : null;
        out(`browser currency: running ${major(ver) || "?"}, CfT stable ${major(latest) || "?"}${drift !== null && drift > 0 ? ` — ${drift} major(s) behind (managed installs pin deliberately; distro Chrome drifts with unattended-upgrades)` : ""}`);
      }
    } catch { /* offline or slow: currency is INFO, never a failure */ }
  } else {
    out("browser: not found — run `axuse install-browser` (pins Chrome for Testing under ~/.cache — no sudo), or install distro Chrome, or set AXUSE_BROWSER");
  }
  // Server-mode readiness, hoisted: the desktop-driver section below and the
  // smoke test both report against the same facts.
  let serverModeReady = false;
  let serverModeActive = false;
  if (process.platform === "linux") {
    // Chrome keeps shared memory in /dev/shm; a full tmpfs kills headed AND
    // headless launches with a bare crash. axuse auto-passes
    // --disable-dev-shm-usage, but the underlying pressure is worth knowing.
    try {
      const { statfsSync } = await import("node:fs");
      const st = statfsSync("/dev/shm");
      const free = (BigInt(st.bavail) * BigInt(st.bsize)) / 1024n / 1024n;
      const total = (BigInt(st.blocks) * BigInt(st.bsize)) / 1024n / 1024n;
      const pct = total > 0n ? Number((free * 100n) / total) : 100;
      if (free < 128n) {
        out(`/dev/shm: ${free} MB free of ${total} MB — Chrome needs ≥64MB; axuse adds --disable-dev-shm-usage automatically, but free space if launches still fail`);
      } else if (pct < 5) {
        out(`/dev/shm: low (${free} MB free, ${pct.toFixed(0)}%) — axuse mitigates with --disable-dev-shm-usage`);
      }
    } catch {
      // no /dev/shm mount — the launch path mitigates silently
    }
    // Server mode: the anti-bot-safe lane for display-less hosts (headed
    // Chrome on an auto-booted Xvfb — never --headless, which challenge
    // pages detect first).
    const vd = await import("./core/vdisplay.js");
    const displayAlive = vd.displayUsable();
    if (displayAlive) {
      out(`server mode: not needed — X display ${process.env.DISPLAY} is alive; Chrome launches headed on it`);
    } else if (!vd.serverModeEnabled()) {
      out("server mode: DISABLED (AXUSE_SERVER_MODE=0) and no live X display — headed Chrome will fail typed; unset AXUSE_SERVER_MODE to auto-boot Xvfb, or run under xvfb-run / in a desktop session");
    } else if (!vd.xvfbBinary()) {
      out("server mode: NOT READY — no live X display and Xvfb not found: sudo apt-get install -y xvfb (challenge/anti-bot pages detect --headless Chrome; headed-on-Xvfb is the supported server lane; headless stays available for trusted internal pages via AXUSE_HEADLESS=1 or headless: true)");
    } else {
      serverModeReady = true;
      const geo = vd.displayGeometry();
      out(`server mode: ready — no live X display; headed Chrome will auto-boot a private Xvfb (${geo.w}x${geo.h}x${geo.d}, NOT headless — anti-bot-safe; AXUSE_DISPLAY_SIZE to change — 'random' picks a stable geometry per machine so a fleet is not all 1920x1080; AXUSE_SERVER_MODE=0 to disable)`);
    }
    serverModeActive = vd.virtualDisplayStatus().active;
    const fonts = vd.fontHint(vd.fontProbe());
    if (fonts) out(`fonts: ${fonts}`);
    if (typeof process.getuid === "function" && process.getuid() === 0) {
      out("running as root: server-mode launches add --no-sandbox automatically (Chrome refuses root otherwise; AXUSE_NO_SANDBOX=1 forces it for any launch mode)");
    }
    if (/^(1|true)$/i.test(process.env.AXUSE_HEADLESS ?? "")) {
      out("AXUSE_HEADLESS=1 is set: EVERY launch runs --headless and challenge/anti-bot pages WILL detect it — unset it on servers (server mode keeps Chrome headed for free)");
    }
    // Persona coherence: the browser persona must match the egress persona
    // (timezone/locale mismatches vs the exit IP are classic scores).
    if (process.env.AXUSE_TIMEZONE) {
      const ok = vd.validTimezone(process.env.AXUSE_TIMEZONE);
      out(`AXUSE_TIMEZONE=${process.env.AXUSE_TIMEZONE}: ${ok ? "valid — Chrome runs with this TZ (Date/Intl match the egress persona)" : `INVALID (not an IANA zone Chrome accepts) — fix it or unset it; Intl rejects it`}`);
    } else {
      out(`persona: timezone not overridden (Chrome uses this host's TZ, currently ${new Intl.DateTimeFormat().resolvedOptions().timeZone}) — set AXUSE_TIMEZONE to the egress persona's zone when routing through a proxy`);
    }
    if (process.env.AXUSE_LANG) {
      const langLine = `AXUSE_LANG=${process.env.AXUSE_LANG}: drives --accept-lang (navigator.language(s) + Accept-Language) and LC_ALL (Intl default locale — measured: Chrome flags alone leave Intl at the OS locale)`;
      const m = /^([a-z]{2,3})[-_]([A-Za-z]{2})$/.exec(String(process.env.AXUSE_LANG).trim());
      if (m) {
        const tag = `${m[1]}_${m[2].toUpperCase()}`;
        const locales = (() => {
          try { return String(spawnSync("locale", ["-a"], { timeout: 4000 }).stdout ?? ""); } catch { return ""; }
        })();
        const generated = new RegExp(`${tag}\\.(utf8|UTF-8)`, "i").test(locales);
        out(`${langLine}`);
        out(`locale ${tag}.UTF-8: ${generated ? "generated — Intl resolves coherently" : `NOT generated — Intl silently falls back to the OS locale: sudo locale-gen ${tag}.UTF-8 && sudo update-locale, then restart the MCP server`}`);
      } else {
        out(`${langLine}`);
      }
    }
    if (process.env.AXUSE_FP_CORES || process.env.AXUSE_FP_MEMORY) {
      out(`persona overrides active: hardwareConcurrency=${process.env.AXUSE_FP_CORES ?? "(honest)"} · deviceMemory=${process.env.AXUSE_FP_MEMORY ?? "(honest)"} — a big server's honest cores are a persona mismatch for residential-looking traffic`);
    }
    const zombies = vd.sweepStaleXvfb({ kill: () => {} });
    if (zombies.length) {
      out(`stale Xvfb zombies: ${zombies.length} leaked axuse Xvfb process(es) (${zombies.map((z) => z.display).join(", ")}) — SIGKILLed harness processes; they are swept automatically at the next server-mode boot`);
    }
    const ambientOff = /^(0|off|false|no)$/i.test(String(process.env.AXUSE_AMBIENT_MOTION ?? "").trim());
    if (serverModeReady && !ambientOff) {
      out("ambient motion: on (trusted pointer park-fuzz between tool calls — AXUSE_AMBIENT_MOTION=0 to disable)");
    } else if (ambientOff) {
      out("ambient motion: disabled (AXUSE_AMBIENT_MOTION=0)");
    }
  }
  if (process.platform === "win32" || process.platform === "darwin") {
    if (process.platform === "darwin" && !/^(1|true)$/i.test(process.env.AXUSE_ENABLE_MACOS_CUA ?? "")) {
      out("desktop driver: disabled (set AXUSE_ENABLE_MACOS_CUA=1; Accessibility + Screen Recording grants are required for OS control)");
    } else {
      try {
        const d = await resolveDesktopDriver();
        if (requestTcc && process.platform === "darwin" && typeof d.requestPermissions === "function") {
          // The ONLY path that may open a TCC prompt — explicit user request.
          const r = await d.requestPermissions();
          out(`desktop tcc request: prompts opened (accessibility now: ${r.accessibility ? "granted" : "not granted yet"}; screen-recording prompt: ${r.screen_recording_prompt_opened ? "shown" : "not shown"})`);
          out(`  ${r.note}`);
        }
        const info = await d.screenInfo();
        const units = process.platform === "darwin" ? "macOS display points" : "physical px";
        out(`desktop driver: ok — active screen layout ${info.w}x${info.h} at (${info.x},${info.y}) (${units})`);
        if (process.platform === "darwin" && typeof d.permissions === "function") {
          const p = await d.permissions();
          out(`desktop permissions: Accessibility ${p.accessibility ? "granted" : "not detected"}; Screen Recording ${p.screen_recording === null ? "check on first screenshot" : p.screen_recording ? "granted" : "not detected"}`);
          try {
            await d.probe();
            out("desktop native events: CoreGraphics construction ok (no events posted)");
          } catch (e) {
            out(`desktop native events: unavailable — ${e.message}`);
          }
          if (p.screen_recording === false) {
            out("desktop screenshot: skipped — Screen Recording permission not detected");
          } else {
            try {
              await d.screenshot({ x: info.x, y: info.y, w: 1, h: 1, budgetBytes: 20_000 });
              out("desktop screenshot: ok");
            } catch (e) {
              out(`desktop screenshot: unavailable — ${e.message}`);
            }
          }
        }
        d.dispose();
      } catch (e) {
        out(`desktop driver: FAILED — ${e.message}`);
      }
    }
  } else if (process.platform === "linux") {
    if (serverModeActive || serverModeReady) {
      out(`desktop driver: mounts on the server-mode virtual display (${serverModeActive ? "booted" : "boots at the first browser launch"}; ax_desktop_* then speaks XTEST + EWMH over it)`);
    } else if (!process.env.DISPLAY) {
      out(`desktop driver: not available — DISPLAY is not set (X11/XWayland session required; browser tools still work${process.env.WAYLAND_DISPLAY ? "; this looks like a native Wayland session — XWayland usually provides DISPLAY like :0/:1" : ""})`);
    } else {
      try {
        const d = await resolveDesktopDriver();
        const info = await d.screenInfo();
        out(`desktop driver: ok (X11/XWayland, XTEST + EWMH) — root window ${info.w}x${info.h} at (${info.x},${info.y}) (pixels)`);
        try {
          const clip = await d.constructor.clipboardAvailable?.();
          out(`desktop clipboard paste: ${clip === undefined ? "unknown" : clip ? "available (xclip — long/untypeable text pastes via ctrl+v)" : "absent (type() synthesizes per key; sudo apt install xclip for the paste path)"}`);
        } catch {}
        try {
          await d.screenshot({ x: info.x, y: info.y, w: 1, h: 1, budgetBytes: 20_000 });
          out("desktop screenshot: ok");
        } catch (e) {
          out(`desktop screenshot: unavailable — ${e.message}`);
        }
        d.dispose();
      } catch (e) {
        out(`desktop driver: FAILED — ${e.message}`);
      }
    }
  } else {
    out("desktop driver: not implemented on this platform (browser tools still work)");
  }
  try {
    const { readInfo } = await import("./bridge/hub.js");
    const info = readInfo();
    if (info && info.port) {
      out(`bridge hub: info present → 127.0.0.1:${info.port} (pid ${info.pid}${(() => { try { process.kill(info.pid, 0); return ", alive"; } catch { return ", stale"; } })()})`);
    } else {
      out("bridge hub: not started (run `axuse setup` for daily Chrome, or use ax_open {backend:\"extension\"} after loading the bridge)");
    }
  } catch (e) {
    out(`bridge hub: ${e.message}`);
  }
  if (browser && hasWs) {
    // On a display-less Linux host with Xvfb, smoke-test the REAL production
    // lane: headed Chrome on an auto-booted virtual display (asserts the
    // anti-bot facts: no Headless UA, no webdriver flag, real viewport).
    // Everywhere else, the classic headless smoke (no display required).
    const vdMod = process.platform === "linux" ? await import("./core/vdisplay.js") : null;
    const useServerMode = !!vdMod && !vdMod.displayUsable() && vdMod.serverModeEnabled() && !!vdMod.xvfbBinary();
    if (useServerMode) {
      out("smoke test: server mode — booting a virtual display, launching HEADED Chrome…");
    } else {
      out("smoke test: launching headless browser, opening a page, observing…");
    }
    try {
      const { Controller } = await import("./core/controller.js");
      const c = new Controller();
      const html = `data:text/html,<button id="b" onclick="this.textContent='clicked'">go</button><input id="i" value="">`;
      const r = await c.execute("open", { url: html, ...(useServerMode ? {} : { headless: true }), settle_ms: 100 });
      const obs = r.observation;
      if (useServerMode) {
        const ua = JSON.parse((await c.execute("evaluate", { expression: "navigator.userAgent" })).value ?? '""');
        const fp = JSON.parse((await c.execute("evaluate", {
          expression: `(() => {
            const g = document.createElement('canvas').getContext('webgl');
            const dbg = g && g.getExtension('WEBGL_debug_renderer_info');
            return { webdriver: navigator.webdriver, w: innerWidth, h: innerHeight, webgl: !!g,
              renderer: dbg ? g.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
              tz: Intl.DateTimeFormat().resolvedOptions().timeZone, lang: navigator.language,
              cores: navigator.hardwareConcurrency, mem: navigator.deviceMemory };
          })()`,
        })).value ?? "{}");
        const antiBotOk = !/headless/i.test(String(ua)) && fp.webdriver === false && fp.w >= 1200;
        out(`server-mode facts: display ${r.display?.id} (virtual) · UA ${/headless/i.test(String(ua)) ? "CONTAINS Headless (BAD)" : "headed (no Headless substring)"} · navigator.webdriver ${fp.webdriver} · viewport ${fp.w}x${fp.h} · WebGL ${fp.webgl ? `present (${String(fp.renderer).slice(0, 40)})` : "absent"}`);
        out(`server-mode persona: tz ${fp.tz} · language ${fp.lang} · cores ${fp.cores} · deviceMemory ${fp.mem}${process.env.AXUSE_TIMEZONE ? " (AXUSE_TIMEZONE set)" : ""}`);
        out(`server-mode anti-bot posture: ${antiBotOk && fp.webgl ? "OK — indistinguishable headed build" : "CHECK the facts above"}`);
      }
      const click = await c.execute("click", { target: { ref: "e1" }, settle_ms: 100 });
      const ok = /clicked|changed/.test(click.observation?.text ?? "");
      out(`smoke test: ${ok ? "OK — observed tree, clicked ref e1, saw the diff" : "partial — observe worked, verify diff below"}`);
      out((click.observation?.text ?? obs.text).split("\n").slice(0, 8).map((l) => `  | ${l}`).join("\n"));
      await c.execute("close", { browser: true });
    } catch (e) {
      out(`smoke test: FAILED — ${e.message}`);
    }
  }
}

const [command = "mcp", ...rest] = process.argv.slice(2);
const arg = (name) => {
  const i = rest.indexOf(name);
  return i >= 0 ? rest[i + 1] : undefined;
};

async function bridgeCmd(sub) {
  const { readInfo } = await import("./bridge/hub.js");
  if (!sub) {
    const { runStandaloneHub } = await import("./bridge/hub.js");
    await runStandaloneHub();
    return;
  }
  if (sub === "install") {
    const { installBridge } = await import("./bridge/install.js");
    const browser = arg("--browser") ?? "chrome";
    const r = await installBridge({ browser });
    console.log(`native host: ${r.hostPath} (${r.via})`);
    console.log(`registered: ${r.registry}`);
    console.log(`extension id: ${r.extensionId} (allowed_extensions is pinned to it)`);
    console.log(`\nLoad the extension UNPACKED in ${browser} → chrome://extensions → Developer mode → Load unpacked:`);
    console.log(`  ${r.extensionDir}`);
    console.log(`\nThe service worker connects automatically whenever Chrome runs. Start the hub with:`);
    console.log(`  axuse bridge        (or just use ax_open {backend:"extension"} — the hub starts on demand)`);
    return;
  }
  if (sub === "uninstall") {
    const { uninstallBridge } = await import("./bridge/install.js");
    const browser = arg("--browser") ?? "chrome";
    const r = await uninstallBridge({ browser });
    console.log(r.removed.length ? `removed:\n  ${r.removed.join("\n  ")}` : "nothing to remove");
    return;
  }
  if (sub === "status") {
    const info = readInfo();
    if (!info || !info.port) {
      console.log("hub: not running (it starts on demand with the MCP server, or: axuse bridge)");
    } else {
      let alive = false;
      try { process.kill(info.pid, 0); alive = true; } catch {}
      console.log(`hub: ${alive ? `running (pid ${info.pid})` : `stale info (pid ${info.pid})`} → 127.0.0.1:${info.port}`);
    }
    return;
  }
  if (sub === "verify") {
    await verifyBridge();
    return;
  }
  console.log("usage: axuse bridge [install|uninstall|status|verify] [--browser chrome|edge|chromium|chrome-for-testing]");
}

async function installBrowserCmd() {
  const { installManagedBrowser, managedBinary, managedVersion, MANAGED_ROOT_DEFAULT } = await import("./core/managed-browser.js");
  const channel = (arg("--channel") ?? "Stable").replace(/^\w/, (c) => c.toUpperCase());
  const version = arg("--version");
  const t0 = Date.now();
  let lastProgress = 0;
  console.log(`resolving Chrome for Testing: ${version ? `version ${version} (pinned)` : `channel ${channel}`}…`);
  const r = await installManagedBrowser({
    channel,
    version,
    onProgress: (got, total) => {
      if (got - lastProgress > 20 * 1024 * 1024) {
        lastProgress = got;
        const mb = (got / 1024 / 1024).toFixed(0);
        const of = total ? ` of ${(total / 1024 / 1024).toFixed(0)} MB` : " MB";
        console.log(`  ${mb}${of}`);
      }
    },
  });
  if (r.alreadyInstalled) {
    console.log(`✓ already installed: ${managedVersion()} → ${managedBinary()}`);
  } else {
    console.log(`✓ installed ${r.version} (${r.files} files, ${(r.bytes / 1024 / 1024).toFixed(0)} MB) in ${((Date.now() - t0) / 1000).toFixed(1)}s`);
    console.log(`  binary: ${r.binary} (mode ${(r.mode ?? 0).toString(8)})`);
  }
  console.log(`axuse finds the managed build automatically; pin explicitly with AXUSE_BROWSER=${managedBinary()}`);
  console.log(`managed root: ${MANAGED_ROOT_DEFAULT()} (AXUSE_MANAGED_BROWSER_DIR to relocate — mount a cached volume there for fleets)`);
}

async function postureCmd() {
  const json = rest.includes("--json");
  const verifyEgress = rest.includes("--verify-egress");
  const { collectPosture, collectEgress, judgePosture, formatPosture } = await import("./workflows/posture.js");
  const c = new Controller();
  let report;
  try {
    const collected = await collectPosture(c);
    const egress = verifyEgress ? await collectEgress(c) : null;
    report = judgePosture({ ...collected, egress }, process.env);
    const browser = locateBrowser();
    let ver = "";
    try { ver = String(spawnSync(browser, ["--version"], { timeout: 15_000, encoding: "utf8" }).stdout ?? "").trim(); } catch {}
    console.log(formatPosture(report, { json, browser: ver ? `${browser} (${ver})` : browser, egress }));
    if (report.failures) process.exitCode = 1;
  } finally {
    await c.dispose();
  }
}

try {
  switch (command) {
    case "mcp":
      await runStdio();
      break;
    case "setup":
      await setupCmd();
      break;
    case "open-urls":
      await openUrlsCmd();
      break;
    case "bridge":
      await bridgeCmd(rest[0]);
      break;
    case "plugins":
      await pluginsCmd();
      break;
    case "install-browser":
      await installBrowserCmd();
      break;
    case "posture":
      await postureCmd();
      break;
    case "solve":
      await solveCmd();
      break;
    case "print-config": {
      const harness = arg("--harness") ?? arg("-h");
      printConfig(harness ?? "generic");
      break;
    }
    case "doctor":
      await doctor();
      break;
    case "help":
    case "--help":
    case "-h":
    default:
      usage();
  }
} catch (e) {
  console.error(`axuse: ${e.message ?? e}`);
  process.exitCode = 1;
}
