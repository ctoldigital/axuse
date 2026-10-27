/**
 * axuse — desktop (computer use) driver layer.
 *
 * Lineage: ZCode's desktop CUA is an AX-tree-first actuator behind a native
 * permission-brokered Helper process; on Windows it uses a native N-API
 * addon. axuse stays zero-dependency by using each OS's scriptable surface:
 *
 *   - The BROWSER (ax_*) is structure-first: refs, diffed AX snapshots,
 *     trusted input. Use it whenever the target is a web page.
 *   - The DESKTOP (ax_desktop_*) is now ALSO structure-first on macOS:
 *     System Events exposes the platform AX tree (same Accessibility grant
 *     as input synthesis), so ax_desktop_ax_snapshot gives refs, roles,
 *     titles, values and bounds; ax_desktop_ax_click/set/read act on them
 *     (trusted AXPress with a CGEvent coordinate fallback). Coordinates and
 *     screenshots remain the fallback mode for canvas/games — and the only
 *     mode on Windows today.
 *
 * Driver matrix (fail-closed where permissions are not granted):
 *   - Windows: resident PowerShell + SendInput/GDI driver — verified live
 *     (coordinate+vision; no AX tree yet).
 *   - macOS:   JXA/CoreGraphics/System Events + screencapture/sips driver;
 *     loads with AXUSE_ENABLE_MACOS_CUA=1; reports TCC failures clearly.
 *     AX-tree ops need the Accessibility grant; capture needs Screen
 *     Recording.
 *   - Linux:   zero-dependency X11/XWayland driver (src/desktop/linux.js):
 *     XTEST FakeInput for trusted input, GetImage+baseline JPEG for
 *     capture, EWMH for the window list and activation. Mounts whenever
 *     DISPLAY is set (verified on Xorg 1.21 / Ubuntu 22.04+ and Xvfb);
 *     native Wayland sessions without XWayland fail typed with guidance.
 *     Coordinate+vision mode, like Windows — no AT-SPI AX tree in v1.
 *
 * The driver interface is the seam: a Windows UIA driver slots in behind
 * the same tools without touching the contract.
 */
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { join, dirname } from "node:path";
import { AxError, Codes } from "../core/errors.js";
import { KEYS, MOD, parseCombo } from "../core/actuate.js";
import { DEFAULT_BUDGET_BYTES } from "../core/screenshot.js";

const PS1 = join(dirname(fileURLToPath(import.meta.url)), "windows.ps1");
const OP_TIMEOUT_MS = 15_000;

/** Exact payload size of a base64 string (padding-aware; the ×¾/4 estimate
 *  over-reports by up to 2 bytes per image and skews budget decisions). */
function exactBase64Bytes(b64) {
  return Buffer.byteLength(Buffer.from(b64, "base64"));
}

/** Pointer-event pacing validation, mirroring the macOS driver's timing
 *  controls: 0–1000 ms between events, typed COMMAND_REJECTED otherwise. */
function pointerInterval(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 1000) {
    throw new AxError(Codes.COMMAND_REJECTED, "interval_ms must be between 0 and 1000", { retry: "never" });
  }
  return value;
}

/** Resident Windows driver: one PowerShell process, one op at a time. */
class WindowsDriver {
  constructor() {
    this.proc = null;
    this.dead = true; // set synchronously on exit AND on dispose
    this.chain = Promise.resolve(); // input ops are strictly ordered
  }

  ensure() {
    // Respawn only when the old process is really gone. `exitCode` alone
    // is racy (it stays null until the exit event is processed), so
    // `dead` is set synchronously in dispose() and in the exit handler.
    if (this.proc && !this.dead && this.proc.exitCode === null) return;
    this.dead = false;
    const proc = spawn("powershell", ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", PS1], {
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.proc = proc;
    this.buffer = "";
    this.waiters = [];
    // Guard every handler against superseded processes: the OLD proc's
    // events can arrive after a respawn and would poison the new state.
    proc.stdout.on("data", (d) => { if (this.proc === proc) this.onStdout(d); });
    proc.stderr.on("data", (d) => {
      if (this.proc !== proc) return;
      const s = String(d).trim();
      if (s) process.stderr.write(`[axuse:desktop] ${s}\n`);
    });
    proc.on("exit", () => {
      if (this.proc !== proc) return; // a newer process owns this driver
      this.dead = true;
      for (const w of this.waiters.splice(0)) w.reject(new AxError(Codes.DESKTOP_OP_FAILED, "Desktop driver exited"));
    });
  }

  onStdout(d) {
    this.buffer += String(d);
    let idx;
    while ((idx = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, idx).trim();
      this.buffer = this.buffer.slice(idx + 1);
      if (!line) continue;
      const w = this.waiters.shift();
      if (!w) continue;
      try {
        w.resolve(JSON.parse(line));
      } catch {
        w.reject(new AxError(Codes.DESKTOP_OP_FAILED, `Driver protocol garbage: ${line.slice(0, 200)}`));
      }
    }
  }

  /** Ops are serialized: input ordering must never interleave. */
  op(payload, timeoutMs = OP_TIMEOUT_MS) {
    this.ensure();
    const run = () =>
      new Promise((resolve, reject) => {
        // Ops are strictly serialized, so this op's waiter is the only
        // outstanding one — on timeout, reject and drop the oldest.
        const timer = setTimeout(() => {
          const w = this.waiters.shift();
          if (w) w.reject(new AxError(Codes.TIMEOUT, `Desktop op "${payload.op}" timed out`, { retry: "retry" }));
          else reject(new AxError(Codes.TIMEOUT, `Desktop op "${payload.op}" timed out`, { retry: "retry" }));
        }, timeoutMs);
        this.waiters.push({
          resolve: (v) => { clearTimeout(timer); resolve(v); },
          reject: (e) => { clearTimeout(timer); reject(e); },
        });
        try {
          this.proc.stdin.write(JSON.stringify(payload) + "\n");
        } catch (e) {
          clearTimeout(timer);
          this.waiters.shift();
          reject(new AxError(Codes.DESKTOP_OP_FAILED, `Driver write failed: ${e.message}`));
        }
      });
    this.chain = this.chain.then(run, run);
    return this.chain;
  }

  async check(op) {
    const res = await op;
    if (!res || res.ok === false) {
      throw new AxError(Codes.DESKTOP_OP_FAILED, res?.error ?? "op failed", { retry: "retry" });
    }
    return res;
  }

  /* ------------------------------------------------------------ operations */

  async screenInfo() {
    const info = await this.check(this.op({ op: "screen_info" }));
    // Same field the Linux and macOS drivers report: the agent is told which
    // units x/y/w/h (and every click) are in. The script returns physical px.
    return { ...info, coordinate_space: "Windows virtual-screen physical pixels (spans all monitors; x,y is the top-left)" };
  }

  async screenshot({ x, y, w, h, budgetBytes = DEFAULT_BUDGET_BYTES, quality = 80 } = {}) {
    let res = await this.check(this.op({ op: "screenshot", x, y, w, h, quality }));
    let bytes = exactBase64Bytes(res.base64);
    if (bytes > budgetBytes) {
      res = await this.check(this.op({ op: "screenshot", x, y, w, h, quality: 45 }));
      bytes = exactBase64Bytes(res.base64);
    }
    if (bytes > budgetBytes) {
      throw new AxError(Codes.SCREENSHOT_BUDGET_EXCEEDED,
        `Desktop screenshot is ${bytes} bytes, over the ${budgetBytes} byte budget.`,
        { hint: "Pass a crop region ({x,y,w,h} in physical pixels from screen_info).", retry: "never" });
    }
    return { base64: res.base64, mime: res.mime, bytes, budget_bytes: budgetBytes, quality };
  }

  async move(x, y) {
    await this.check(this.op({ op: "move", x, y }));
    return { action_sent: "sent" };
  }

  async click({ x, y, button = "left", double = false, interval_ms = 0 } = {}) {
    interval_ms = pointerInterval(interval_ms);
    // interval_ms is slept inside the resident PS process between down and up,
    // keeping the press a single atomic gesture (like the macOS JXA delay()).
    await this.check(this.op({ op: "click", x, y, button, double, interval_ms }));
    return { action_sent: "sent" };
  }

  async down(button = "left") {
    await this.check(this.op({ op: "down", button }));
    return { action_sent: "sent" };
  }

  async up(button = "left") {
    await this.check(this.op({ op: "up", button }));
    return { action_sent: "sent" };
  }

  async wheel(dx = 0, dy = 0) {
    await this.check(this.op({ op: "wheel", dx, dy }));
    return { action_sent: "sent" };
  }

  async keyTap(vk) {
    await this.check(this.op({ op: "key_tap", vk }));
    return { action_sent: "sent" };
  }

  async keyDown(vk) {
    await this.check(this.op({ op: "key_down", vk }));
    return { action_sent: "sent" };
  }

  async keyUp(vk) {
    await this.check(this.op({ op: "key_up", vk }));
    return { action_sent: "sent" };
  }

  async type(text) {
    await this.check(this.op({ op: "key_text", text: String(text) }));
    return { action_sent: "sent" };
  }

  async windows() {
    const res = await this.check(this.op({ op: "windows" }));
    return { windows: res.windows };
  }

  async activate({ title, hwnd } = {}) {
    const res = await this.op({ op: "activate", title, hwnd });
    if (!res.ok) {
      throw new AxError(Codes.DESKTOP_OP_FAILED, res.error, { hint: "List windows with ax_desktop_windows and retry with an exact title or hwnd.", retry: "retry" });
    }
    return { activated: true };
  }

  /** Combo press ("ctrl+s") mapped through the shared KEYS table. */
  async press(combo) {
    const { mods, main } = parseCombo(combo);
    for (const m of mods) await this.keyDown(KEYS[m].vk);
    await this.keyTap(main.vk);
    for (let i = mods.length - 1; i >= 0; i--) await this.keyUp(KEYS[mods[i]].vk);
    return { action_sent: "sent", combo };
  }

  /** Drag via move/down/move/up (interpolated like the browser actuator).
   *  interval_ms paces the gesture — after the press, between interpolated
   *  moves, and before the release — mirroring the macOS timing controls.
   *  The steps × interval_ms budget keeps a paced drag bounded in time. */
  async drag({ from, to, steps = 12, interval_ms = 0 } = {}) {
    interval_ms = pointerInterval(interval_ms);
    const requestedSteps = steps;
    if (typeof requestedSteps !== "number" || !Number.isFinite(requestedSteps) || requestedSteps < 1) {
      throw new AxError(Codes.COMMAND_REJECTED, "steps must be ≥ 1", { retry: "never" });
    }
    const count = Math.round(requestedSteps);
    if (count < 1) throw new AxError(Codes.COMMAND_REJECTED, "steps must round to at least 1", { retry: "never" });
    if (count * interval_ms > 12000) throw new AxError(Codes.COMMAND_REJECTED, "steps × interval_ms must be ≤ 12000", { retry: "never" });
    if (count > 200) throw new AxError(Codes.COMMAND_REJECTED, "steps must be ≤ 200", { retry: "never" });
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    await this.move(from.x, from.y);
    await this.down("left");
    if (interval_ms) await sleep(interval_ms);
    for (let i = 1; i <= count; i++) {
      await this.move(
        Math.round(from.x + ((to.x - from.x) * i) / count),
        Math.round(from.y + ((to.y - from.y) * i) / count),
      );
      if (interval_ms) await sleep(interval_ms);
    }
    await this.up("left");
    return { action_sent: "sent" };
  }

  dispose() {
    this.dead = true;
    if (this.proc) { try { this.proc.kill(); } catch {} }
  }
}

let cached = null;

/** Get the platform driver, or null when unsupported. Tools hide when null.
 * macOS remains explicitly opt-in because it can synthesize OS input; the
 * Linux X11 driver mounts when a DISPLAY is configured (same trust level as
 * the always-on Windows console-session driver). */
export function getDesktopDriver() {
  if (process.platform === "win32") {
    if (!cached) cached = new WindowsDriver();
    return cached;
  }
  if (process.platform === "darwin") {
    if (!/^(1|true)$/i.test(process.env.AXUSE_ENABLE_MACOS_CUA ?? "")) return null;
    if (!cached) {
      // Lazy import keeps the permissioned macOS module out of the default path.
      // (Synchronous require is avoided; this is a single controlled load.)
      // eslint-disable-next-line no-async-promise-executor
      cached = macosDriverPromise().catch((e) => {
        cached = null;
        throw e;
      });
    }
    return cached;
  }
  if (process.platform === "linux") {
    if (!process.env.DISPLAY) return null; // no X11/XWayland to drive
    if (!cached) {
      // Lazy import keeps the X11/JPEG modules out of non-Linux paths; the
      // driver resolves through resolveDesktopDriver() like the macOS one.
      cached = import("./linux.js")
        .then(({ LinuxDriver }) => new LinuxDriver())
        .catch((e) => {
          cached = null;
          throw e;
        });
    }
    return cached;
  }
  return null;
}

async function macosDriverPromise() {
  const { MacDriver } = await import("./macos.js");
  return new MacDriver();
}

/** Desktop dispatch needs sync availability in the MCP layer; the macOS
 *  driver is the one async case — pre-resolve it once at server start. */
export async function resolveDesktopDriver() {
  const d = getDesktopDriver();
  return d instanceof Promise ? await d : d;
}

export function desktopUnavailable() {
  const macNote = process.platform === "darwin"
    ? " The macOS driver is disabled: set AXUSE_ENABLE_MACOS_CUA=1; desktop input/capture also needs Accessibility and Screen Recording grants for the host."
    : "";
  const linuxNote = process.platform === "linux" && !process.env.DISPLAY
    ? " The Linux driver speaks X11/XWayland: set DISPLAY (usually :0/:1) — or run under xvfb-run — and the ax_desktop_* tools mount automatically."
    : "";
  throw new AxError(Codes.DESKTOP_DRIVER_UNAVAILABLE,
    process.platform === "darwin"
      ? `Desktop control is disabled on macOS until explicitly enabled.${macNote}`
      : process.platform === "linux"
        ? `Desktop control is unavailable on this Linux session.${linuxNote}`
        : `Desktop control is not implemented for ${process.platform} yet.${macNote}`,
    { hint: "Prefer the ax_* browser tools whenever the target is a web page.", retry: "never" });
}
