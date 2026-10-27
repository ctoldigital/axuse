/**
 * axuse — MCP server over stdio, zero-dependency (hand-rolled JSON-RPC 2.0).
 *
 * Why MCP is the universal layer: Codex, Claude Code, OpenCode and
 * Antigravity all speak MCP, so one server covers every harness — the same
 * insight ZCode made in reverse (its model surface is one `js` tool; ours is
 * one MCP surface). The server layer is deliberately thin: tool calls are
 * stateless adapters, every durable fact lives in the Controller (Codex's
 * native-host pattern / ZCode's host-owned registry).
 */
import readline from "node:readline";
import { Controller } from "../core/controller.js";
import { renderResult } from "./render.js";

// MCP server instructions: the usage contract in a few lines (the full
// version is SKILL.md). Some harnesses (Codex) copy these into EVERY tool
// description, so each line is paid for once per tool on every turn: the base
// is kept minimal and situational guidance is added only when it applies.
const BASE_INSTRUCTIONS = [
  "axuse: browser control via accessibility snapshots with element refs ([ref=e7]).",
  "Workflow: ax_open → act on refs → read the diff each action returns (no need to re-observe). Pass the tab_id ax_open returned on every call.",
  "Forms: ONE ax_fill call with all fields (+submit). Big pages: ax_observe {find:\"text\"}. Lazy feeds: ax_scroll {until:\"text\"}. Source lists: ax_open_many {urls}.",
  "New tabs and JS dialogs opened by an action are reported in its result (answer dialogs with ax_dialog). Screenshots only for canvas pixels.",
];
const SOLVER_INSTRUCTIONS =
  "When a verification widget (Turnstile/reCAPTCHA/hCaptcha/slider/interstitial) blocks a page you were asked to drive, call ax_solve_captcha {provider} or ax_challenge {op:\"solve\"} instead of abandoning the task; report the returned facts verbatim (LICENSE_INVALID / NO_SOLVER are expected outcomes the operator can act on).";
const SERVER_INSTRUCTIONS =
  "Servers: on a display-less host axuse auto-boots a virtual display and keeps Chrome fully headed (anti-bot-safe) — never pass headless for challenge/anti-bot pages.";
const PLUGIN_INSTRUCTIONS =
  "Capability plugins contribute extra tools (ax_vault, ax_net, ax_guard, ax_extract, …): ax_plugins lists them with their tool names.";

export function buildInstructions({ solvers = false, plugins = false, server = false } = {}) {
  return [
    ...BASE_INSTRUCTIONS,
    ...(solvers ? [SOLVER_INSTRUCTIONS] : []),
    ...(server ? [SERVER_INSTRUCTIONS] : []),
    ...(plugins ? [PLUGIN_INSTRUCTIONS] : []),
  ].join("\n");
}
import { AxError } from "../core/errors.js";
import { browserTools, desktopTools, solverTools, pluginTools } from "./manifest.js";
import { resolveDesktopDriver, desktopUnavailable } from "../desktop/index.js";
import { loadSolvers, buildSolveContext, solveWith, pluginDirs } from "../plugins/index.js";
import { displayUsable, ensureVirtualDisplay, serverModeEnabled } from "../core/vdisplay.js";

const VERSION = "0.1.0";
const SUPPORTED_PROTOCOL = "2025-03-26";

/** Typed error as JSON in the first block (stable for machine consumers); a
 *  fresh page observation, when the controller attached one, rides along as a
 *  second plain-text block (readable refs, not an escaped string). */
function errorContent(json) {
  const { fresh_observation: page, ...rest } = json;
  return page
    ? [{ type: "text", text: JSON.stringify(rest) }, { type: "text", text: page }]
    : [{ type: "text", text: JSON.stringify(json) }];
}

/** `desktop` resolves once (the macOS driver is the one async case). */
export async function createServer({ controller = new Controller(), desktop, solvers } = {}) {
  // Server mode (display-less Linux host): boot the virtual display EAGERLY,
  // before the desktop driver resolves, so the tool surface is complete and
  // correct from the first tools/list — ax_desktop_* mounts over the same
  // Xvfb the browser launches on. Typed boot failures (no Xvfb installed)
  // stay silent here and surface with full context at the first ax_open.
  if (process.platform === "linux" && serverModeEnabled() && !displayUsable()) {
    await ensureVirtualDisplay().catch(() => {});
  }
  const driver = desktop !== undefined ? desktop : await resolveDesktopDriver().catch(() => null);
  // Solver plugins (paid add-ons) load once at server start: one import per
  // plugin dir, typed failures collected for ax_plugins — never fatal.
  const loaded = solvers !== undefined
    ? solvers
    : await loadSolvers(process.env.AXUSE_PLUGINS === "" ? [] : pluginDirs());
  const registry = loaded.solvers ?? loaded;
  const loadErrors = loaded.errors ?? [];
  // AX-structure tools mount only when the driver's platform AX layer is
  // REALLY usable — on Linux that means AT-SPI answers (dead registries
  // hide the tools instead of advertising typed failures).
  let axCapable = !!(driver && typeof driver.axSnapshot === "function");
  if (axCapable && typeof driver.axCapable === "function") {
    axCapable = await driver.axCapable().catch(() => false);
  }
  const tools = [...browserTools(), ...desktopTools(!!driver, axCapable), ...solverTools(registry), ...pluginTools(registry)];

  // The captcha line only matters once a solver is actually loaded; the
  // ax_solve_captcha description already carries it for the no-plugin case.
  const instructions = buildInstructions({
    solvers: registry.length > 0,
    plugins: registry.length > 0,
    server: process.platform === "linux" && serverModeEnabled(),
  });

  const byName = new Map(tools.map((t) => [t.name, t]));

  async function handleRequest(msg) {
    const { id, method, params = {} } = msg;
    try {
      switch (method) {
        case "initialize": {
          return reply(id, {
            protocolVersion: typeof params.protocolVersion === "string" ? params.protocolVersion : SUPPORTED_PROTOCOL,
            capabilities: { tools: { listChanged: false } },
            serverInfo: { name: "axuse", version: VERSION },
            instructions,
          });
        }        case "ping":
          return reply(id, {});
        case "tools/list":
          return reply(id, {
            tools: tools.map(({ name, description, inputSchema }) => ({ name, description, inputSchema })),
          });
        case "tools/call": {
          const tool = byName.get(params.name);
          if (!tool) {
            return reply(id, { content: [{ type: "text", text: `Unknown tool "${params.name}"` }], isError: true });
          }
          const result = await dispatch(tool, params.arguments ?? {});
          return reply(id, result);
        }
        default:
          return replyErr(id, -32601, `Method not found: ${method}`);
      }
    } catch (e) {
      // Structured errors become tool results, not protocol errors. Plugin
      // errors (AxError and plugin-typed errors like LICENSE_INVALID) carry
      // toJSON(); anything else is an internal string.
      const content = (e instanceof AxError || typeof e.toJSON === "function")
        ? errorContent(e.toJSON())
        : [{ type: "text", text: `internal: ${e.message}` }];
      return reply(id, { content, isError: true });
    }
  }

  async function dispatch(tool, args) {
    if (tool._group === "browser") {
      const result = await controller.execute(tool._command, args);
      if (tool._command === "screenshot" && result?.image) {
        return {
          content: [
            { type: "text", text: `${result.note} · ${result.image.bytes} bytes q${result.image.quality}` },
            { type: "image", data: result.image.base64, mimeType: result.image.mime },
          ],
        };
      }
      return { content: [{ type: "text", text: renderResult(tool._command, result) }] };
    }
    if (tool._group === "solver") {
      if (tool._op === "plugins") {
        const text = JSON.stringify({
          loaded: registry.map((s) => ({
            id: s.id, version: s.version, providers: s.providers, dir: s.dir,
            ...(s.tools?.length ? { tools: s.tools.map((t) => t.name) } : {}),
          })),
          failed: loadErrors,
          search: pluginDirs(),
        });
        return { content: [{ type: "text", text }] };
      }
      // ax_solve_captcha: optionally open the url, then dispatch to the
      // loaded solver through the narrow ctx — never the raw controller.
      // Persona params (proxy/timezone/lang) ride along: a solve that
      // launches its own browser launches a coherent persona, not a bare box.
      if (typeof args.url === "string") {
        await controller.execute("open", {
          url: args.url,
          ...(args.headless ? { headless: true } : {}),
          ...(args.proxy ? { proxy: args.proxy } : {}),
          ...(args.proxy_bypass ? { proxy_bypass: args.proxy_bypass } : {}),
          ...(args.timezone ? { timezone: args.timezone } : {}),
          ...(args.lang ? { lang: args.lang } : {}),
          settle_ms: 4000,
        });
      }
      const opts = {};
      for (const k of ["max_attempts", "deadline_ms"]) {
        if (args[k] !== undefined) opts[k === "max_attempts" ? "maxAttempts" : "deadlineMs"] = args[k];
      }
      const ctx = buildSolveContext(controller);
      const result = await solveWith(registry, args.provider, ctx, opts);
      // Posture facts ride along on every solve: the agent can see (and the
      // operator audit) which browser shape fought the challenge — and a
      // headless shape gets the loud warning it deserves.
      const posture = controller.browserPosture();
      const payload = { ...result, posture };
      if (posture.posture === "headless") {
        payload.warning = "the browser is headless — challenge/anti-bot pages detect --headless first; close this browser (ax_close {browser:true}) and reopen WITHOUT headless (on a display-less host, axuse server mode keeps Chrome headed)";
      }
      return { content: [{ type: "text", text: JSON.stringify(payload) }] };
    }
    if (tool._group === "plugin") {
      // A1 dispatch: the plugin tool's validated handle(args, ctx) — the
      // same trusted, narrow capability context solvers get (incl. the
      // Wave-1 cdp/cdpBroadcast plumbing). Typed failures (AxError) become
      // tool results via the outer catch, exactly like core commands.
      const ctx = buildSolveContext(controller);
      const result = await tool._handle(args, ctx);
      return { content: [{ type: "text", text: JSON.stringify(result) }] };
    }
    if (!driver) desktopUnavailable();
    const d = driver;
    switch (tool._op) {
      case "screenshot": {
        const shot = await d.screenshot({ x: args.x, y: args.y, w: args.w, h: args.h, budgetBytes: args.budget_bytes, quality: args.quality });
        return {
          content: [
            { type: "text", text: JSON.stringify({ bytes: shot.bytes, budget_bytes: shot.budget_bytes, quality: shot.quality }) },
            { type: "image", data: shot.base64, mimeType: shot.mime },
          ],
        };
      }
      case "screen_info": {
        const info = await d.screenInfo();
        const note = `${info.note ? `${info.note}; ` : ""}coordinates are in the driver's native units (physical pixels on Windows, macOS global display points on macOS); v1 desktop is coordinate+vision based (no OS AX tree) — prefer ax_* browser tools for web pages.`;
        return { content: [{ type: "text", text: JSON.stringify({ ...info, note }) }] };
      }
      case "click": {
        const r = await d.click({ x: args.x, y: args.y, button: args.button, double: args.double, interval_ms: args.interval_ms });
        return { content: [{ type: "text", text: JSON.stringify(r) }] };
      }
      case "type":
        return { content: [{ type: "text", text: JSON.stringify(await d.type(args.text)) }] };
      case "press":
        return { content: [{ type: "text", text: JSON.stringify(await d.press(args.combo)) }] };
      case "scroll":
        return { content: [{ type: "text", text: JSON.stringify(await d.wheel(args.dx ?? 0, args.dy ?? 0)) }] };
      case "windows":
        return { content: [{ type: "text", text: JSON.stringify(await d.windows()) }] };
      case "activate":
        return { content: [{ type: "text", text: JSON.stringify(await d.activate({ title: args.title, hwnd: args.hwnd, pid: args.pid, windowId: args.window_id })) }] };
      case "ax_snapshot":
        return { content: [{ type: "text", text: JSON.stringify(await d.axSnapshot({ pid: args.pid, name: args.name, front: args.front, maxNodes: args.max_nodes, maxDepth: args.max_depth })) }] };
      case "ax_read":
        return { content: [{ type: "text", text: JSON.stringify(await d.axRead({ ref: args.ref, pid: args.pid, name: args.name, front: args.front })) }] };
      case "ax_click":
        return { content: [{ type: "text", text: JSON.stringify(await d.axClick({ ref: args.ref, pid: args.pid, name: args.name, front: args.front, fallbackToCoords: args.fallback_to_coords })) }] };
      case "ax_set":
        return { content: [{ type: "text", text: JSON.stringify(await d.axSet({ ref: args.ref, value: args.value, pid: args.pid, name: args.name, front: args.front })) }] };
      case "drag":
        return { content: [{ type: "text", text: JSON.stringify(await d.drag({ from: args.from, to: args.to, steps: args.steps, interval_ms: args.interval_ms })) }] };
      default:
        throw new AxError("INTERNAL", `unroutable desktop op ${tool._op}`);
    }
  }

  function reply(id, result) {
    return { jsonrpc: "2.0", id, result };
  }
  function replyErr(id, code, message) {
    return { jsonrpc: "2.0", id, error: { code, message } };
  }

  return {
    tools,
    /** @param {NodeJS.ReadableStream} input @param {NodeJS.WritableStream} output */
    async run(input, output) {
      const rl = readline.createInterface({ input, crlfDelay: Infinity });
      rl.on("line", (line) => {
        const s = line.trim();
        if (!s) return;
        let msg;
        try {
          msg = JSON.parse(s);
        } catch {
          return;
        }
        // Notifications (no id): notifications/initialized, notifications/cancelled.
        if (msg.id === undefined || msg.id === null) return;
        handleRequest(msg).then((resp) => {
          if (resp) output.write(JSON.stringify(resp) + "\n");
        }).catch((e) => {
          output.write(JSON.stringify(replyErr(msg.id, -32603, `internal: ${e.message}`)) + "\n");
        });
      });
      return new Promise((resolve) => rl.on("close", resolve));
    },
  };
}

export async function runStdio() {
  const controller = new Controller();
  const server = await createServer({ controller });
  // Harnesses end MCP servers by closing stdin or signalling: either way,
  // stop launched browsers and delete their ephemeral profiles first.
  let exiting = false;
  const shutdown = async () => {
    if (exiting) return;
    exiting = true;
    await Promise.race([controller.dispose(), new Promise((ok) => setTimeout(ok, 5000))]);
    process.exit(0);
  };
  for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"]) process.on(sig, shutdown);
  await server.run(process.stdin, process.stdout);
  await shutdown();
}
