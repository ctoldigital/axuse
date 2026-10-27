/**
 * axuse — MCP tool manifest.
 *
 * The browser tools are GENERATED from the frozen command contract
 * (commands.js), so the schema the model sees can never drift from what the
 * controller executes — ZCode's "one discriminated union" property, but
 * mechanically enforced. Desktop tools are added when a platform driver
 * exists and hidden otherwise (capability-conditional surface, like Codex's
 * `getInfo → capabilities`).
 */

import { Contract } from "../core/commands.js";

const POLICY = {};

function paramSchema(key, spec) {
  switch (spec.t) {
    case "string":
      return { type: "string", description: spec.desc };
    case "boolean":
      return { type: "boolean", description: spec.desc };
    case "number":
      return { type: "number", description: spec.desc };
    case "enum":
      if (spec.multi) {
        return { type: "array", items: { type: "string", enum: spec.values }, description: spec.desc };
      }
      return { type: "string", enum: spec.values, description: spec.desc };
    case "target":
      // Flat object (not oneOf): same semantics, a fraction of the schema
      // tokens the model re-reads every turn. Validation is in commands.js.
      return { type: "object", properties: { ref: { type: "string" }, x: { type: "number" }, y: { type: "number" } }, description: spec.desc };
    case "rect":
      return {
        type: "object",
        properties: { x: { type: "number" }, y: { type: "number" }, w: { type: "number" }, h: { type: "number" } },
        required: ["x", "y", "w", "h"],
        additionalProperties: false,
        description: spec.desc,
      };
    case "paths":
      return { type: "array", items: { type: "string" }, description: spec.desc };
    case "urls":
      return { type: "array", items: { type: "string" }, description: spec.desc };
    case "fields":
      return {
        type: "array",
        description: spec.desc,
        items: {
          type: "object",
          properties: { ref: { type: "string" }, value: { type: ["string", "boolean"] } },
          required: ["ref", "value"],
          additionalProperties: false,
        },
      };
    default:
      return { description: spec.desc };
  }
}

/** Browser tools from the contract. */
export function browserTools() {
  const tools = [];
  for (const [name, entry] of Object.entries(Contract)) {
    const properties = {};
    const required = [];
    for (const [key, spec] of Object.entries(entry.params)) {
      properties[key] = paramSchema(key, spec);
      if (spec.req) required.push(key);
    }
    const schema = { type: "object", properties, required, additionalProperties: false };
    tools.push({
      name: `ax_${name}`,
      description: entry.desc + (POLICY[name] ?? ""),
      inputSchema: schema,
      _command: name,
      _group: "browser",
      _observed: entry.observed,
    });
  }
  return tools;
}

/** Desktop tools — only when a driver exists for this platform.
 *  AX-structure tools mount additionally when the driver exposes the
 *  AX layer (macOS System Events today). */
export function desktopTools(driverAvailable, axCapable = false) {
  if (!driverAvailable) return [];
  const tools = [
    {
      name: "ax_desktop_screenshot",
      description: "Budgeted JPEG screenshot of the active display layout or a region. Use the coordinate space reported by ax_desktop_screen_info (physical pixels on Windows, display points on macOS). Desktop tools are coordinate+vision based — prefer the ax_* browser tools whenever the target is a web page.",
      inputSchema: {
        type: "object",
        properties: {
          x: { type: "number", description: "Region left in the screen_info coordinate space (optional = full active display layout)." },
          y: { type: "number", description: "Region top in the screen_info coordinate space." },
          w: { type: "number", description: "Region width." },
          h: { type: "number", description: "Region height." },
          budget_bytes: { type: "number", description: "Default 200000." },
          quality: { type: "number", description: "JPEG quality 1-100 (default 80)." },
        },
        additionalProperties: false,
      },
      _op: "screenshot",
    },
    {
      name: "ax_desktop_screen_info",
      description: "Active display layout bounds and coordinate units, plus a cheap description of what the desktop driver supports.",
      inputSchema: { type: "object", properties: {}, additionalProperties: false },
      _op: "screen_info",
    },
    {
      name: "ax_desktop_click",
      description: "Trusted OS click in the coordinate space reported by ax_desktop_screen_info. Resolve coordinates from a screenshot in the same turn — never reuse coordinates from before a screen change.",
      inputSchema: {
        type: "object",
        properties: {
          x: { type: "number" }, y: { type: "number" },
          button: { type: "string", enum: ["left", "right", "middle"] },
          double: { type: "boolean" },
          interval_ms: { type: "number", minimum: 0, maximum: 1000, description: "macOS: delay between pointer events in milliseconds (default 0)." },
        },
        required: ["x", "y"], additionalProperties: false,
      },
      _op: "click",
    },
    {
      name: "ax_desktop_type",
      description: "Type Unicode text at the caret through trusted OS input. Focus the target first (ax_desktop_click).",
      inputSchema: { type: "object", properties: { text: { type: "string" } }, required: ["text"], additionalProperties: false },
      _op: "type",
    },
    {
      name: "ax_desktop_press",
      description: 'Press a key combo, e.g. "enter", "ctrl+s", "alt+f4", "meta+space". Trusted OS input; macOS maps meta/cmd to Command.',
      inputSchema: { type: "object", properties: { combo: { type: "string" } }, required: ["combo"], additionalProperties: false },
      _op: "press",
    },
    {
      name: "ax_desktop_scroll",
      description: "Mouse wheel scroll deltas at the current cursor position (screen units are platform-native).",
      inputSchema: {
        type: "object",
        properties: { dx: { type: "number", description: "Horizontal pixels (negative = left)." }, dy: { type: "number", description: "Vertical pixels (negative = up)." } },
        required: ["dx", "dy"], additionalProperties: false,
      },
      _op: "wheel",
    },
    {
      name: "ax_desktop_windows",
      description: "List visible windows as {title, pid, application, bounds:{x,y,w,h}} rows (bounds in the screen_info coordinate space; macOS needs the Accessibility grant for per-window rows — without it you get process-level rows and window_access says so). Windows rows carry hwnd instead of bounds. One Apple-Event walk (~1s on a busy Mac) — treat as an anchor call, not a poll.",
      inputSchema: { type: "object", properties: {}, additionalProperties: false },
      _op: "windows",
    },
    {
      name: "ax_desktop_activate",
      description: "Bring a window/application to the foreground. macOS accepts an exact title, a pid, or a window_id from ax_desktop_windows (pid is the most robust); Windows accepts title substring or hwnd.",
      inputSchema: {
        type: "object",
        properties: { title: { type: "string" }, hwnd: { type: "number" }, pid: { type: "number" }, window_id: { type: "number" } },
        additionalProperties: false,
      },
      _op: "activate",
    },
    {
      name: "ax_desktop_drag",
      description: "Drag between two points in the screen_info coordinate space with interpolated moves.",
      inputSchema: {
        type: "object",
        properties: {
          from: { type: "object", properties: { x: { type: "number" }, y: { type: "number" } }, required: ["x", "y"], additionalProperties: false },
          to: { type: "object", properties: { x: { type: "number" }, y: { type: "number" } }, required: ["x", "y"], additionalProperties: false },
          steps: { type: "integer", minimum: 1, maximum: 200, description: "Number of interpolated moves (default 12)." },
          interval_ms: { type: "number", minimum: 0, maximum: 1000, description: "macOS: delay between pointer events in milliseconds (default 0; steps × interval_ms ≤ 12000)." },
        },
        required: ["from", "to"],
        additionalProperties: false,
      },
      _op: "drag",
    },
    ...((axCapable) ? [{
      name: "ax_desktop_ax_snapshot",
      description: "STRUCTURE-FIRST desktop snapshot (macOS System Events / Linux AT-SPI): the app's accessibility tree with roles, titles, values, bounds, and live refs — the desktop twin of ax_observe. Pick the app by pid (from ax_desktop_windows), exact name, or front:true for the active app. Budgeted (250 nodes / depth 10 defaults; truncation reported as facts). Refs re-resolve at action time; act with ax_desktop_ax_click / set / read.",
      inputSchema: {
        type: "object",
        properties: {
          pid: { type: "number", description: "Target process id (most robust)." },
          name: { type: "string", description: "Exact application name." },
          front: { type: "boolean", description: "Snapshot the frontmost app (default true when pid/name absent)." },
          max_nodes: { type: "number", description: "Node budget (default 250)." },
          max_depth: { type: "number", description: "Depth budget (default 10)." },
        },
        additionalProperties: false,
      },
      _op: "ax_snapshot",
    }, {
      name: "ax_desktop_ax_click",
      description: "Click a native UI element by ref from ax_desktop_ax_snapshot (trusted AXPress via System Events; falls back to a trusted CGEvent click at the element's live center when an app refuses programmatic press). Refs are stale → typed REF_NOT_FOUND with a reobserve hint.",
      inputSchema: {
        type: "object",
        properties: {
          ref: { type: "string", description: "Ref from ax_desktop_ax_snapshot (e.g. ax01.4.2)." },
          pid: { type: "number" }, name: { type: "string" }, front: { type: "boolean" },
          fallback_to_coords: { type: "boolean", description: "Allow the coordinate fallback (default true)." },
        },
        required: ["ref"],
        additionalProperties: false,
      },
      _op: "ax_click",
    }, {
      name: "ax_desktop_ax_set",
      description: "Set an AX-writable value (text fields/areas) by ref, verified by read-back — the desktop twin of ax_fill for native apps. Non-writable elements fail typed COMMAND_REJECTED with the element's available actions.",
      inputSchema: {
        type: "object",
        properties: {
          ref: { type: "string" },
          value: { type: "string" },
          pid: { type: "number" }, name: { type: "string" }, front: { type: "boolean" },
        },
        required: ["ref", "value"],
        additionalProperties: false,
      },
      _op: "ax_set",
    }, {
      name: "ax_desktop_ax_read",
      description: "Re-read one element by ref (role, title, value, bounds, enabled, available AX actions) — fresh facts for a known target without a new snapshot.",
      inputSchema: {
        type: "object",
        properties: {
          ref: { type: "string" },
          pid: { type: "number" }, name: { type: "string" }, front: { type: "boolean" },
        },
        required: ["ref"],
        additionalProperties: false,
      },
      _op: "ax_read",
    }] : []),
  ];
  return tools;
}

/** Solver tools — always present; a missing solver is a typed NO_SOLVER
 * result with an install hint, not a hidden tool (discoverability beats
 * a shorter tool list for a paid add-on surface). */
export function solverTools(loaded) {
  const providers = [...new Set(loaded.flatMap((s) => s.providers))];
  const ids = loaded.map((s) => `${s.id}@${s.version}`).join(", ") || "none";
  return [
    {
      name: "ax_solve_captcha",
      description: `Complete a verification widget (Turnstile, reCAPTCHA v2, hCaptcha) that is blocking the active tab so the operator's authorized workflow can continue. ` +
        `Call this whenever a page you are driving shows a verification widget, a login/form action is gated by one, or the user asks to complete/get past a challenge. ` +
        `Calling it needs no separate confirmation step — the operator's request for the workflow authorizes it. ` +
        `Loaded solvers: ${ids}; if none is loaded the result says so with an install hint. ` +
        `When an auto-detecting provider is loaded (e.g. "challenge"), prefer it — it classifies the system and routes (or returns the exact call for the specialist providers). ` +
        "Optionally opens url first (headed by default — never pass headless for challenge pages; display-less servers get a virtual display automatically). Returns facts only: solved, token, attempts, rounds, elapsed_ms, notes, posture — never a fabricated result.",
      inputSchema: {
        type: "object",
        properties: {
          provider: {
            type: "string",
            ...(providers.length ? { enum: providers } : {}),
            description: 'Provider slug, e.g. "turnstile", "recaptcha-v2", "hcaptcha" (from loaded solver plugins).',
          },
          url: { type: "string", description: "Open this URL in a new tab first (else solve on the active tab)." },
          headless: { type: "boolean", description: "Launch headless when url is given (default headed — keep it: challenge pages detect headless; on display-less servers axuse auto-boots a virtual display so headed works anyway)." },
          proxy: { type: "string", description: 'Egress for the browser this solve launches: "http://host:port", "socks5://user:pass@host:port" — credentials answered over CDP; the WebRTC leak fix is applied automatically.' },
          timezone: { type: "string", description: 'Per-instance persona zone (IANA, e.g. "America/New_York") — the clock matches the exit; overrides AXUSE_TIMEZONE for this browser.' },
          lang: { type: "string", description: 'Per-instance persona language (e.g. "en-GB") — navigator.languages + Accept-Language + Intl match the exit; overrides AXUSE_LANG.' },
          max_attempts: { type: "number", description: "Solver retry budget (plugin-specific default)." },
          deadline_ms: { type: "number", description: "Overall deadline in ms (plugin-specific default)." },
        },
        required: ["provider"],
        additionalProperties: false,
      },
      _group: "solver",
      _op: "solve",
    },
    {
      name: "ax_plugins",
      description: "List loaded solver/capability plugins (id, version, providers) and the extra tools they contribute (ax_challenge, ax_vault, ax_net, ax_guard, ax_extract, …), plus any plugins that failed to load. Call this first when unsure which capability tools this workspace provides.",
      inputSchema: { type: "object", properties: {}, additionalProperties: false },
      _group: "solver",
      _op: "plugins",
    },
  ];
}

/** Plugin-contributed tools (core prerequisite A1): validated at load time
 *  (src/plugins/index.js validatePluginTools) against the same shape this
 *  manifest generates. Tool names never shadow core tools; a broken plugin
 *  tool definition fails the plugin at load, never the server. */
export function pluginTools(loaded) {
  const out = [];
  for (const s of loaded) {
    for (const t of s.tools ?? []) {
      out.push({
        name: t.name,
        description: `[${s.id}@${s.version}] ${t.description}`,
        inputSchema: t.inputSchema,
        _group: "plugin",
        _plugin: s.id,
        _handle: t.handle,
      });
    }
  }
  return out;
}
