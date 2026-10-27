/**
 * Passive desktop-driver coverage. It never moves the real user's pointer or
 * sends keys. Windows exercises the resident driver; macOS exercises the
 * permission-safe screen/window discovery path when explicitly enabled with
 * AXUSE_TEST_MACOS_DESKTOP=1. A real screenshot additionally requires
 * AXUSE_TEST_MACOS_SCREENSHOT=1 because Screen Recording is TCC-protected.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import { getDesktopDriver } from "../src/desktop/index.js";
import { MacDriver, MACOS_KEY_CODES, parseMacCombo } from "../src/desktop/macos.js";

const isWindows = process.platform === "win32";
const isMac = process.platform === "darwin";
const macEnabled = isMac && process.env.AXUSE_TEST_MACOS_DESKTOP === "1";
const driver = isWindows ? getDesktopDriver() : macEnabled ? new MacDriver() : null;

test("desktop driver: screen_info reports a usable native coordinate space", { skip: !driver }, async (t) => {
  t.after(() => driver.dispose());
  const info = await driver.screenInfo();
  assert.ok(info.w > 0 && info.h > 0, `screen ${info.w}x${info.h}`);
  assert.equal(typeof info.coordinate_space, "string");
  if (isMac) {
    assert.equal(info.coordinate_space, "macOS global display points");
    assert.ok(info.scale_x > 0 && info.scale_y > 0);
  }
});

test("desktop driver: budgeted screenshot of a small region", { skip: !isWindows }, async (t) => {
  t.after(() => driver.dispose());
  const shot = await driver.screenshot({ x: 0, y: 0, w: 300, h: 200, budgetBytes: 200_000 });
  assert.ok(shot.base64.length > 100);
  assert.ok(shot.bytes <= 200_000, `shot ${shot.bytes} bytes within budget`);
  assert.equal(shot.mime, "image/jpeg");
});

test("desktop driver: macOS screenshot is opt-in and budgeted", { skip: !macEnabled || process.env.AXUSE_TEST_MACOS_SCREENSHOT !== "1" }, async (t) => {
  t.after(() => driver.dispose());
  const shot = await driver.screenshot({ x: 0, y: 0, w: 300, h: 200, budgetBytes: 200_000 });
  assert.ok(shot.base64.length > 100);
  assert.ok(shot.bytes <= 200_000, `shot ${shot.bytes} bytes within budget`);
  assert.equal(shot.mime, "image/jpeg");
  assert.equal(shot.quality, 80);
});

test("desktop driver: window discovery has stable title/pid records", { skip: !driver }, async (t) => {
  t.after(() => driver.dispose());
  const { windows } = await driver.windows();
  assert.ok(Array.isArray(windows));
  assert.ok(windows.some((w) => typeof w.title === "string" && w.title.length > 0));
  assert.ok(windows.some((w) => Number.isInteger(w.pid) && w.pid > 0));
  if (isMac) assert.ok(windows.every((w) => w.hwnd === null));
});

test("macOS permission probe is non-interactive and typed", { skip: !macEnabled }, async () => {
  const permissions = await driver.permissions();
  assert.equal(typeof permissions.accessibility, "boolean");
  assert.equal(typeof permissions.screen_recording, "boolean");
});

test("macOS screenshot reports the typed TCC error when preflight denies access", { skip: !macEnabled }, async (t) => {
  const permissions = await driver.permissions();
  if (permissions.screen_recording !== false) t.skip("Screen Recording is already granted or unavailable to preflight");
  await assert.rejects(
    () => driver.screenshot({ x: 0, y: 0, w: 1, h: 1, budgetBytes: 20_000 }),
    (e) => e.code === "DESKTOP_PERMISSION_REQUIRED" && /Screen Recording|Grant the host|screencapture/i.test(`${e.message} ${e.hint ?? ""}`),
  );
});

test("macOS posted input fails typed when Accessibility preflight denies access", { skip: !macEnabled }, async (t) => {
  const permissions = await driver.permissions();
  if (permissions.accessibility !== false) t.skip("Accessibility is already granted");
  await assert.rejects(
    () => driver.move(0, 0),
    (e) => e.code === "DESKTOP_PERMISSION_REQUIRED" && /Accessibility|assistive/i.test(`${e.message} ${e.hint ?? ""}`),
  );
  await assert.rejects(
    () => driver.press("f2"),
    (e) => e.code === "DESKTOP_PERMISSION_REQUIRED" && /Accessibility|assistive/i.test(`${e.message} ${e.hint ?? ""}`),
  );
});

test("macOS key contract uses real function-key codes and Command aliases", () => {
  assert.equal(MACOS_KEY_CODES.f2, 120);
  assert.equal(MACOS_KEY_CODES.f12, 111);
  assert.deepEqual(parseMacCombo("cmd+shift+f2"), {
    mods: ["command down", "shift down"], main: "f2", keyCode: 120,
  });
  assert.deepEqual(parseMacCombo("meta+a"), {
    mods: ["command down"], main: "a", keyCode: undefined,
  });
  assert.throws(() => parseMacCombo("cmd+"), /No non-modifier/);
});

test("macOS screenshot adapter retries encoding quality before rejecting the budget", async () => {
  const calls = [];
  const fakeRun = async (file, args) => {
    calls.push({ file, args });
    if (file === "osascript") return JSON.stringify({ display_id: 1, x: 0, y: 0, w: 1920, h: 1080, pixels_w: 1920, pixels_h: 1080, scale_x: 1, scale_y: 1 });
    if (file === "sips") {
      const q = args[args.indexOf("formatOptions") + 1];
      writeFileSync(args.at(-1), Buffer.alloc(q === "80" ? 4_000 : 1_000));
    }
    return "";
  };
  const d = new MacDriver({ runFn: fakeRun });
  const shot = await d.screenshot({ x: 10, y: 20, w: 300, h: 200, budgetBytes: 2_000 });
  assert.equal(shot.quality, 45);
  assert.ok(shot.bytes <= 2_000);
  const firstSips = calls.find(({ file }) => file === "sips");
  assert.deepEqual(firstSips.args.slice(0, 8), ["-s", "format", "jpeg", "-s", "formatOptions", "80", "-z", "200"]);
  assert.ok(calls.some(({ file, args }) => file === "screencapture" && args.includes("-R") && args.includes("10,20,300,200")));
});

test("macOS screenshot adapter preserves negative global display coordinates", async () => {
  const calls = [];
  const fakeRun = async (file, args) => {
    calls.push({ file, args });
    if (file === "osascript") return JSON.stringify({ display_id: 2, x: -1440, y: -100, w: 3360, h: 1180, pixels_w: 2560, pixels_h: 1180, scale_x: 2, scale_y: 2 });
    if (file === "sips") writeFileSync(args.at(-1), Buffer.alloc(900));
    return "";
  };
  const shot = await new MacDriver({ runFn: fakeRun }).screenshot({ x: -1400, y: -90, w: 300, h: 200, budgetBytes: 2_000 });
  assert.equal(shot.bytes, 900);
  assert.ok(calls.some(({ file, args }) => file === "screencapture" && args.includes("-1400,-90,300,200")));
});

test("macOS input adapter emits correct button, scroll, and modifier events", async () => {
  const calls = [];
  const d = new MacDriver({ runFn: async (file, args) => { calls.push({ file, args }); return "ok"; } });
  await d.click({ x: 4, y: 5, button: "right" });
  await d.wheel(7, 9);
  await d.press("cmd+f2");
  const scripts = calls.filter(({ file }) => file === "osascript").map(({ args }) => args.at(-1)).join("\n");
  assert.match(scripts, /kCGEventRightMouseDown/);
  assert.match(scripts, /kCGMouseButtonRight/);
  assert.match(scripts, /var e0 = \$\.CGEventCreateMouseEvent[^]*var e1 = \$\.CGEventCreateMouseEvent[^]*var e2 = \$\.CGEventCreateMouseEvent/);
  assert.match(scripts, /CGEventCreateScrollWheelEvent[^]*, 2, -9, -7/);
  assert.match(scripts, /key code 120 using \{command down\}/);
});

test("macOS double click is ONE gesture whose second pair carries clickState=2", async () => {
  const calls = [];
  const d = new MacDriver({ runFn: async (file, args) => { calls.push({ file, args }); return "ok"; } });
  await d.click({ x: 3, y: 4, double: true });
  const scripts = calls.filter(({ file }) => file === "osascript").map(({ args }) => args.at(-1));
  assert.equal(scripts.length, 1, "the whole double click is a single osascript");
  const s = scripts[0];
  const states = [...s.matchAll(/CGEventSetIntegerValueField\(e\d+, 1, (\d)\)/g)].map((m) => Number(m[1]));
  assert.deepEqual(states, [1, 1, 2, 2], "clickState 1 on the first pair, 2 on the second (Cocoa reads the field, not timing alone)");
  assert.match(s, /kCGEventLeftMouseDown/);
});

test("macOS screen info and permissions are cached and invalidated on permission failures", async () => {
  const calls = [];
  let screencaptureFails = false;
  const d = new MacDriver({ runFn: async (file, args) => {
    calls.push(file);
    if (file === "osascript" && args.at(-1).includes("CGMainDisplayID")) {
      return JSON.stringify({ display_id: 1, x: 0, y: 0, w: 1920, h: 1080, pixels_w: 1920, pixels_h: 1080, scale_x: 1, scale_y: 1 });
    }
    if (file === "osascript") return JSON.stringify({ accessibility: true, screen_recording: true });
    if (file === "screencapture" && screencaptureFails) {
      const e = new Error("screencapture failed: screencapture: could not create image from display");
      e.code = "DESKTOP_PERMISSION_REQUIRED";
      screencaptureFails = false;
      d.invalidateCaches();
      throw e;
    }
    if (file === "sips") writeFileSync(args.at(-1), Buffer.alloc(500));
    return "";
  } });
  await d.screenshot({ x: 0, y: 0, w: 100, h: 80, budgetBytes: 2_000 });
  const first = calls.filter((f) => f === "osascript").length; // info + permissions probes
  assert.equal(first, 2, "first screenshot probes screen info and permissions once each");
  calls.length = 0;
  await d.screenshot({ x: 0, y: 0, w: 100, h: 80, budgetBytes: 2_000 }); // warm: no probes
  assert.equal(calls.filter((f) => f === "osascript").length, 0, "warm screenshot skips both JXA probes");
  calls.length = 0;
  screencaptureFails = true;
  await assert.rejects(() => d.screenshot({ x: 0, y: 0, w: 100, h: 80, budgetBytes: 2_000 }), /could not create image/);
  await d.screenshot({ x: 0, y: 0, w: 100, h: 80, budgetBytes: 2_000 });
  assert.ok(calls.includes("osascript"), "permission-classified failure invalidates caches → next call re-probes");
});

test("macOS screenshot re-probes display layout once when a cached-bounds rejection could be stale", async () => {
  let layout = { display_id: 1, x: 0, y: 0, w: 1920, h: 1080, pixels_w: 1920, pixels_h: 1080, scale_x: 1, scale_y: 1 };
  const probeCount = { info: 0 };
  const d = new MacDriver({ runFn: async (file, args) => {
    if (file === "osascript" && args.at(-1).includes("CGMainDisplayID")) {
      probeCount.info++;
      return JSON.stringify(layout);
    }
    if (file === "osascript") return JSON.stringify({ accessibility: true, screen_recording: true });
    if (file === "sips") writeFileSync(args.at(-1), Buffer.alloc(500));
    return "";
  } });
  await d.screenshot({ x: 0, y: 0, w: 100, h: 80, budgetBytes: 2_000 }); // caches the 1920x1080 layout
  layout = { display_id: 1, x: 0, y: 0, w: 3840, h: 2160, pixels_w: 3840, pixels_h: 2160, scale_x: 2, scale_y: 2 };
  const shot = await d.screenshot({ x: 2000, y: 1000, w: 300, h: 200, budgetBytes: 8_000 }); // outside stale layout, inside new one
  assert.equal(shot.bytes, 500);
  assert.equal(probeCount.info, 2, "initial probe, then exactly one stale-layout re-probe");
});

test("macOS probe scripts never request a TCC prompt (first-run discipline)", async () => {
  const d = new MacDriver({ runFn: async (file, args) => {
    if (file === "osascript" && args.at(-1).includes("AXIsProcessTrusted")) {
      assert.doesNotMatch(args.at(-1), /Options|kAXTrustedCheckOptionPrompt|CGRequestScreenCaptureAccess/,
        "probes must stay non-interactive: AXIsProcessTrusted/CGPreflight only");
    }
    return "{}";
  } });
  await d.permissions();
  await d.permissions({ refresh: true });
});

test("macOS requestPermissions is the ONLY path that opens the TCC prompt", async () => {
  const calls = [];
  const d = new MacDriver({ runFn: async (file, args) => {
    calls.push(args.at(-1));
    return JSON.stringify({ accessibility: false, screen_capture_prompt: true });
  } });
  const r = await d.requestPermissions();
  assert.deepEqual(r, {
    accessibility: false,
    screen_recording_prompt_opened: true,
    note: "If the prompts were dismissed, grant manually in System Settings → Privacy & Security; Screen Recording requires restarting this host after granting.",
  });
  assert.match(calls[0], /kAXTrustedCheckOptionPrompt/);
  assert.match(calls[0], /CGRequestScreenCaptureAccess/);
  // And the permission cache was dropped: the next probe re-reads TCC.
  const probes = [];
  const d2 = new MacDriver({ runFn: async (file, args) => { probes.push(args.at(-1)); return JSON.stringify({ accessibility: true, screen_recording: true }); } });
  d2._permCache = { ts: Date.now(), value: { accessibility: false, screen_recording: false } };
  await d2.requestPermissions();
  await d2.permissions();
  assert.equal(probes.length, 2, "request + re-probe (cache invalidated by the request)");
  assert.match(probes[0], /kAXTrustedCheckOptionPrompt/);
  assert.doesNotMatch(probes[1], /kAXTrustedCheckOptionPrompt|CGRequestScreenCaptureAccess/, "the follow-up probe stays non-interactive");
});

test("macOS drag batches moves and always releases after a move failure", async () => {
  const calls = [];
  let failMoves = true;
  const d = new MacDriver({ runFn: async (file, args) => {
    calls.push({ file, args });
    if (file === "osascript" && failMoves && args.at(-1).includes("kCGEventLeftMouseDragged")) {
      failMoves = false;
      throw new Error("synthetic drag failure");
    }
    return "ok";
  } });
  await assert.rejects(() => d.drag({ from: { x: 1, y: 2 }, to: { x: 10, y: 20 }, steps: 3 }), /synthetic drag failure/);
  const scripts = calls.filter(({ file }) => file === "osascript").map(({ args }) => args.at(-1));
  assert.equal(scripts.length, 3, "hover+down, batched moves, release");
  assert.match(scripts.at(-1), /kCGEventLeftMouseUp/);
});

test("macOS desktop inputs reject coercible coordinates and zero-length drags", async () => {
  const d = new MacDriver({ runFn: async () => "ok" });
  await assert.rejects(() => d.move(null, 1), (e) => e.code === "BAD_COORDS");
  await assert.rejects(() => d.drag({ from: { x: 1, y: 1 }, to: { x: 2, y: 2 }, steps: 0.1 }), (e) => e.code === "COMMAND_REJECTED");
  await assert.rejects(() => d.type(null), (e) => e.code === "COMMAND_REJECTED");
});

test("macOS native event probe never posts an event", async () => {
  const calls = [];
  const d = new MacDriver({ runFn: async (file, args) => { calls.push({ file, args }); return "ok"; } });
  assert.deepEqual(await d.probe(), { native_events: "ok" });
  const scripts = calls.map(({ args }) => args.at(-1)).join("\n");
  assert.doesNotMatch(scripts, /CGEventPost/);
});

test("macOS native event construction runs on this host without posting input", { skip: !macEnabled }, async () => {
  assert.deepEqual(await new MacDriver().probe(), { native_events: "ok" });
});

test("macOS gestures never interleave: concurrent click and drag serialize", async () => {
  const order = [];
  const d = new MacDriver({ runFn: async (file, args) => {
    const script = args.at(-1);
    // The click carries its unique coordinates; the drag starts at (10,10).
    const label = script.includes("CGPointMake(1, 1)") ? "click"
      : script.includes("LeftMouseDragged") ? "drag-moves"
        : script.includes("kCGEventLeftMouseDown") ? "drag-press"
          : script.includes("kCGEventLeftMouseUp") ? "drag-release" : "other";
    order.push(`start:${label}`);
    await new Promise((ok) => setTimeout(ok, 5));
    order.push(`end:${label}`);
    return "ok";
  } });
  // Fire both gestures without awaiting the first: the chain must serialize.
  await Promise.all([
    d.click({ x: 1, y: 1 }),
    d.drag({ from: { x: 10, y: 10 }, to: { x: 30, y: 30 }, steps: 2 }),
  ]);
  const starts = order.filter((e) => e.startsWith("start:")).map((s) => s.slice(6));
  assert.equal(starts[0], "click", "whichever gesture was issued first runs first");
  assert.ok(order.indexOf("end:click") < order.indexOf("start:drag-press"), "the drag's first script starts only after the click's gesture fully finished");
  assert.deepEqual(starts.slice(1), ["drag-press", "drag-moves", "drag-release"], "the drag's own scripts stay contiguous");
});

test("macOS wheel follows the Natural Scrolling preference (content-direction contract)", async () => {
  const posted = (calls) => {
    const s = calls.filter((c) => c.file === "osascript").map((c) => c.args.at(-1)).find((x) => x.includes("ScrollWheel"));
    return /UnitPixel, (1|2), (-?\d+)(?:, (-?\d+))?\)/.exec(s);
  };
  const driverWithPref = (prefOut) => {
    const calls = [];
    const d = new MacDriver({ runFn: async (file, args) => {
      calls.push({ file, args });
      if (file === "defaults") return prefOut;
      return "ok";
    } });
    return { d, calls };
  };
  // dy>0 = content DOWN regardless of the pref; only the CG delta flips.
  const natural = driverWithPref("1\n");
  await natural.d.wheel(0, 9);
  let m = posted(natural.calls);
  assert.equal(m[2], "9", "natural ON → positive CG delta moves content down (builder pre-negates)");
  const traditional = driverWithPref("0\n");
  await traditional.d.wheel(0, 9);
  m = posted(traditional.calls);
  assert.equal(m[2], "-9", "natural OFF → negative CG delta moves content down");
  // Two-axis case keeps dx in wheel1 with the same mapping.
  const nat2 = driverWithPref("1\n");
  await nat2.d.wheel(7, 9);
  m = posted(nat2.calls);
  assert.equal(m[1], "2");
  assert.equal(m[2], "9");
  assert.equal(m[3], "7");
});

test("macOS windows(): full-AX rows carry titles and global-point bounds", async () => {
  const axPayload = { window_access: true, processes: [
    { name: "Google Chrome", pid: 101, windows: [
      { title: "Docs - Chrome", x: 5, y: 38, w: 1200, h: 800 },
      { title: null, x: 1300, y: 40, w: 600, h: 400 },
    ] },
  ] };
  const d = new MacDriver({ runFn: async (file, args) => JSON.stringify(axPayload) });
  const out = await d.windows();
  assert.equal(out.windows.length, 2);
  const first = out.windows[0];
  assert.equal(first.title, "Docs - Chrome");
  assert.deepEqual(first.bounds, { x: 5, y: 38, w: 1200, h: 800 }, "bounds are clickable global display points");
  assert.equal(first.application, "Google Chrome");
  assert.equal(out.windows[1].title, "Google Chrome", "untitled windows fall back to the app name");
  assert.equal(out.window_access, "full");
  assert.match(out.note, /Accessibility granted/);
});

test("macOS windows(): without Accessibility, process-level rows + honest hint", async () => {
  const axPayload = { window_access: false, processes: [
    { name: "Notes", pid: 55, windows: [] },
    { name: "Finder", pid: 66, windows: [] },
  ] };
  const d = new MacDriver({ runFn: async (file, args) => JSON.stringify(axPayload) });
  const out = await d.windows();
  assert.equal(out.windows.length, 2);
  assert.ok(out.windows.every((w) => w.bounds === undefined), "no geometry invented without the grant");
  assert.equal(out.window_access, "process-fallback");
  assert.match(out.note, /grant Accessibility/);
});

test("macOS activate accepts a pid (robust when titles are TCC-gated)", async () => {
  const calls = [];
  const d = new MacDriver({ runFn: async (file, args) => {
    calls.push(args.at(-1));
    return JSON.stringify({ ok: true, application: "Google Chrome", pid: 101 });
  } });
  const r = await d.activate({ pid: 101 });
  assert.equal(r.activated, true);
  assert.equal(r.application, "Google Chrome");
  assert.match(calls[0], /wantPid = 101/);
  await assert.rejects(() => d.activate({}), (e) => e.code === "COMMAND_REJECTED" && /title, pid, or window_id/.test(e.message) === true);
});

test("macOS type(\"\") is a guaranteed no-op: no spawn, no permission path", async () => {
  const calls = [];
  const d = new MacDriver({ runFn: async (file, args) => { calls.push({ file, args }); return "ok"; } });
  const r = await d.type("");
  assert.equal(r.action_sent, "sent");
  assert.match(r.note, /no events posted/);
  assert.equal(calls.length, 0, "an empty string must never spawn osascript or hit the AX gate");
});

test("macOS windows(): frontmost app's rows come first and are flagged", async () => {
  const payload = { window_access: true, frontmost_pid: 202, processes: [
    { name: "Finder", pid: 101, windows: [{ title: "Downloads", x: 0, y: 0, w: 500, h: 400 }], frontmost: false },
    { name: "TextEdit", pid: 202, windows: [{ title: "notes.txt", x: 10, y: 10, w: 400, h: 300 }], frontmost: true },
  ] };
  const d = new MacDriver({ runFn: async () => JSON.stringify(payload) });
  const out = await d.windows();
  assert.equal(out.windows[0].title, "notes.txt");
  assert.equal(out.windows[0].frontmost, true);
  assert.equal(out.frontmost_pid, 202);
  assert.equal(out.windows[1].frontmost, undefined, "only the frontmost app carries the flag");
});

/* ------------------------------------------------------- AX layer (mocked) */

const axTree = {
  app: "Notes", pid: 4242, window_count: 1,
  truncated_children: 3, hit_max_nodes: false, hit_max_depth: false,
  nodes: [
    { path: "0", p: null, d: 0, role: "AXWindow", title: "Notes", x: 0, y: 0, w: 700, h: 500, enabled: true },
    { path: "0/0", p: 0, d: 1, role: "AXButton", title: "New Note", x: 8, y: 8, w: 30, h: 24, enabled: true },
    { path: "0/1", p: 0, d: 1, role: "AXTextArea", title: "Body", value: "hello", x: 10, y: 60, w: 600, h: 400 },
    { path: "0/1/0", p: 2, d: 2, role: "AXStaticText", title: "hello", x: 12, y: 62, w: 40, h: 18 },
  ],
};

test("macOS AX snapshot renders a browser-observe-style tree with live refs", async () => {
  const d = new MacDriver({ runFn: async (file, args) => JSON.stringify(axTree) });
  const snap = await d.axSnapshot({ pid: 4242 });
  assert.equal(snap.app, "Notes");
  assert.equal(snap.nodes, 4);
  assert.equal(snap.truncated_children, 3);
  assert.match(snap.text, /window "Notes" box=0,0,700x500 \[ref=ax1\]/);
  assert.match(snap.text, /button "New Note" box=8,8,30x24 \[act\] \[ref=ax1\.1\]/);
  assert.match(snap.text, /textarea "Body" value="hello" box=10,60,600x400 \[act\] \[ref=ax1\.2\]/);
  assert.match(snap.text, / {4}text "hello" box=12,62,40x18 \[ref=ax1\.2\.1\]/);
  // Ref ↔ path round-trip for every rendered ref (axD<i>.<j>… → <i-1>/<j-1>…).
  assert.equal(MacDriver.axRefToPath("ax1"), "0");
  assert.equal(MacDriver.axRefToPath("ax1.1"), "0/0");
  assert.equal(MacDriver.axRefToPath("ax1.2"), "0/1");
  assert.equal(MacDriver.axRefToPath("ax1.2.1"), "0/1/0");
  for (const m of snap.text.matchAll(/\[ref=(ax[0-9.]+)\]/g)) {
    assert.ok(MacDriver.axRefToPath(m[1]), `ref ${m[1]} decodes`);
  }
  // Malformed refs are rejected before any spawn.
  await assert.rejects(() => d.axRead({ ref: "e1", pid: 1 }), (e) => e.code === "COMMAND_REJECTED");
  await assert.rejects(() => d.axClick({ ref: "ax0x", pid: 1 }), (e) => e.code === "COMMAND_REJECTED");
  await assert.rejects(() => d.axSet({ ref: "ax00" }), (e) => e.code === "COMMAND_REJECTED" && /value is required/.test(e.message) === true);
});

test("macOS AX click: AXPress first, coordinate fallback when refused, typed error without bounds", async () => {
  const calls = [];
  const makeD = (out) => new MacDriver({ runFn: async (file, args) => {
    calls.push({ file, args });
    if (file === "osascript" && args.at(-1).includes("AXPress") === false && args.at(-1).includes("uiElements")) return "ok";
    return JSON.stringify(out());
  } });
  // 1. Clean AXPress.
  let d = makeD(() => ({ ok: true, via: "AXPress", center: [100, 50] }));
  let r = await d.axClick({ ref: "ax1.1", pid: 1 });
  assert.deepEqual(r, { action_sent: "sent", via: "AXPress", ref: "ax1.1" });
  // 2. Refused with center → one trusted CGEvent click at the live center.
  calls.length = 0;
  d = makeD(() => ({ ok: false, error: "not allowed", center: [220, 140], actions: ["AXShowMenu"] }));
  r = await d.axClick({ ref: "ax1.1", pid: 1 });
  assert.equal(r.via, "coordinate-fallback (AXPress refused)");
  const fallback = calls.filter((c) => c.file === "osascript" && c.args.at(-1).includes("kCGEventLeftMouseDown"));
  assert.ok(fallback.length === 1, "exactly one coordinate click");
  assert.match(fallback[0].args.at(-1), /CGPointMake\(220, 140\)/);
  // 3. Refused without bounds → typed, actionable, with the action list.
  d = makeD(() => ({ ok: false, error: "nope", center: null, actions: ["AXShowMenu"] }));
  await assert.rejects(() => d.axClick({ ref: "ax1.1", pid: 1 }), (e) => e.code === "DESKTOP_OP_FAILED" && /AXShowMenu/.test(e.hint) === true);
  // 4. Stale path → REF_NOT_FOUND with a reobserve hint (browser discipline).
  d = makeD(() => ({ error: "stale_path", path: "0/1" }));
  await assert.rejects(() => d.axClick({ ref: "ax1.1", pid: 1 }), (e) => e.code === "REF_NOT_FOUND" && e.retry === "reobserve");
});

test("macOS AX set verifies read-back and reports mismatches; non-writable is typed", async () => {
  const makeD = (out) => new MacDriver({ runFn: async () => JSON.stringify(out()) });
  let d = makeD(() => ({ ok: true, set: "typed text", readBack: "typed text", verified: true }));
  let r = await d.axSet({ ref: "ax1.2", value: "typed text", pid: 1 });
  assert.equal(r.verified, true);
  d = makeD(() => ({ ok: true, set: "x", readBack: "y", verified: false }));
  r = await d.axSet({ ref: "ax1.2", value: "x", pid: 1 });
  assert.equal(r.verified, false);
  assert.match(r.note, /never assumed/);
  d = makeD(() => ({ error: "not_writable", detail: "button has no settable value" }));
  await assert.rejects(() => d.axSet({ ref: "ax1.1", value: "x", pid: 1 }), (e) => e.code === "COMMAND_REJECTED" && /not AX-writable/.test(e.message) === true);
});

test("macOS AX scripts inject targets safely ($-sequences and quotes never corrupt the script)", async () => {
  const seen = [];
  const d = new MacDriver({ runFn: async (file, args) => {
    seen.push(args.at(-1));
    return JSON.stringify(axTree);
  } });
  await d.axSnapshot({ name: `Notes "$& evil'` });
  const script = seen[0];
  assert.ok(script.includes('"Notes \\"$& evil\'"') || /Notes "\$& evil'/.test(script), "the name lands as one JSON string literal");
  assert.doesNotMatch(script, /__WANT_|__PATH_|__MAX_|__VALUE_/, "no placeholder survives");
  assert.match(script, /maxNodes = 250/, "defaults filled");
  // pid + front default: explicit pid wins, front literal false.
  seen.length = 0;
  await d.axSnapshot({ pid: 99 });
  assert.match(seen[0], /wantPid = 99, wantName = "", wantFront = false/);
  seen.length = 0;
  await d.axSnapshot({ front: true });
  assert.match(seen[0], /wantFront = true/);
});

test("macOS ax_snapshot: no matching app is typed REF_NOT_FOUND; AX-missing is typed permission", async () => {
  let d = new MacDriver({ runFn: async () => JSON.stringify({ error: "no_process", want: "Ghost" }) });
  await assert.rejects(() => d.axSnapshot({ name: "Ghost" }), (e) => e.code === "REF_NOT_FOUND" && /Ghost/.test(e.message) === true);
  d = new MacDriver({ runFn: async () => JSON.stringify({ error: "no_windows_access", app: "Notes" }) });
  await assert.rejects(() => d.axSnapshot({ name: "Notes" }), (e) => e.code === "DESKTOP_PERMISSION_REQUIRED");
  await assert.rejects(() => d.axSnapshot({}), (e) => e.code === "COMMAND_REJECTED");
});

test("macOS ax ops fail typed without Accessibility (live, denial path)", { skip: !macEnabled }, async (t) => {
  const d = new MacDriver();
  const p = await d.permissions();
  if (p.accessibility === true) t.skip("Accessibility is granted here — granted paths run in the manual lane");
  await assert.rejects(
    () => d.axSnapshot({ front: true }),
    (e) => e.code === "DESKTOP_PERMISSION_REQUIRED" && /Accessibility/.test(e.message) === true,
  );
});

test("macOS pointer timing is inserted between events and validated", async () => {
  const calls = [];
  const d = new MacDriver({ runFn: async (file, args) => { calls.push({ file, args }); return "ok"; } });
  await d.click({ x: 4, y: 5, interval_ms: 40 });
  assert.match(calls[0].args.at(-1), /delay\(0\.04\)/);
  calls.length = 0;
  await d.drag({ from: { x: 1, y: 2 }, to: { x: 10, y: 20 }, steps: 3, interval_ms: 1 });
  assert.match(calls[1].args.at(-1), /delay\(0\.001\)/);
  assert.match(calls[2].args.at(-1), /kCGEventLeftMouseUp/);
  await assert.rejects(() => d.click({ x: 1, y: 2, interval_ms: -1 }), (e) => e.code === "COMMAND_REJECTED");
  await assert.rejects(() => d.drag({ from: { x: 1, y: 2 }, to: { x: 3, y: 4 }, steps: 200, interval_ms: 100 }), (e) => e.code === "COMMAND_REJECTED");
});
