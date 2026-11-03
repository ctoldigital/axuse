/**
 * Posture audit — the productized form of every posture claim, plus the
 * governance caps that keep massive-ops processes from OOM-killing the box.
 *
 *  - unit (always): judgePosture is pure — every verdict path (pass/warn/
 *    fail, persona matches and mismatches, egress geo vs clock) is pinned.
 *  - live (browser): `axuse posture` end-to-end in full server mode — the
 *    same facts the doctor smoke prints, machine-checkable; the browser cap
 *    fails typed instead of launching the box into the OOM killer.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { judgePosture, publicIpCandidates } from "../src/workflows/posture.js";
import { locateBrowser } from "../src/core/cdp.js";
import { Controller } from "../src/core/controller.js";

const GOOD_FACTS = {
  ua: "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
  webdriver: false, webdriverNative: true,
  languages: ["en-US", "en"], intl: "en-US", tz: "America/New_York",
  cores: 8, memory: 8,
  webgl: true, renderer: "ANGLE (SwiftShader)",
  viewport: { w: 1919, h: 992, ow: 1920, oh: 1080, dpr: 1 },
  screen: { w: 1920, h: 1080, depth: 24, avail: 1080 },
  plugins: 5, pdfViewer: true, notifications: "denied", audioOffline: true,
};
const by = (report) => Object.fromEntries(report.checks.map((c) => [c.id, c.status]));

test("posture: a healthy server-lane browser passes every critical check", () => {
  const r = judgePosture({ facts: GOOD_FACTS, posture: { posture: "headed-on-virtual-display", display: ":42" }, egress: null }, { AXUSE_TIMEZONE: "America/New_York", AXUSE_LANG: "en-US" });
  const s = by(r);
  assert.equal(s["user-agent"], "pass");
  assert.equal(s["navigator.webdriver"], "pass");
  assert.equal(s["webgl"], "pass");
  assert.equal(s["timezone"], "pass");
  assert.equal(s["language"], "pass");
  assert.equal(s["pdf-plugins"], "pass");
  assert.equal(r.failures, 0);
});

test("posture: the headless class fails loudly", () => {
  const r = judgePosture({ facts: { ...GOOD_FACTS, ua: GOOD_FACTS.ua.replace("Chrome/151", "HeadlessChrome/151") }, posture: {} }, {});
  assert.equal(by(r)["user-agent"], "fail");
  assert.ok(r.failures >= 1);
  const wd = judgePosture({ facts: { ...GOOD_FACTS, webdriver: true }, posture: {} }, {});
  assert.equal(by(wd)["navigator.webdriver"], "fail");
  const noWebgl = judgePosture({ facts: { ...GOOD_FACTS, webgl: false, renderer: null }, posture: {} }, {});
  assert.equal(by(noWebgl)["webgl"], "fail");
});

test("posture: the masked-fallback script is a warn (build ignored the flag), never a silent pass", () => {
  const r = judgePosture({ facts: { ...GOOD_FACTS, webdriverNative: false }, posture: {} }, {});
  assert.equal(by(r)["navigator.webdriver"], "warn");
  assert.match(r.checks.find((c) => c.id === "navigator.webdriver").detail, /fallback script/);
});

test("posture: persona mismatches are diagnosed, not just failed", () => {
  const tz = judgePosture({ facts: { ...GOOD_FACTS, tz: "UTC" }, posture: {} }, { AXUSE_TIMEZONE: "America/New_York" });
  assert.equal(by(tz)["timezone"], "fail");
  assert.match(tz.checks.find((c) => c.id === "timezone").detail, /persona broken/);
  // Intl mismatch carries the locale-gen hint (the LC_ALL mechanism).
  const lang = judgePosture({ facts: { ...GOOD_FACTS, intl: "en-US", languages: ["en-GB"] }, posture: {} }, { AXUSE_LANG: "en-GB" });
  assert.equal(by(lang)["language"], "warn");
  assert.match(lang.checks.find((c) => c.id === "language").detail, /locale-gen/);
});

test("posture: egress geo vs clock — the fleet misconfiguration detector", () => {
  // Same area: pass.
  const ok = judgePosture({ facts: GOOD_FACTS, posture: {}, egress: { ip: "1.2.3.4", geo: { timezone: "America/Chicago", country: "United States" } } }, { AXUSE_TIMEZONE: "America/New_York" });
  assert.equal(by(ok)["egress"], "pass");
  // Different area: warn with both facts (caught live during development:
  // a Swiss exit behind an America/New_York persona).
  const bad = judgePosture({ facts: GOOD_FACTS, posture: {}, egress: { ip: "1.2.3.4", geo: { timezone: "Europe/Zurich", country: "Switzerland" } } }, { AXUSE_TIMEZONE: "America/New_York" });
  assert.equal(by(bad)["egress"], "warn");
  assert.match(bad.checks.find((c) => c.id === "egress").detail, /does not match the exit/);
  // Echo unreachable: info, never fail — the network says nothing about posture.
  const offline = judgePosture({ facts: GOOD_FACTS, posture: {}, egress: { ip: null, note: "echo unreachable: x" } }, {});
  assert.equal(by(offline)["egress"], "info");
});

test("posture: webrtc classification — mDNS/private candidates never leak, public IPs do", () => {
  assert.deepEqual(publicIpCandidates([
    { ip: "904bc723-6ac3-47e4-9c8e-f390cc12caea.local", typ: "host" },
    { ip: "192.168.1.5", typ: "host" },
    { ip: "fe80::1", typ: "host" },
  ]), [], "mDNS and private IPs are a real VPN shape, not leaks");
  assert.deepEqual(publicIpCandidates([{ ip: "79.127.184.170", typ: "srflx" }]), [{ ip: "79.127.184.170", typ: "srflx" }]);
  // IPv6 is not uniformly "local": only link-local/ULA/loopback are a VPN
  // shape. A routable IPv6 address is a leak exactly like a public IPv4 one.
  assert.deepEqual(publicIpCandidates([
    { ip: "::1", typ: "host" },
    { ip: "fe80::a1b2:c3d4", typ: "host" },
    { ip: "fc00::1", typ: "host" },
    { ip: "fdab:cdef::1", typ: "host" },
  ]), [], "IPv6 loopback, link-local and unique-local are private, not leaks");
  assert.deepEqual(
    publicIpCandidates([{ ip: "2001:4860:4860::8888", typ: "srflx" }]),
    [{ ip: "2001:4860:4860::8888", typ: "srflx" }],
    "a routable (public) IPv6 candidate is a leak beside a proxied exit",
  );
  // The judge: leak → warn beside a proxied exit; clean → pass; unprobed → info.
  const proxied = { posture: "headed", egress: "http://1.2.3.4:8080" };
  const leak = judgePosture({ facts: { ...GOOD_FACTS, webrtcSupported: true }, posture: proxied, webrtc: { supported: true, candidates: [{ ip: "79.127.184.170", typ: "srflx" }] } }, {});
  assert.equal(by(leak)["webrtc"], "warn");
  assert.match(leak.checks.find((c) => c.id === "webrtc").detail, /LEAK/);
  const clean = judgePosture({ facts: { ...GOOD_FACTS, webrtcSupported: true }, posture: proxied, webrtc: { supported: true, candidates: [{ ip: "abc.local", typ: "host" }] } }, {});
  assert.equal(by(clean)["webrtc"], "pass");
  const unprobed = judgePosture({ facts: { ...GOOD_FACTS, webrtcSupported: true }, posture: proxied, webrtc: null }, {});
  assert.equal(by(unprobed)["webrtc"], "info");
  // Unproxied lanes never get the check (their IP is honestly theirs).
  assert.equal(by(judgePosture({ facts: GOOD_FACTS, posture: { posture: "headed" } }, {}))["webrtc"], undefined);
});

test("posture: per-instance persona drives the judge (before process env)", () => {
  const persona = judgePosture(
    { facts: { ...GOOD_FACTS, tz: "Asia/Tokyo", languages: ["ja-JP"], intl: "ja-JP" }, posture: { posture: "headed", persona: { timezone: "Asia/Tokyo", lang: "ja-JP" } } },
    { AXUSE_TIMEZONE: "America/New_York", AXUSE_LANG: "en-US" },
  );
  assert.equal(by(persona)["timezone"], "pass", "instance persona wins over the process env");
  assert.equal(by(persona)["language"], "pass");
  assert.match(persona.checks.find((c) => c.id === "timezone").detail, /instance persona/);
});

const hasBrowser = !!locateBrowser();
const linux = process.platform === "linux";
const xvfb = linux && (await import("node:child_process")).spawnSync("which", ["Xvfb"]).status === 0;
const liveGate = hasBrowser && linux && xvfb;
const liveSkip = liveGate ? false : "requires Linux + Xvfb + a Chromium-family browser (the server-mode audit lane)";

test("posture: the live command audits the full server lane (json mode)", { skip: liveSkip }, async (t) => {
  const saved = { DISPLAY: process.env.DISPLAY, TZ: process.env.TZ };
  process.env.DISPLAY = ":988"; // dead → server mode boots the virtual display
  process.env.AXUSE_TIMEZONE = "America/New_York";
  t.after(() => {
    process.env.DISPLAY = saved.DISPLAY;
    delete process.env.AXUSE_TIMEZONE;
    if (saved.TZ === undefined) delete process.env.TZ; else process.env.TZ = saved.TZ;
  });
  const { collectPosture, judgePosture } = await import("../src/workflows/posture.js");
  const c = new Controller();
  t.after(() => c.dispose());
  const collected = await collectPosture(c);
  const report = judgePosture(collected, process.env);
  const s = by(report);
  assert.equal(collected.posture.posture, "headed-on-virtual-display");
  assert.match(collected.posture.display, /^:\d+$/);
  assert.equal(s["user-agent"], "pass");
  assert.equal(s["navigator.webdriver"], "pass", "native getter, zero tampering");
  assert.equal(s["webgl"], "pass", "SwiftShader on the virtual display");
  assert.equal(s["timezone"], "pass");
  assert.equal(report.failures, 0, JSON.stringify(report.checks.filter((x) => x.status === "fail")));
});

test("governance: the browser cap fails typed instead of OOM-killing the box", { skip: !hasBrowser ? "live-gate-browser: needs any Chromium-family browser" : false }, async (t) => {
  const prev = process.env.AXUSE_MAX_BROWSERS;
  process.env.AXUSE_MAX_BROWSERS = "1";
  t.after(() => { if (prev === undefined) delete process.env.AXUSE_MAX_BROWSERS; else process.env.AXUSE_MAX_BROWSERS = prev; });
  const c = new Controller();
  t.after(() => c.dispose());
  await c.execute("open", { url: "about:blank", headless: true, settle_ms: 100 });
  // Same launch key → reuse, never capped.
  const again = await c.execute("open", { url: "about:blank", headless: true, settle_ms: 100 });
  assert.ok(again.tab_id);
  // A DIFFERENT key (different proxy) at the cap → typed rejection.
  await assert.rejects(
    () => c.execute("open", { url: "about:blank", headless: true, proxy: "http://127.0.0.1:9", settle_ms: 100 }),
    (e) => e.code === "COMMAND_REJECTED" && /Browser cap reached/.test(e.message),
  );
});

test("per-instance persona: one process, two browsers, two coherent personas", { skip: !hasBrowser ? "live-gate-browser: needs any Chromium-family browser" : false }, async (t) => {
  const c = new Controller();
  t.after(() => c.dispose());
  const readPersona = async () => JSON.parse((await c.execute("evaluate", {
    expression: "({tz: Intl.DateTimeFormat().resolvedOptions().timeZone, lang: navigator.language, intl: Intl.DateTimeFormat().resolvedOptions().locale})",
  })).value ?? "{}");

  const tokyo = await c.execute("open", { url: "about:blank", headless: true, timezone: "Asia/Tokyo", lang: "en-GB", settle_ms: 150 });
  const p1 = await readPersona();
  assert.equal(p1.tz, "Asia/Tokyo");
  assert.equal(p1.lang, "en-GB");
  assert.equal(p1.intl, "en-GB");
  assert.deepEqual(c.browserPosture().persona, { timezone: "Asia/Tokyo", lang: "en-GB" });

  const ny = await c.execute("open", { url: "about:blank", headless: true, timezone: "America/New_York", lang: "en-US", settle_ms: 150 });
  assert.notEqual(ny.browser_id, tokyo.browser_id, "a different persona is a different browser, never a mutation");
  const p2 = JSON.parse((await c.execute("evaluate", {
    expression: "({tz: Intl.DateTimeFormat().resolvedOptions().timeZone, lang: navigator.language, intl: Intl.DateTimeFormat().resolvedOptions().locale})",
    tab_id: ny.tab_id, // two tabs are controlled now — the strict lane requires it
  })).value ?? "{}");
  assert.equal(p2.tz, "America/New_York");
  assert.equal(p2.lang, "en-US");

  // Same persona again → same browser (the reuse key includes the persona).
  const again = await c.execute("open", { url: "about:blank", headless: true, timezone: "Asia/Tokyo", lang: "en-GB", settle_ms: 100 });
  assert.equal(again.browser_id, tokyo.browser_id);
});

test("webrtc: a proxied launch leaks no public ICE candidate (the profile-pref fix)", { skip: !hasBrowser ? "live-gate-browser: needs any Chromium-family browser" : false }, async (t) => {
  // A working CONNECT tunnel — the production proxy shape. (The pref fix
  // suppresses non-proxied paths; whether the tunnel carries STUN or not,
  // NO public candidate may appear beside the proxied persona.)
  const { default: http } = await import("node:http");
  const { default: net } = await import("node:net");
  const proxy = http.createServer((req, res) => { res.writeHead(403); res.end(); });
  proxy.on("connect", (req, clientSocket, head) => {
    const [host, port] = req.url.split(":");
    const upstream = net.connect(Number(port) || 443, host, () => {
      clientSocket.write("HTTP/1.1 200 Connection Established\r\n\r\n");
      upstream.write(head);
      upstream.pipe(clientSocket);
      clientSocket.pipe(upstream);
    });
    upstream.on("error", () => clientSocket.destroy());
    clientSocket.on("error", () => upstream.destroy());
  });
  await new Promise((ok) => proxy.listen(0, "127.0.0.1", ok));
  t.after(() => proxy.close());
  const { WEBRTC_EXPRESSION } = await import("../src/workflows/posture.js");
  const c = new Controller();
  t.after(() => c.dispose());
  await c.execute("open", {
    url: "data:text/html,<h1>rtc</h1>",
    headless: true,
    proxy: `http://127.0.0.1:${proxy.address().port}`,
    settle_ms: 200,
  });
  const webrtc = JSON.parse((await c.execute("evaluate", { expression: WEBRTC_EXPRESSION })).value ?? "null");
  assert.ok(webrtc?.supported, "RTCPeerConnection present (the surface itself is not removed)");
  const leak = publicIpCandidates(webrtc.candidates ?? []);
  assert.deepEqual(leak, [], `no public candidate beside the proxied exit (got ${JSON.stringify(leak)})`);
});

test("open_many: a batch carries one coherent persona (clock + language)", { skip: !hasBrowser ? "live-gate-browser: needs any Chromium-family browser" : false }, async (t) => {
  const c = new Controller();
  t.after(() => c.dispose());
  const batch = await c.execute("open_many", {
    urls: ["about:blank"],
    headless: true,
    timezone: "Asia/Tokyo",
    lang: "en-GB",
    close_after: false,
    settle_ms: 120,
  });
  assert.equal(batch.opened, 1);
  const p = JSON.parse((await c.execute("evaluate", {
    expression: "({tz: Intl.DateTimeFormat().resolvedOptions().timeZone, lang: navigator.language, intl: Intl.DateTimeFormat().resolvedOptions().locale})",
  })).value ?? "{}");
  assert.equal(p.tz, "Asia/Tokyo", "the batch clock matches the persona");
  assert.equal(p.lang, "en-GB");
  assert.match(p.intl, /^en[-_]GB/, "Intl follows the persona (LC_ALL derivation)");
  assert.deepEqual(c.browserPosture().persona, { timezone: "Asia/Tokyo", lang: "en-GB" });
});

test("the console-getter CDP-attach detector does not fire (measured; pins the docs claim)", { skip: !hasBrowser ? "live-gate-browser: needs any Chromium-family browser" : false }, async (t) => {
  // The classic detector (console.log of a getter-carrying object) targets
  // the DevTools UI, not the raw protocol. Measured on Chrome 151 with our
  // exact attach sequence: the getter is NOT invoked during
  // consoleAPICalled serialization. This pins the measured fact so the
  // docs claim cannot silently rot.
  const c = new Controller();
  t.after(() => c.dispose());
  await c.execute("open", { url: "data:text/html,<h1>console detector</h1>", headless: true, settle_ms: 120 });
  const fired = JSON.parse((await c.execute("evaluate", {
    expression: `(async () => {
      let fired = false;
      const trap = { get x() { fired = true; return 1; } };
      console.log(trap);
      await new Promise((r) => setTimeout(r, 300));
      return fired;
    })()`,
  })).value ?? "null");
  assert.equal(fired, false, "raw CDP console serialization must not invoke getters");
});
