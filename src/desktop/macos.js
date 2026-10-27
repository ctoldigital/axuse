/**
 * axuse — macOS desktop driver.
 *
 * macOS deliberately keeps the driver behind AXUSE_ENABLE_MACOS_CUA=1. OS
 * input and screen capture are permissioned capabilities, so the driver must
 * fail with a useful, typed fact when the host has not been granted
 * Accessibility or Screen Recording access. The implementation itself is
 * dependency-free: JXA bridges CoreGraphics/System Events and the native
 * `screencapture` + `sips` tools handle pixels.
 *
 * Coordinate contract:
 *   - x/y/w/h are macOS global display points (the same units used by
 *     CGDisplayBounds and CGEvent mouse events).
 *   - returned JPEGs are resized to the requested point dimensions, so a
 *     pixel read from the image maps directly to a click coordinate even on
 *     a Retina display.
 */
import { execFile } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { AxError, Codes } from "../core/errors.js";
import { DEFAULT_BUDGET_BYTES } from "../core/screenshot.js";

const COMMAND_TIMEOUT_MS = 15_000;
/** Probe results are cached briefly: a screenshot used to cost four process
 *  spawns (screen_info + permissions + screencapture + sips); caching the two
 *  JXA probes halves it. Every DESKTOP_PERMISSION_REQUIRED failure
 *  invalidates both caches, so a just-granted permission is picked up on the
 *  next call without restarting the host. */
const PROBE_CACHE_TTL_MS = 5_000;
const PERMISSION_HINT =
  "Grant the host running axuse Accessibility and Screen Recording in System Settings → Privacy & Security, then retry. Keep AXUSE_ENABLE_MACOS_CUA=1 enabled for desktop control.";
const STALE_PERMISSION_HINT =
  "The TCC grant may be stale (binary moved/updated, or the grant predates this host process). Quit and restart the host running axuse; if it persists, run `tccutil reset ScreenCapture` (and `tccutil reset Accessibility`) from Terminal, then re-grant in System Settings → Privacy & Security.";

function appleScriptLiteral(value) {
  return `"${String(value).replaceAll("\\", "\\\\").replaceAll('"', '\\"')}"`;
}

/** Run a native macOS command without invoking a shell. */
function run(file, args, { timeout = COMMAND_TIMEOUT_MS, hint = PERMISSION_HINT } = {}) {
  return new Promise((resolve, reject) => {
    execFile(file, args, { timeout, maxBuffer: 64 * 1024 * 1024 }, (err, stdout, stderr) => {
      if (err) {
        const detail = String(stderr || err.message || `${file} failed`).trim().slice(0, 600);
        const permission = /not allowed assistive access|not allowed to send|not authorized|could not create image from (rect|display)|screen recording|accessibility|kCGErrorFailure|operation not permitted/i.test(detail);
        // A capture that fails after a *passing* preflight is the classic
        // stale-grant signature (TCC still trusts an old binary path).
        const stale = permission && /could not create image from (rect|display)|operation not permitted/i.test(detail);
        reject(new AxError(permission ? Codes.DESKTOP_PERMISSION_REQUIRED : Codes.DESKTOP_OP_FAILED,
          `${file} failed: ${detail}`,
          { hint: stale ? STALE_PERMISSION_HINT : hint, retry: "retry" }));
      } else {
        resolve(String(stdout));
      }
    });
  });
}

function parseJson(out, operation) {
  try {
    return JSON.parse(String(out).trim());
  } catch {
    throw new AxError(Codes.DESKTOP_OP_FAILED,
      `macOS ${operation} returned invalid JSON`,
      { hint: "Retry the operation; if it persists, check the host's osascript installation and permissions.", retry: "retry" });
  }
}

function finite(value, name) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new AxError(Codes.BAD_COORDS, `${name} must be a finite number`, { retry: "never" });
  }
  return value;
}

function pointerInterval(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 1000) {
    throw new AxError(Codes.COMMAND_REJECTED, "interval_ms must be between 0 and 1000", { retry: "never" });
  }
  return value;
}

function positive(value, name) {
  const n = finite(value, name);
  if (n < 1) throw new AxError(Codes.BAD_COORDS, `${name} must be ≥ 1`, { retry: "never" });
  return n;
}

function rectValues(rect, info) {
  const supplied = rect && Object.values(rect).some((v) => v !== undefined);
  if (supplied && (!rect || ["x", "y", "w", "h"].some((k) => rect[k] === undefined))) {
    throw new AxError(Codes.BAD_COORDS, "Desktop screenshot requires x, y, w, and h together", { retry: "never" });
  }
  const source = supplied ? rect : info;
  const x = Math.round(finite(source.x, "x"));
  const y = Math.round(finite(source.y, "y"));
  const w = Math.round(positive(source.w, "w"));
  const h = Math.round(positive(source.h, "h"));
  if (w < 1 || h < 1) throw new AxError(Codes.BAD_COORDS, "Screenshot width and height must round to at least 1", { retry: "never" });
  // Refusing an out-of-bounds rectangle is safer than asking screencapture to
  // silently clamp it. The bounds are the union of active displays.
  if (x < info.x || y < info.y || x + w > info.x + info.w || y + h > info.y + info.h) {
    throw new AxError(Codes.BAD_COORDS,
      `Screenshot rect (${x},${y},${w},${h}) is outside the active display layout (${info.x},${info.y},${info.w},${info.h})`,
      { hint: "Call ax_desktop_screen_info and keep the crop within the reported display bounds.", retry: "never" });
  }
  return { x, y, w, h };
}

function buttonSpec(button) {
  const b = String(button ?? "left").toLowerCase();
  if (!new Set(["left", "right", "middle"]).has(b)) {
    throw new AxError(Codes.COMMAND_REJECTED, `Unknown mouse button "${button}"`, { retry: "never" });
  }
  return b;
}

/** Exact payload size of a base64 string (padding-aware). */
function exactBase64Bytes(b64) {
  return Buffer.byteLength(Buffer.from(b64, "base64"));
}

/** AppleScript key codes for a US-independent physical key layout. */
export const MACOS_KEY_CODES = Object.freeze({
  enter: 36, return: 36, tab: 48, escape: 53, esc: 53,
  backspace: 51, delete: 117, del: 117, space: 49,
  up: 126, down: 125, left: 123, right: 124,
  home: 115, end: 119, pageup: 116, pagedown: 121,
  f1: 122, f2: 120, f3: 99, f4: 118, f5: 96, f6: 97,
  f7: 98, f8: 100, f9: 101, f10: 109, f11: 103, f12: 111,
  f13: 105, f14: 107, f15: 113, f16: 106, f17: 64, f18: 79, f19: 80, f20: 90,
  help: 114, insert: 114, forwarddelete: 117,
});

const MODIFIERS = Object.freeze({
  ctrl: "control down", control: "control down",
  alt: "option down", option: "option down",
  shift: "shift down",
  meta: "command down", cmd: "command down", command: "command down",
});

/** Parse a desktop key combo once, with the same aliases on every macOS call. */
export function parseMacCombo(combo) {
  const parts = String(combo ?? "").toLowerCase().split("+").map((s) => s.trim()).filter(Boolean);
  const mods = [];
  let main = null;
  for (const part of parts) {
    if (MODIFIERS[part]) mods.push(MODIFIERS[part]);
    else if (main === null) main = part;
    else throw new AxError(Codes.COMMAND_REJECTED, `More than one non-modifier key in combo "${combo}"`, { retry: "never" });
  }
  if (!main) throw new AxError(Codes.COMMAND_REJECTED, `No non-modifier key in combo "${combo}"`, { retry: "never" });
  return { mods, main, keyCode: MACOS_KEY_CODES[main] };
}

/** CGEvent-based trusted mouse synthesis (JXA ObjC bridge). */
const MOUSE_EVENTS = Object.freeze({
  down: "$.kCGEventLeftMouseDown",
  up: "$.kCGEventLeftMouseUp",
  moved: "$.kCGEventMouseMoved",
  rdown: "$.kCGEventRightMouseDown",
  rup: "$.kCGEventRightMouseUp",
  mdown: "$.kCGEventOtherMouseDown",
  mup: "$.kCGEventOtherMouseUp",
  drag: "$.kCGEventLeftMouseDragged",
});

const MOUSE_BUTTONS = Object.freeze({
  left: "$.kCGMouseButtonLeft",
  right: "$.kCGMouseButtonRight",
  center: "$.kCGMouseButtonCenter",
});

function mouseButtonFor(kind) {
  return kind === "rdown" || kind === "rup" ? MOUSE_BUTTONS.right
    : kind === "mdown" || kind === "mup" ? MOUSE_BUTTONS.center
      : MOUSE_BUTTONS.left;
}

function nativeEventCode(events, { post = true, intervalMs = 0 } = {}) {
  const body = events.map((item, i) => {
    if (item.kind === "scroll") {
      const horizontal = item.dx !== 0;
      const wheelCount = horizontal ? 2 : 1;
      const wheelArgs = horizontal ? `${-item.dy}, ${-item.dx}` : `${-item.dy}`;
      return `
        var e${i} = $.CGEventCreateScrollWheelEvent(null, $.kCGScrollEventUnitPixel, ${wheelCount}, ${wheelArgs});
        if (!e${i}) throw new Error('CGEventCreateScrollWheelEvent returned null');
        ${post ? `$.CGEventPost($.kCGHIDEventTap, e${i});` : ""}`;
    }
    const event = MOUSE_EVENTS[item.kind];
    if (!event) throw new AxError(Codes.DESKTOP_OP_FAILED, `Unknown macOS mouse event "${item.kind}"`, { retry: "never" });
    const button = mouseButtonFor(item.kind);
    // kCGMouseEventClickState = 1 (CGEventField, CGEventTypes.h). Cocoa reads
    // the click state from the event, not from inter-event timing alone: two
    // rapid clicks without clickState=2 register as two single clicks.
    const clickState = item.clickState ? `
      $.CGEventSetIntegerValueField(e${i}, 1, ${item.clickState});` : "";
    return `
      var e${i} = $.CGEventCreateMouseEvent(null, ${event}, $.CGPointMake(${item.x}, ${item.y}), ${button});
      if (!e${i}) throw new Error('CGEventCreateMouseEvent returned null');${clickState}
      ${post ? `$.CGEventPost($.kCGHIDEventTap, e${i});` : ""}`;
  }).join(intervalMs ? `\ndelay(${intervalMs / 1000});\n` : "\n");
  return `
    ${post ? "ObjC.import('ApplicationServices'); if (!$.AXIsProcessTrusted()) throw new Error('Accessibility permission required');" : ""}
    ObjC.import('CoreGraphics');
    (function () {
      ${body}
      return "ok";
    })()`;
}

function mouseCode(kind, x, y, options) {
  return nativeEventCode([{ kind, x, y }], options);
}

/*
 * Keep the public operations cheap on macOS: spawning osascript once per
 * mouse event made a drag take dozens of process launches and introduced
 * avoidable gaps between down/move/up. The event builder remains injectable
 * through jxa(), while normal actions post an ordered native sequence.
 */
function mouseSequenceCode(events, options) {
  return nativeEventCode(events, options);
}

const SCREEN_INFO_SCRIPT = `
  ObjC.import('CoreGraphics');
  ObjC.import('AppKit');
  (function () {
    var main = Number($.CGMainDisplayID()), screens = $.NSScreen.screens, displays = [];
    for (var i = 0; i < screens.count; i++) {
      var screen = screens.objectAtIndex(i), number = null;
      try { number = screen.deviceDescription.objectForKey('NSScreenNumber').intValue; } catch (_) {}
      var id = Number(number);
      if (!Number.isFinite(id)) continue;
      var b = $.CGDisplayBounds(id);
      var width = Number(b.size.width), height = Number(b.size.height);
      // CGDisplayPixelsWide can report the logical mode on scaled desktop
      // sessions. AppKit's backingScaleFactor is the authoritative mapping
      // between display points (input) and capture pixels (raster).
      var scale = Number(screen.backingScaleFactor) || 1;
      displays.push({
        id: id, x: Number(b.origin.x), y: Number(b.origin.y), w: width, h: height,
        scale_x: scale, scale_y: scale, pixels_w: Math.round(width * scale), pixels_h: Math.round(height * scale)
      });
    }
    if (!displays.length) {
      var fallback = $.CGDisplayBounds(main);
      displays.push({ id: main, x: Number(fallback.origin.x), y: Number(fallback.origin.y), w: Number(fallback.size.width), h: Number(fallback.size.height), scale_x: 1, scale_y: 1, pixels_w: Number(fallback.size.width), pixels_h: Number(fallback.size.height) });
    }
    var primary = displays[0];
    for (var j = 0; j < displays.length; j++) if (displays[j].id === main) primary = displays[j];
    var left = displays[0].x, top = displays[0].y, right = displays[0].x + displays[0].w, bottom = displays[0].y + displays[0].h;
    for (var k = 1; k < displays.length; k++) {
      left = Math.min(left, displays[k].x); top = Math.min(top, displays[k].y);
      right = Math.max(right, displays[k].x + displays[k].w); bottom = Math.max(bottom, displays[k].y + displays[k].h);
    }
    return JSON.stringify({
      display_id: primary.id, x: left, y: top, w: right - left, h: bottom - top,
      pixels_w: primary.pixels_w, pixels_h: primary.pixels_h,
      scale_x: primary.scale_x, scale_y: primary.scale_y, display_count: displays.length, displays: displays
    });
  })()`;

const PERMISSIONS_SCRIPT = `
  ObjC.import('ApplicationServices');
  ObjC.import('CoreGraphics');
  (function () {
    var accessibility = false;
    var screenRecording = null;
    try { accessibility = Boolean($.AXIsProcessTrusted()); } catch (_) {}
    try {
      // JXA does not expose this C symbol until it is explicitly bound. The
      // preflight call is non-interactive: it never opens the TCC prompt.
      ObjC.bindFunction('CGPreflightScreenCaptureAccess', ['bool', []]);
      screenRecording = Boolean($.CGPreflightScreenCaptureAccess());
    } catch (_) {}
    return JSON.stringify({ accessibility: accessibility, screen_recording: screenRecording });
  })()`;

/* -------------------------------------------------------- AX (System Events)
 *
 * Structure-first desktop automation on macOS: the same Accessibility grant
 * already required for input lets System Events read the platform AX tree.
 * One Apple Event per node (`properties`), path-based refs (re-resolved
 * live at action time, browser-ref discipline), and hard budgets so huge
 * trees (Xcode, browsers) degrade to honest truncation facts.
 */

/** Locate a System Events process by pid / name-or-window-title / frontmost
 *  and dump a budgeted AX subtree as flat JSON nodes. */
const AX_SNAPSHOT_SCRIPT = `
  (function () {
    var se = Application('System Events');
    var wantPid = __WANT_PID__, wantName = __WANT_NAME__, wantFront = __WANT_FRONT__;
    var maxNodes = __MAX_NODES__, maxDepth = __MAX_DEPTH__;
    var procs = se.processes.whose({ backgroundOnly: false })();
    var p = null;
    for (var i = 0; i < procs.length; i++) {
      var q = procs[i], pid = 0, name = '', front = false;
      try { pid = Number(q.unixId()); } catch (_) {}
      try { name = String(q.name()); } catch (_) {}
      try { front = Boolean(q.frontmost()); } catch (_) {}
      if (wantPid ? pid === wantPid : (String(wantName) ? (name === wantName || name.toLowerCase() === String(wantName).toLowerCase()) : front)) { p = q; break; }
    }
    if (!p) return JSON.stringify({ error: 'no_process', want: wantPid || wantName || 'frontmost' });
    var nodes = [], truncatedChildren = 0, hitDepth = 0;
    function describe(el, path, parent, depth) {
      if (nodes.length >= maxNodes || depth > maxDepth) { if (depth > hitDepth) hitDepth = depth; return null; }
      var pr = {};
      try { pr = el.properties(); } catch (e) { return null; }
      var row = { path: path, p: parent, d: depth };
      try { row.role = String(pr.role || el.role() || ''); } catch (_) {}
      try { row.subrole = pr.subrole ? String(pr.subrole) : null; } catch (_) {}
      try { row.title = pr.title != null ? String(pr.title).slice(0, 120) : null; } catch (_) {}
      try { if (row.title == null && pr.description != null) row.title = String(pr.description).slice(0, 120); } catch (_) {}
      try { row.value = pr.value != null ? String(pr.value).slice(0, 200) : null; } catch (_) {}
      try { var pos = pr.position || el.position(); row.x = Number(pos[0]); row.y = Number(pos[1]); } catch (_) {}
      try { var sz = pr.size || el.size(); row.w = Number(sz[0]); row.h = Number(sz[1]); } catch (_) {}
      try { row.enabled = pr.enabled === undefined ? null : Boolean(pr.enabled); } catch (_) {}
      var idx = nodes.length;
      nodes.push(row);
      var kids = [];
      try { kids = el.uiElements(); } catch (_) {}
      var shown = 0;
      for (var k = 0; k < kids.length && nodes.length < maxNodes; k++) {
        if (shown >= 60) { truncatedChildren += (kids.length - shown); break; }
        var child = describe(kids[k], path + '/' + k, idx, depth + 1);
        shown++;
        if (child === null && depth + 1 > maxDepth) break;
      }
      return idx;
    }
    var wins = [];
    try { wins = p.windows(); } catch (e) { return JSON.stringify({ error: 'no_windows_access', app: String(p.name()), hint: 'Accessibility grant missing for window access' }); }
    for (var w = 0; w < wins.length && nodes.length < maxNodes; w++) {
      describe(wins[w], String(w), null, 0);
    }
    return JSON.stringify({
      app: String(p.name()), pid: Number(p.unixId()), nodes: nodes,
      truncated_children: truncatedChildren, hit_max_nodes: nodes.length >= maxNodes,
      hit_max_depth: hitDepth > maxDepth, window_count: wins.length,
    });
  })()`;

/** Re-resolve a ref path against the live tree and return its properties
 *  (plus available AX actions for actuation decisions). */
const AX_RESOLVE_SCRIPT = `
  (function () {
    var se = Application('System Events');
    var wantPid = __WANT_PID__, wantName = __WANT_NAME__, wantFront = __WANT_FRONT__;
    var path = String(__PATH__).split('/');
    var procs = se.processes.whose({ backgroundOnly: false })();
    var p = null;
    for (var i = 0; i < procs.length; i++) {
      var q = procs[i], pid = 0, name = '', front = false;
      try { pid = Number(q.unixId()); } catch (_) {}
      try { name = String(q.name()); } catch (_) {}
      try { front = Boolean(q.frontmost()); } catch (_) {}
      if (wantPid ? pid === wantPid : (String(wantName) ? (name === wantName || name.toLowerCase() === String(wantName).toLowerCase()) : front)) { p = q; break; }
    }
    if (!p) return JSON.stringify({ error: 'no_process' });
    var el = null;
    try {
      el = p.windows()[Number(path[0])];
      for (var j = 1; j < path.length && el; j++) el = el.uiElements()[Number(path[j])];
    } catch (e) { el = null; }
    if (!el) return JSON.stringify({ error: 'stale_path', path: path.join('/') });
    var out = {};
    try { var pr = el.properties(); out.role = String(pr.role || ''); out.title = pr.title != null ? String(pr.title) : null;
      out.value = pr.value != null ? String(pr.value) : null; out.subrole = pr.subrole ? String(pr.subrole) : null;
      out.enabled = pr.enabled === undefined ? null : Boolean(pr.enabled);
      var pos = pr.position; if (pos) { out.x = Number(pos[0]); out.y = Number(pos[1]); }
      var sz = pr.size; if (sz) { out.w = Number(sz[0]); out.h = Number(sz[1]); }
    } catch (e) {}
    var acts = [];
    try { var as = el.actions(); for (var a = 0; a < as.length; a++) { try { acts.push(String(as[a].name())); } catch (_) {} } } catch (_) {}
    out.actions = acts;
    return JSON.stringify({ app: String(p.name()), pid: Number(p.unixId()), element: out });
  })()`;

/** Act on a resolved element: AXPress via System Events' click (with an
 *  optional CGEvent coordinate fallback handled driver-side). */
const AX_PRESS_SCRIPT = `
  (function () {
    var se = Application('System Events');
    var wantPid = __WANT_PID__, wantName = __WANT_NAME__, wantFront = __WANT_FRONT__;
    var path = String(__PATH__).split('/');
    var procs = se.processes.whose({ backgroundOnly: false })();
    var p = null;
    for (var i = 0; i < procs.length; i++) {
      var q = procs[i], pid = 0, name = '', front = false;
      try { pid = Number(q.unixId()); } catch (_) {}
      try { name = String(q.name()); } catch (_) {}
      try { front = Boolean(q.frontmost()); } catch (_) {}
      if (wantPid ? pid === wantPid : (String(wantName) ? (name === wantName || name.toLowerCase() === String(wantName).toLowerCase()) : front)) { p = q; break; }
    }
    if (!p) return JSON.stringify({ error: 'no_process' });
    var el = null;
    try {
      el = p.windows()[Number(path[0])];
      for (var j = 1; j < path.length && el; j++) el = el.uiElements()[Number(path[j])];
    } catch (e) { el = null; }
    if (!el) return JSON.stringify({ error: 'stale_path', path: path.join('/') });
    var center = null;
    try { var pos = el.position(), sz = el.size(); center = [Number(pos[0]) + Number(sz[0]) / 2, Number(pos[1]) + Number(sz[1]) / 2]; } catch (_) {}
    try { el.click(); return JSON.stringify({ ok: true, via: 'AXPress', center: center }); }
    catch (e) { return JSON.stringify({ ok: false, error: String(e), center: center, actions: (function(){ var a=[]; try { var as=el.actions(); for (var i2=0;i2<as.length;i2++) a.push(String(as[i2].name())); } catch(_){} return a; })() }); }
  })()`;

/** Set an AX-writable value (text fields/areas); read-back verified. */
const AX_SET_SCRIPT = `
  (function () {
    var se = Application('System Events');
    var wantPid = __WANT_PID__, wantName = __WANT_NAME__, wantFront = __WANT_FRONT__;
    var path = String(__PATH__).split('/'), value = __VALUE__;
    var procs = se.processes.whose({ backgroundOnly: false })();
    var p = null;
    for (var i = 0; i < procs.length; i++) {
      var q = procs[i], pid = 0, name = '', front = false;
      try { pid = Number(q.unixId()); } catch (_) {}
      try { name = String(q.name()); } catch (_) {}
      try { front = Boolean(q.frontmost()); } catch (_) {}
      if (wantPid ? pid === wantPid : (String(wantName) ? (name === wantName || name.toLowerCase() === String(wantName).toLowerCase()) : front)) { p = q; break; }
    }
    if (!p) return JSON.stringify({ error: 'no_process' });
    var el = null;
    try {
      el = p.windows()[Number(path[0])];
      for (var j = 1; j < path.length && el; j++) el = el.uiElements()[Number(path[j])];
    } catch (e) { el = null; }
    if (!el) return JSON.stringify({ error: 'stale_path', path: path.join('/') });
    try { el.value = value; } catch (e) { return JSON.stringify({ error: 'not_writable', detail: String(e) }); }
    var back = null;
    try { back = String(el.value()); } catch (_) {}
    return JSON.stringify({ ok: true, set: value, readBack: back, verified: back === value });
  })()`;

/** Fill the __X__ placeholders of an AX script template (numeric or JSON).
 *  Split/join fills: `$`-sequences in user strings must never be
 *  interpreted as replacement patterns. */
function axScript(template, { pid, name, front, path, value, maxNodes, maxDepth }) {
  let out = template;
  const fill = (ph, v) => { out = out.split(ph).join(v); };
  fill("__WANT_PID__", String(Number(pid) || 0));
  fill("__WANT_NAME__", JSON.stringify(String(name ?? "")));
  fill("__WANT_FRONT__", String(front === true));
  fill("__PATH__", JSON.stringify(String(path ?? "")));
  fill("__VALUE__", JSON.stringify(value === undefined ? "" : String(value)));
  fill("__MAX_NODES__", String(Number(maxNodes) || 250));
  fill("__MAX_DEPTH__", String(Number(maxDepth) || 10));
  return out;
}

/** Friendly role names for the snapshot text (raw roles kept in JSON). */
const AX_ROLE_NAMES = {
  AXButton: "button", AXCheckBox: "checkbox", AXRadioButton: "radio", AXMenu: "menu",
  AXMenuItem: "menuitem", AXTextField: "textfield", AXTextArea: "textarea",
  AXStaticText: "text", AXImage: "image", AXSlider: "slider", AXComboBox: "combobox",
  AXPopUpButton: "popup", AXTabGroup: "tabs", AXTab: "tab", AXTable: "table",
  AXRow: "row", AXCell: "cell", AXScrollArea: "scroll", AXGroup: "group",
  AXWindow: "window", AXSheet: "sheet", AXToolbar: "toolbar", AXLink: "link",
  AXList: "list", AXOutline: "outline", AXValueIndicator: "indicator", AXProgressIndicator: "progress",
  AXMenuBar: "menubar", AXMenuBarItem: "menubaritem", AXSplitGroup: "splitter",
};

function axRef(path) {
  // "0/1/0" → "ax1.2.1": 1-based indices only. Paths are unique, so no
  // depth prefix is needed (a concatenated prefix made refs ambiguous).
  return `ax${path.split("/").map((n) => String(Number(n) + 1)).join(".")}`;
}

/** Render the flat node array as browser-observe-style indented text. */
export function axRenderTree(nodes) {
  const lines = [];
  const byRef = new Map();
  const walk = (idx, indent) => {
    const n = nodes[idx];
    if (!n) return;
    const ref = axRef(n.path);
    byRef.set(ref, n);
    const role = AX_ROLE_NAMES[n.role] ?? String(n.role ?? "element").replace(/^AX/, "").toLowerCase();
    const bits = [];
    if (n.title) bits.push(`"${n.title}"`);
    if (n.value != null && n.value !== "" && n.value !== n.title) bits.push(`value=${JSON.stringify(String(n.value).slice(0, 60))}`);
    if (n.enabled === false) bits.push("disabled");
    if (n.w != null && n.h != null && n.x != null && n.y != null) bits.push(`box=${Math.round(n.x)},${Math.round(n.y)},${Math.round(n.w)}x${Math.round(n.h)}`);
    lines.push(`${"  ".repeat(indent)}${role}${bits.length ? ` ${bits.join(" ")}` : ""}${["button", "checkbox", "radio", "menuitem", "textfield", "textarea", "popup", "tab", "link"].includes(role) ? " [act]" : ""} [ref=${ref}]`);
    for (let i = 0; i < nodes.length; i++) if (nodes[i]?.p === idx) walk(i, indent + 1);
  };
  for (let i = 0; i < nodes.length; i++) if (nodes[i]?.p == null) walk(i, 0);
  return { text: lines.join("\n"), byRef };
}

const WINDOWS_SCRIPT = `  (function () {
    var se = Application('System Events');
    var processes = se.processes.whose({ backgroundOnly: false })();
    var out = [], windowAccess = true, frontmost = null;
    for (var i = 0; i < processes.length; i++) {
      var p = processes[i], name = '', pid = null, windows = [], isFront = false;
      try { name = String(p.name()); } catch (_) {}
      try { pid = Number(p.unixId()); } catch (_) {}
      try { isFront = Boolean(p.frontmost()); if (isFront) frontmost = pid; } catch (_) {}
      // Reading process.windows requires Accessibility. Keep a process-level
      // fallback so window discovery remains useful before that grant exists.
      try {
        var ws = p.windows();
        for (var j = 0; j < ws.length; j++) {
          try {
            var row = { title: String(ws[j].name()) || null };
            try { var pos = ws[j].position(); row.x = Number(pos[0]); row.y = Number(pos[1]); } catch (_) {}
            try { var size = ws[j].size(); row.w = Number(size[0]); row.h = Number(size[1]); } catch (_) {}
            windows.push(row);
          } catch (_) {}
        }
      } catch (_) { windowAccess = false; }
      out.push({ name: name, pid: pid, windows: windows, frontmost: isFront });
    }
    return JSON.stringify({ window_access: windowAccess, frontmost_pid: frontmost, processes: out });
  })()`;

export class MacDriver {
  /** `runFn` is injectable for deterministic unit tests of the protocol. */
  constructor({ runFn = run } = {}) {
    this.runFn = runFn;
    this._infoCache = null;
    this._permCache = null;
    // Gestures must never interleave: a click issued while a drag holds the
    // button would post down/up pairs in arbitrary order (same discipline as
    // the resident Windows driver's op chain).
    this._opChain = Promise.resolve();
  }

  /** Serialize an operation against all other input gestures. Probes
   *  (screen_info/permissions) run outside the chain — they post nothing. */
  _serialized(fn) {
    const run = () => fn();
    this._opChain = this._opChain.then(run, run);
    return this._opChain;
  }

  /** Drop cached probe state so the next call re-probes TCC and displays.
   *  Called after every permission-classified failure: a grant made after a
   *  denial is visible on the very next operation (recovery without restart). */
  invalidateCaches() {
    this._infoCache = null;
    this._permCache = null;
    this._naturalScroll = null;
  }

  command(file, args, options) {
    return this.runFn(file, args, options);
  }

  jxa(code) {
    return this.command("osascript", ["-l", "JavaScript", "-e", code]);
  }

  applescript(code) {
    return this.command("osascript", ["-e", code]);
  }

  async screenInfo({ refresh = false } = {}) {
    const hit = !refresh && this._infoCache && Date.now() - this._infoCache.ts < PROBE_CACHE_TTL_MS;
    if (hit) return this._infoCache.value;
    let info;
    try {
      info = parseJson(await this.jxa(SCREEN_INFO_SCRIPT), "screen_info");
    } catch (e) {
      // A headless macOS session (SSH, CI without WindowServer) has no
      // display server to talk to — report that specifically instead of a
      // generic op failure.
      if (/WindowServer|screen server|cannot open|connection.*invalid|no GUI/i.test(e.message)) {
        throw new AxError(Codes.DESKTOP_OP_FAILED,
          `macOS display probes are unavailable in this session (${e.message.slice(0, 200)})`,
          { hint: "Desktop tools need a logged-in GUI session (WindowServer). In SSH/CI contexts use the ax_* browser tools, which work headless.", retry: "never" });
      }
      throw e;
    }
    if (!["x", "y", "w", "h"].every((key) => Number.isFinite(Number(info[key]))) || Number(info.w) < 1 || Number(info.h) < 1) {
      throw new AxError(Codes.DESKTOP_OP_FAILED, "macOS returned incomplete display bounds",
        { hint: "Retry after the display session is ready. If this is an SSH/headless session, desktop tools are unavailable — use the ax_* browser tools.", retry: "retry" });
    }
    const value = {
      x: Number(info.x), y: Number(info.y), w: Number(info.w), h: Number(info.h),
      display_id: Number(info.display_id),
      pixels_w: Number(info.pixels_w), pixels_h: Number(info.pixels_h),
      scale_x: Number(info.scale_x) || 1, scale_y: Number(info.scale_y) || 1,
      coordinate_space: "macOS global display points",
      display_count: Number(info.display_count) || 1,
      displays: Array.isArray(info.displays) ? info.displays : undefined,
      note: "active display layout; screenshots are resized to point dimensions for direct coordinate mapping",
    };
    this._infoCache = { ts: Date.now(), value };
    return value;
  }

  async permissions({ refresh = false } = {}) {
    const hit = !refresh && this._permCache && Date.now() - this._permCache.ts < PROBE_CACHE_TTL_MS;
    if (hit) return this._permCache.value;
    let out;
    try {
      out = parseJson(await this.jxa(PERMISSIONS_SCRIPT), "permissions");
    } catch (e) {
      // Probe failure (e.g. headless session): unknown, not "denied" — the
      // operations themselves still fail closed with their own typed errors.
      return { accessibility: false, screen_recording: null, error: e.message };
    }
    const value = { accessibility: Boolean(out.accessibility), screen_recording: out.screen_recording === null || out.screen_recording === undefined ? null : Boolean(out.screen_recording) };
    this._permCache = { ts: Date.now(), value };
    return value;
  }

  /** EXPLICIT, user-initiated only: ask macOS to open the TCC prompts for
   *  this binary (AXIsProcessTrustedWithOptions with the prompt option and
   *  CGRequestScreenCaptureAccess). Never called by probes or ops — the
   *  doctor exposes it behind --request-tcc so nothing ever prompts
   *  silently. Returns the state AFTER the request (the prompt itself is
   *  answered by the human, possibly later). */
  async requestPermissions() {
    const out = parseJson(await this.jxa(`
      ObjC.import('ApplicationServices');
      ObjC.import('CoreGraphics');
      (function () {
        var opts = $.CFDictionaryCreate(null, [$.kAXTrustedCheckOptionPrompt], [true], 1, null, null);
        var accessibility = Boolean($.AXIsProcessTrustedWithOptions(opts));
        var requested = false;
        try {
          ObjC.bindFunction('CGRequestScreenCaptureAccess', ['bool', []]);
          requested = Boolean($.CGRequestScreenCaptureAccess());
        } catch (_) {}
        return JSON.stringify({ accessibility: accessibility, screen_capture_prompt: requested });
      })()`), "request_permissions");
    this._permCache = null; // re-probe next time (the human may grant async)
    return {
      accessibility: Boolean(out.accessibility),
      screen_recording_prompt_opened: Boolean(out.screen_capture_prompt),
      note: "If the prompts were dismissed, grant manually in System Settings → Privacy & Security; Screen Recording requires restarting this host after granting.",
    };
  }

  /** Construct, but never post, native input events. Safe for doctor/CI. */
  async probe() {
    const events = ["moved", "down", "up", "rdown", "rup", "mdown", "mup", "drag"]
      .map((kind) => ({ kind, x: 0, y: 0 }));
    events.push({ kind: "scroll", dx: 0, dy: 1 }, { kind: "scroll", dx: 1, dy: 0 });
    await this.jxa(mouseSequenceCode(events, { post: false }));
    return { native_events: "ok" };
  }

  async screenshot({ x, y, w, h, budgetBytes = DEFAULT_BUDGET_BYTES, quality = 80 } = {}) {
    let info = await this.screenInfo();
    let rect;
    try {
      rect = rectValues({ x, y, w, h }, info);
    } catch (e) {
      // The cached layout may predate a display change (resolution, monitor
      // plugged in): re-probe once before rejecting the caller's coordinates.
      if (e.code !== Codes.BAD_COORDS || !this._infoCache) throw e;
      info = await this.screenInfo({ refresh: true });
      rect = rectValues({ x, y, w, h }, info);
    }
    const budget = positive(budgetBytes, "budgetBytes");
    const requestedQuality = Math.round(finite(quality, "quality"));
    if (requestedQuality < 1 || requestedQuality > 100) {
      throw new AxError(Codes.COMMAND_REJECTED, "quality must be between 1 and 100", { retry: "never" });
    }
    const permissions = await this.permissions();
    if (permissions.screen_recording === false) {
      throw new AxError(Codes.DESKTOP_PERMISSION_REQUIRED,
        "macOS Screen Recording permission is not granted",
        { hint: PERMISSION_HINT, retry: "retry" });
    }
    const dir = mkdtempSync(join(tmpdir(), "axuse-shot-"));
    const png = join(dir, "capture.png");
    const jpg = join(dir, "capture.jpg");
    const fallbackQuality = Math.min(requestedQuality, 45);
    const sipsHint = "The screenshot was captured, but macOS could not encode it. Check that /usr/bin/sips is available.";
    try {
      try {
        await this.command("screencapture", ["-x", "-t", "png", "-R", `${rect.x},${rect.y},${rect.w},${rect.h}`, png]);
      } catch (e) {
        if (e.code === Codes.DESKTOP_PERMISSION_REQUIRED) this.invalidateCaches();
        throw e;
      }
      let usedQuality = requestedQuality;
      await this.command("sips", [
        "-s", "format", "jpeg", "-s", "formatOptions", String(usedQuality),
        "-z", String(rect.h), String(rect.w), png, "--out", jpg,
      ], { hint: sipsHint });
      let base64 = readFileSync(jpg, "base64");
      let bytes = exactBase64Bytes(base64);
      if (bytes > budget && fallbackQuality !== usedQuality) {
        usedQuality = fallbackQuality;
        await this.command("sips", [
          "-s", "format", "jpeg", "-s", "formatOptions", String(usedQuality),
          "-z", String(rect.h), String(rect.w), png, "--out", jpg,
        ], { hint: sipsHint });
        base64 = readFileSync(jpg, "base64");
        bytes = exactBase64Bytes(base64);
      }
      if (bytes > budget) {
        throw new AxError(Codes.SCREENSHOT_BUDGET_EXCEEDED,
          `Desktop screenshot is ${bytes} bytes, over the ${budget} byte budget even at quality ${usedQuality}.`,
          { hint: "Pass a smaller crop region in macOS display points or raise budget_bytes.", retry: "never" });
      }
      return {
        base64, mime: "image/jpeg", bytes, budget_bytes: budget, quality: usedQuality,
        geometry: `image ${rect.w}x${rect.h} = macOS display points; pass ax_desktop_click coordinates in the same space`,
      };
    } finally {
      try { rmSync(dir, { recursive: true, force: true }); } catch {}
    }
  }

  async move(x, y) {
    x = finite(x, "x"); y = finite(y, "y");
    await this._serialized(() => this.jxa(mouseCode("moved", x, y)));
    return { action_sent: "sent" };
  }

  async click({ x, y, button = "left", double = false, interval_ms = 0 } = {}) {
    interval_ms = pointerInterval(interval_ms);
    x = finite(x, "x"); y = finite(y, "y");
    const b = buttonSpec(button);
    const down = b === "right" ? "rdown" : b === "middle" ? "mdown" : "down";
    const up = b === "right" ? "rup" : b === "middle" ? "mup" : "up";
    // One osascript for the whole gesture — including the double click, whose
    // second pair carries clickState=2 (what Cocoa reads as a double click).
    const events = [
      { kind: "moved", x, y },
      { kind: down, x, y, clickState: 1 }, { kind: up, x, y, clickState: 1 },
    ];
    if (double) events.push({ kind: down, x, y, clickState: 2 }, { kind: up, x, y, clickState: 2 });
    await this._serialized(() => this.jxa(mouseSequenceCode(events, { intervalMs: interval_ms })));
    return { action_sent: "sent" };
  }

  async drag({ from, to, steps = 12, interval_ms = 0 } = {}) {
    interval_ms = pointerInterval(interval_ms);
    const start = { x: finite(from?.x, "from.x"), y: finite(from?.y, "from.y") };
    const end = { x: finite(to?.x, "to.x"), y: finite(to?.y, "to.y") };
    const requestedSteps = finite(steps, "steps");
    if (requestedSteps < 1) throw new AxError(Codes.COMMAND_REJECTED, "steps must be ≥ 1", { retry: "never" });
    const count = Math.round(requestedSteps);
    if (count < 1) throw new AxError(Codes.COMMAND_REJECTED, "steps must round to at least 1", { retry: "never" });
    if (count * interval_ms > 12000) throw new AxError(Codes.COMMAND_REJECTED, "steps × interval_ms must be ≤ 12000", { retry: "never" });
    if (count > 200) throw new AxError(Codes.COMMAND_REJECTED, "steps must be ≤ 200", { retry: "never" });
    await this._serialized(async () => {
      let held = false;
      try {
        // Hover + press in one script: the gesture stays atomic up to the hold.
        await this.jxa(mouseSequenceCode([
          { kind: "moved", x: start.x, y: start.y }, { kind: "down", x: start.x, y: start.y },
        ], { intervalMs: interval_ms }));
        held = true;
        if (interval_ms) await new Promise((resolve) => setTimeout(resolve, interval_ms));
        const moves = [];
        for (let i = 1; i <= count; i++) moves.push({
          kind: "drag",
          x: Math.round(start.x + ((end.x - start.x) * i) / count),
          y: Math.round(start.y + ((end.y - start.y) * i) / count),
        });
        await this.jxa(mouseSequenceCode(moves, { intervalMs: interval_ms }));
        if (interval_ms) await new Promise((resolve) => setTimeout(resolve, interval_ms));
      } finally {
        // The release runs in the SAME chain slot — a held button can never
        // leak past this call, and no queued gesture can interleave with it.
        if (held) await this.jxa(mouseSequenceCode([{ kind: "up", x: end.x, y: end.y }]));
      }
    });
    return { action_sent: "sent" };
  }

  /** True when the user's Natural Scrolling preference is on (the macOS
   *  default). Wheel semantics then invert: the SAME content direction needs
   *  the opposite CG delta. Cached for the process; injectable for tests. */
  async _naturalScrolling({ refresh = false } = {}) {
    if (!refresh && this._naturalScroll !== null && this._naturalScroll !== undefined) return this._naturalScroll;
    let natural = false;
    try {
      const out = await this.command("defaults", ["read", "-g", "com.apple.swipescrolldirection"]);
      natural = String(out).trim() === "1";
    } catch {
      natural = true; // macOS default when the key is unset/unreadable
    }
    this._naturalScroll = natural;
    return natural;
  }

  async wheel(dx = 0, dy = 0) {
    dx = finite(dx, "dx"); dy = finite(dy, "dy");
    if (dx !== 0 || dy !== 0) {
      // Contract (matches ax_scroll in the browser): dy>0 scrolls content
      // DOWN, dy<0 UP; dx>0 LEFT-to-right. CG deltas encode the wheel, not
      // the content: under Natural Scrolling a negative delta moves content
      // down; under traditional scrolling a positive one does.
      const natural = await this._naturalScrolling();
      const map = (v) => (natural ? -v : v);
      const events = dy !== 0 || dx !== 0 ? [{
        kind: "scroll",
        dx: dx !== 0 ? map(dx) : 0,
        dy: dy !== 0 ? map(dy) : 0,
      }] : [];
      await this._serialized(() => this.jxa(mouseSequenceCode(events)));
    }
    return { action_sent: "sent" };
  }

  async type(text) {
    if (typeof text !== "string") {
      throw new AxError(Codes.COMMAND_REJECTED, "text must be a string", { retry: "never" });
    }
    if (text.length === 0) {
      // Nothing to type: do not spawn osascript (and never trigger the
      // Accessibility path) for a guaranteed no-op.
      return { action_sent: "sent", note: "empty string: no events posted" };
    }
    const value = JSON.stringify(text);
    await this._serialized(() => this.jxa(`ObjC.import('ApplicationServices'); if (!$.AXIsProcessTrusted()) throw new Error('Accessibility permission required'); const se = Application("System Events"); se.keystroke(${value}); "ok"`));
    return { action_sent: "sent" };
  }

  async press(combo) {
    const { mods, main, keyCode } = parseMacCombo(combo);
    const using = mods.length ? ` using {${mods.join(", ")}}` : "";
    if (keyCode !== undefined) {
      await this._serialized(() => this.applescript(`tell application "System Events" to key code ${keyCode}${using}`));
    } else if (main.length === 1) {
      await this._serialized(() => this.applescript(`tell application "System Events" to keystroke ${appleScriptLiteral(main)}${using}`));
    } else {
      throw new AxError(Codes.COMMAND_REJECTED, `Unknown key "${main}" in combo`, { retry: "never" });
    }
    return { action_sent: "sent", combo };
  }

  async windows() {
    // System Events walk (needs Accessibility for window rows; process-level
    // rows come back without it). Costs one Apple Event per window attribute
    // — ~1 s with many processes — so agents should treat this as an anchor
    // call, not a poll. Row bounds (global display points) let an agent
    // target a window's content directly.
    const payload = parseJson(await this.jxa(WINDOWS_SCRIPT), "windows_ax");
    const axRows = Array.isArray(payload?.processes) ? payload.processes : [];
    const axAccessFull = payload?.window_access === true;
    const axAccess = Array.isArray(payload) ? "unknown" : axAccessFull ? "full" : "process-fallback";

    const windows = [];
    for (const row of axRows) {
      if (!row || !row.name) continue;
      const pid = Number(row.pid);
      if (!Number.isFinite(pid)) continue;
      const rows = Array.isArray(row.windows) ? row.windows : [];
      if (rows.length) {
        for (const w of rows) {
          windows.push({
            title: String(w?.title ?? row.name),
            pid,
            application: String(row.name),
            hwnd: null,
            ...(row.frontmost ? { frontmost: true } : {}),
            ...(Number.isFinite(w?.x) && Number.isFinite(w?.w)
              ? { bounds: { x: w.x, y: w.y ?? 0, w: w.w, h: w.h ?? 0 }, coordinates: "macOS global display points" }
              : {}),
          });
        }
      } else if (!axAccessFull) {
        windows.push({ title: String(row.name), pid, application: String(row.name), hwnd: null, ...(row.frontmost ? { frontmost: true } : {}) });
      }
    }
    // Frontmost app's windows first — the row an agent almost always wants.
    windows.sort((a, b) => Number(b.frontmost ?? false) - Number(a.frontmost ?? false));
    return {
      windows,
      coordinate_space: "macOS global display points",
      window_access: axAccess,
      frontmost_pid: Number.isFinite(Number(payload?.frontmost_pid)) ? Number(payload.frontmost_pid) : null,
      note: axAccessFull
        ? "titles + bounds via System Events (Accessibility granted); bounds are global display points you can click within"
        : "Accessibility not granted: process-level rows only (no per-window titles/bounds) — grant Accessibility for window-level rows",
    };
  }

  async activate({ title, pid, windowId } = {}) {
    const wantPid = Number.isFinite(Number(pid)) ? Number(pid) : null;
    if (!title && !wantPid && !windowId) {
      throw new AxError(Codes.COMMAND_REJECTED, "activate: title, pid, or window_id is required on macOS (use ax_desktop_windows first)", { retry: "never" });
    }
    const want = JSON.stringify(String(title ?? "").trim());
    const wantWindowId = Number(windowId) || 0;
    const out = parseJson(await this.jxa(`
      ObjC.import('ApplicationServices');
      if (!$.AXIsProcessTrusted()) throw new Error('Accessibility permission required');
      (function () {
        var se = Application("System Events"), want = ${want}, wantPid = ${wantPid ?? 0}, wantWindowId = ${wantWindowId};
        var lower = want.toLowerCase();
        var processes = se.processes.whose({ backgroundOnly: false })();
        for (var i = 0; i < processes.length; i++) {
          var p = processes[i], name = "", pid = 0;
          try { name = String(p.name()); } catch (_) {}
          try { pid = Number(p.unixId()); } catch (_) {}
          var match = wantPid ? pid === wantPid : false;
          if (!match && want) {
            match = name === want || name.toLowerCase() === lower;
            if (!match) {
              try {
                var ws = p.windows();
                for (var j = 0; j < ws.length; j++) {
                  var wt = String(ws[j].name());
                  if (wt === want || wt.toLowerCase() === lower) { match = true; break; }
                }
              } catch (_) {}
            }
          }
          if (match) { p.frontmost = true; return JSON.stringify({ ok: true, application: name, pid: pid }); }
        }
        return JSON.stringify({ ok: false, error: "window or application not found" });
      })()`), "activate");
    if (!out.ok) {
      throw new AxError(Codes.DESKTOP_OP_FAILED, `macOS activate failed: ${out.error}`, { hint: `${PERMISSION_HINT} Use an exact application name, window title, or the pid from ax_desktop_windows.`, retry: "retry" });
    }
    return { activated: true, application: out.application, pid: out.pid };
  }


  async down() { throw unsupported("mouse down (hold)"); }
  async up() { throw unsupported("mouse up (release)"); }

  /* ------------------------------------------------------------ AX layer */

  /** Structure-first snapshot of one app's AX tree (browser-observe style).
   *  Refs are path-encoded (axD<i>.<j>…) and re-resolved LIVE by the act
   *  ops — the same discipline as browser refs. */
  async axSnapshot({ pid, name, front, maxNodes = 250, maxDepth = 10 } = {}) {
    if (!Number.isFinite(Number(pid)) && !name && front !== true) {
      throw new AxError(Codes.COMMAND_REJECTED, "ax_snapshot: one of pid, name, or front=true is required", { retry: "never" });
    }
    const useFront = front ?? (!pid && !name);
    const out = parseJson(await this.jxa(axScript(AX_SNAPSHOT_SCRIPT, { pid, name, front: useFront, maxNodes, maxDepth })), "ax_snapshot");
    if (out.error === "no_process") {
      throw new AxError(Codes.REF_NOT_FOUND, `no running app matches ${out.want}`,
        { hint: "List apps with ax_desktop_windows, or use front:true for the active app.", retry: "reobserve" });
    }
    if (out.error === "no_windows_access") {
      throw new AxError(Codes.DESKTOP_PERMISSION_REQUIRED, `cannot read ${out.app}'s windows — Accessibility is not granted`,
        { hint: PERMISSION_HINT, retry: "retry" });
    }
    const nodes = Array.isArray(out.nodes) ? out.nodes : [];
    const { text, byRef } = axRenderTree(nodes);
    return {
      app: out.app, pid: out.pid, window_count: out.window_count,
      nodes: nodes.length,
      truncated_children: out.truncated_children || 0,
      hit_max_nodes: !!out.hit_max_nodes,
      hit_max_depth: !!out.hit_max_depth,
      text,
      refs: [...byRef.keys()].length,
      note: "refs are live-re-resolved at action time; bounds are in macOS global display points",
    };
  }

  /** Decode an ax ref back to a tree path ("ax1.2.1" → "0/1/0"). */
  static axRefToPath(ref) {
    const m = /^ax(\d+(?:\.\d+)*)$/.exec(String(ref ?? "").trim());
    if (!m) return null;
    const parts = m[1].split(".").map((n) => Number(n) - 1);
    if (parts.some((n) => !Number.isInteger(n) || n < 0)) return null;
    return parts.join("/");
  }

  _axTarget({ pid, name, front = true } = {}) {
    return { pid, name, front };
  }

  async axRead({ ref, pid, name, front } = {}) {
    const path = MacDriver.axRefToPath(ref);
    if (!path) throw new AxError(Codes.COMMAND_REJECTED, `ax_read: malformed ref "${ref}"`, { retry: "never" });
    const useFront = front ?? (!pid && !name);
    const out = parseJson(await this.jxa(axScript(AX_RESOLVE_SCRIPT, { pid, name, front: useFront, path })), "ax_read");
    if (out.error === "stale_path" || out.error === "no_process") {
      throw new AxError(Codes.REF_NOT_FOUND, `ref "${ref}" no longer resolves (${out.error})`,
        { hint: "The UI changed under you — take a fresh ax_snapshot.", retry: "reobserve" });
    }
    return { app: out.app, pid: out.pid, ref, ...out.element };
  }

  async axClick({ ref, pid, name, front, fallbackToCoords = true } = {}) {
    const path = MacDriver.axRefToPath(ref);
    if (!path) throw new AxError(Codes.COMMAND_REJECTED, `ax_click: malformed ref "${ref}"`, { retry: "never" });
    const useFront = front ?? (!pid && !name);
    const out = parseJson(await this.jxa(axScript(AX_PRESS_SCRIPT, { pid, name, front: useFront, path })), "ax_click");
    if (out.error === "stale_path" || out.error === "no_process") {
      throw new AxError(Codes.REF_NOT_FOUND, `ref "${ref}" no longer resolves (${out.error})`,
        { hint: "The UI changed under you — take a fresh ax_snapshot.", retry: "reobserve" });
    }
    if (out.ok) return { action_sent: "sent", via: "AXPress", ref };
    // AXPress failed (some apps refuse programmatic press): fall back to a
    // trusted CGEvent click at the element's live center — geometry came
    // from the SAME resolve call, so it cannot be stale.
    if (fallbackToCoords && Array.isArray(out.center) && Number.isFinite(out.center[0]) && Number.isFinite(out.center[1])) {
      await this.click({ x: out.center[0], y: out.center[1] });
      return { action_sent: "sent", via: "coordinate-fallback (AXPress refused)", ref };
    }
    throw new AxError(Codes.DESKTOP_OP_FAILED,
      `AXPress refused for "${ref}" and no bounds for a coordinate fallback`,
      { hint: `Element actions: ${(out.actions ?? []).join(", ") || "none reported"}. Use ax_read to re-observe.`, retry: "retry" });
  }

  async axSet({ ref, value, pid, name, front } = {}) {
    if (value === undefined || value === null) {
      throw new AxError(Codes.COMMAND_REJECTED, "ax_set: value is required", { retry: "never" });
    }
    const path = MacDriver.axRefToPath(ref);
    if (!path) throw new AxError(Codes.COMMAND_REJECTED, `ax_set: malformed ref "${ref}"`, { retry: "never" });
    const useFront = front ?? (!pid && !name);
    const out = parseJson(await this.jxa(axScript(AX_SET_SCRIPT, { pid, name, front: useFront, path, value: String(value) })), "ax_set");
    if (out.error === "stale_path" || out.error === "no_process") {
      throw new AxError(Codes.REF_NOT_FOUND, `ref "${ref}" no longer resolves (${out.error})`,
        { hint: "The UI changed under you — take a fresh ax_snapshot.", retry: "reobserve" });
    }
    if (out.error === "not_writable") {
      throw new AxError(Codes.COMMAND_REJECTED, `element "${ref}" is not AX-writable (${out.detail?.slice(0, 120) ?? "no settable value"})`,
        { hint: "Text fields/areas are writable; for others use ax_click.", retry: "never" });
    }
    return { action_sent: "sent", ref, set: out.set, readBack: out.readBack, verified: !!out.verified,
      ...(out.verified ? {} : { note: "read-back mismatch reported, never assumed" }) };
  }

  dispose() {}
}

function unsupported(op) {
  return new AxError(Codes.DESKTOP_OP_FAILED,
    `Op "${op}" is not implemented on the macOS driver.`,
    { hint: "Use ax_desktop_drag for a complete drag gesture; file an issue for a separately held button gesture.", retry: "never" });
}
