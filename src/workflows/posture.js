/**
 * axuse — posture audit: one command that measures, in a REAL launched
 * browser, every fact the server lane promises — the same assertions the
 * test suite pins, productized for fleet operators and CI.
 *
 *   node src/cli.js posture [--json] [--verify-egress]
 *
 * Design: collectPosture() gathers in-page facts through a Controller
 * (server mode boots the virtual display exactly as production would);
 * judgePosture() is PURE (facts × env → verdicts) so the rules are
 * unit-testable; formatPosture() renders human or JSON. Exit code 1 when
 * any FAIL exists — CI-able (`linux-server` lane runs it).
 *
 * `--verify-egress` closes the persona loop: it fetches an IP echo THROUGH
 * THE BROWSER (through the proxy when one is configured) and, when a geo
 * service answers, compares the exit's timezone with AXUSE_TIMEZONE — a
 * UTC server behind a US exit is the classic mismatch, caught here instead
 * of by the challenge page. Network-gated parts are INFO on failure, never
 * FAIL: an unreachable echo service says nothing about the posture.
 */

const POSTURE_EXPRESSION = `(() => {
  const g = document.createElement("canvas").getContext("webgl");
  const dbg = g && g.getExtension("WEBGL_debug_renderer_info");
  const wd = Object.getOwnPropertyDescriptor(Navigator.prototype, "webdriver") || {};
  let audioOffline = true;
  try {
    const ctx = new OfflineAudioContext(1, 4096, 44100);
    const o = ctx.createOscillator(); o.connect(ctx.destination); o.start(0);
  } catch (e) { audioOffline = false; }
  let webrtcSupported = false;
  try { webrtcSupported = typeof RTCPeerConnection !== "undefined"; } catch (e) {}
  return {
    ua: navigator.userAgent,
    webdriver: navigator.webdriver,
    webdriverNative: String(wd.get ?? "").includes("[native code]"),
    languages: (navigator.languages || []).slice(0, 3),
    intl: Intl.DateTimeFormat().resolvedOptions().locale,
    tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
    cores: navigator.hardwareConcurrency,
    memory: typeof navigator.deviceMemory === "number" ? navigator.deviceMemory : null,
    webgl: !!g,
    renderer: dbg ? g.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
    viewport: { w: innerWidth, h: innerHeight, ow: outerWidth, oh: outerHeight, dpr: devicePixelRatio },
    screen: { w: screen.width, h: screen.height, depth: screen.colorDepth, avail: screen.availHeight },
    plugins: navigator.plugins ? navigator.plugins.length : -1,
    pdfViewer: navigator.pdfViewerEnabled === true,
    notifications: (typeof Notification !== "undefined" && Notification.permission) || "unsupported",
    audioOffline,
    webrtcSupported,
  };
})()`;

/** WebRTC leak probe (proxied lanes): gather ICE candidates and classify
 *  them — mDNS host candidates (.local) are fine (real Chrome behind a VPN
 *  shows those too); any PUBLIC IP (srflx/relay of the real interface)
 *  beside a proxied HTTP persona is the classic mismatch leak. Bounded:
 *  never waits longer than ~4 s for the ICE state. Exported for tests. */
export const WEBRTC_EXPRESSION = `(async () => {
  if (typeof RTCPeerConnection === "undefined") return { supported: false, candidates: [] };
  const cands = [];
  const pc = new RTCPeerConnection({ iceServers: [{ urls: "stun:stun.l.google.com:19302" }] });
  try {
    pc.createDataChannel("axuse-posture");
    const gathered = new Promise((done) => {
      pc.onicecandidate = (e) => {
        if (!e.candidate) { done(); return; }
        const m = /candidate:\\S+ \\d+ \\S+ \\d+ (\\S+) \\d+ typ (\\w+)/.exec(e.candidate.candidate || "");
        if (m) cands.push({ ip: m[1], typ: m[2] });
      };
      setTimeout(done, 4000);
    });
    await pc.setLocalDescription(await pc.createOffer());
    await Promise.race([gathered, new Promise((r) => setTimeout(r, 4500))]);
  } catch (e) { /* ICE unavailable — reported as empty */ }
  try { pc.close(); } catch (e) {}
  return { supported: true, candidates: cands };
})()`;

/** fe80::/10 (link-local), fc00::/7 (unique-local/ULA) and ::1 (loopback) —
 *  the private IPv6 ranges. Anything else with a colon is a routable IPv6
 *  address and, same as a public IPv4, a real leak beside a proxied exit. */
function isPrivateIPv6(ip) {
  const lc = ip.toLowerCase();
  if (lc === "::1" || lc === "::") return true;
  const first = lc.split(":")[0];
  return /^fe[89ab]/.test(first) || /^f[cd]/.test(first);
}

/** Private-IP and mDNS classification: a candidate is a PERSONA CONTRADICTION
 *  only when it carries a public, non-mDNS address next to a proxied exit. */
export function publicIpCandidates(candidates) {
  return (candidates ?? []).filter((c) => {
    const ip = String(c.ip ?? "");
    if (ip.endsWith(".local")) return false; // mDNS host candidate
    // IPv6 — every address contains a colon, but only the private ranges
    // (link-local, ULA, loopback) are a VPN shape; a routable IPv6 address
    // beside a proxied exit leaks the real interface exactly like IPv4 does.
    if (ip.includes(":")) return !isPrivateIPv6(ip);
    const m = /^(\d+)\.(\d+)\.(\d+)\.(\d+)$/.exec(ip);
    if (!m) return false;
    const [a, b] = [Number(m[1]), Number(m[2])];
    if (a === 10 || a === 127 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 169 && b === 254)) return false;
    return true;
  });
}

const EGRESS_EXPRESSION = `(async () => {
  try {
    const echo = await fetch(${JSON.stringify(process.env.AXUSE_EGRESS_ECHO_URL ?? "https://api.ipify.org?format=json")}, { cache: "no-store" });
    const j = await echo.json();
    return { ip: j.ip ?? j.query ?? null };
  } catch (e) { return { ip: null, error: String(e) }; }
})()`;

const GEO_URL = (ip) => (process.env.AXUSE_EGRESS_GEO_URL ?? `https://ipapi.co/{ip}/json/`).replace("{ip}", encodeURIComponent(ip));

/** Measure the live posture of the controller's active tab (opens one when
 *  none exists — the production lane, server mode included). When the
 *  browser is PROXIED, also probes the WebRTC surface: the one place a real
 *  interface can leak next to a proxied persona. */
export async function collectPosture(controller) {
  if (!controller.activeTabId) {
    await controller.execute("open", { url: "about:blank", settle_ms: 150 });
  }
  const facts = JSON.parse((await controller.execute("evaluate", { expression: POSTURE_EXPRESSION })).value ?? "null");
  const browser = [...controller.browsers.values()].at(-1);
  let webrtc = null;
  if (browser?.egress && facts?.webrtcSupported) {
    webrtc = JSON.parse((await controller.execute("evaluate", { expression: WEBRTC_EXPRESSION })).value ?? "null");
  }
  return { facts, webrtc, posture: controller.browserPosture(), browserId: browser?.browserId ?? null };
}

/** In-browser egress check (opt-in): the IP the EXIT sees, and — when a geo
 *  service answers — the exit's timezone for the persona comparison. */
export async function collectEgress(controller) {
  // $evaluate awaits in-page promises by contract — no extra param needed.
  const out = await controller.execute("evaluate", { expression: EGRESS_EXPRESSION });
  const echo = JSON.parse(out.value ?? "null") ?? {};
  if (!echo.ip) return { ip: null, note: echo.error ? `echo unreachable: ${String(echo.error).slice(0, 80)}` : "no ip" };
  let geo = null;
  try {
    const res = await controller.execute("evaluate", {
      expression: `(async () => { try { const r = await fetch(${JSON.stringify(GEO_URL(echo.ip))}, { cache: "no-store" }); return await r.json(); } catch (e) { return null; } })()`,
    });
    geo = JSON.parse(res.value ?? "null");
  } catch { /* geo service optional */ }
  return { ip: echo.ip, geo: geo ? { timezone: geo.timezone ?? null, country: geo.country_name ?? geo.country ?? null } : null };
}

const langPrefix = (v) => String(v ?? "").toLowerCase().split(/[-_]/)[0];
/** The REGION subtag when present ("en-GB" → "GB"; "de" → "" — a bare
 *  language is legitimate and regionless). */
const regionOf = (v) => {
  const m = /[-_]([A-Za-z]{2})$/.exec(String(v ?? "").trim());
  return m ? m[1].toUpperCase() : "";
};

/** Pure: facts × env → verdicts. The rules the tests pin, in one place.
 *  Persona expectations resolve PER-INSTANCE first (open {timezone, lang} /
 *  the browser record's persona), then process env, then info. */
export function judgePosture({ facts, posture, egress, webrtc }, env = process.env) {
  const checks = [];
  const add = (id, status, detail) => checks.push({ id, status, detail });
  if (!facts) {
    add("facts", "fail", "no in-page facts collected — the browser lane is broken");
    return { checks, failures: 1, warnings: 0 };
  }
  const persona = posture?.persona ?? {};
  // Critical: the headless class of signals.
  add("user-agent", /headless/i.test(facts.ua) ? "fail" : "pass",
    /headless/i.test(facts.ua) ? "UA contains Headless — the exact first-step detection signal" : `headed (${facts.ua.replace(/^.*?\(([^)]*\)).*$/, "$1")})`);
  const wdOk = facts.webdriver === false;
  add("navigator.webdriver", !wdOk ? "fail" : (facts.webdriverNative ? "pass" : "warn"),
    !wdOk ? "navigator.webdriver is true — automation is page-visible"
      : facts.webdriverNative ? "false via a NATIVE getter (the launch flag; zero tampering)"
        : "false via the masked fallback script — this build ignored the AutomationControlled flag");
  add("webgl", facts.webgl ? "pass" : "fail",
    facts.webgl ? `present (${String(facts.renderer).slice(0, 60)})` : "absent — a strong bot signal on GPU-less hosts (--enable-unsafe-swiftshader should restore it)");
  add("pdf-plugins", facts.pdfViewer && facts.plugins > 0 ? "pass" : "warn",
    facts.pdfViewer && facts.plugins > 0 ? `${facts.plugins} plugins, PDF viewer enabled (headed build shape)` : "no PDF plugin — headless-like plugin surface");
  // Persona coherence: clock, languages, viewport (per-instance first).
  const wantTz = persona.timezone ?? env.AXUSE_TIMEZONE;
  if (wantTz) {
    add("timezone", facts.tz === wantTz ? "pass" : "fail",
      facts.tz === wantTz ? `${facts.tz} (matches ${persona.timezone ? "the instance persona" : "AXUSE_TIMEZONE"})` : `page reports ${facts.tz}, expected ${wantTz} — persona broken`);
  } else {
    add("timezone", "info", `${facts.tz} (host default — set a per-instance timezone or AXUSE_TIMEZONE to the egress persona's zone)`);
  }
  const wantLang = persona.lang ?? env.AXUSE_LANG;
  if (wantLang) {
    const langOk = langPrefix(facts.languages[0]) === langPrefix(wantLang) && regionOf(facts.languages[0] ?? "") === regionOf(wantLang);
    // Intl can legitimately drop the region (measured: LC_ALL=de_DE.UTF-8 →
    // "de"), so only mismatch when BOTH sides carry a region and they
    // differ (en-US Intl under an en-GB persona = LC_ALL never took).
    const intlRegion = regionOf(facts.intl);
    const wantRegion = regionOf(wantLang);
    const intlOk = langPrefix(facts.intl) === langPrefix(wantLang)
      && (!intlRegion || !wantRegion || intlRegion === wantRegion);
    add("language", langOk && intlOk ? "pass" : "warn",
      `navigator.languages=[${facts.languages.join(", ")}] · Intl=${facts.intl} vs AXUSE_LANG=${wantLang}`
      + (!intlOk ? " — Intl mismatch: is the locale generated on this host? (sudo locale-gen <tag>.UTF-8)" : ""));
  } else {
    add("language", "info", `navigator.languages=[${facts.languages.join(", ")}] · Intl=${facts.intl} (host defaults)`);
  }
  const vd = posture && typeof posture === "object" ? posture : {};
  add("browser-shape", "info", `${vd.posture ?? "unknown"}${vd.display ? ` on ${vd.display}` : ""}${vd.egress ? ` · egress ${vd.egress}` : ""}${vd.persona?.timezone || vd.persona?.lang ? ` · persona ${[vd.persona.timezone, vd.persona.lang].filter(Boolean).join("/")}` : ""}`);
  // WebRTC (proxied lanes only — the one place a real interface can leak
  // next to a proxied persona; unproxied lanes honestly show their IP).
  if (vd.egress) {
    if (!facts.webrtcSupported) {
      add("webrtc", "info", "RTCPeerConnection unavailable on this build");
    } else if (!webrtc) {
      add("webrtc", "info", "not probed");
    } else {
      const leak = publicIpCandidates(webrtc.candidates);
      add("webrtc", leak.length ? "warn" : "pass",
        leak.length
          ? `LEAK: public WebRTC candidate(s) ${leak.map((c) => `${c.ip} (${c.typ})`).join(", ")} beside the proxied exit ${vd.egress} — disable_non_proxied_udp should suppress these (build ignored the policy?)`
          : "no public candidates beside the proxied exit (mDNS host candidates only — a real VPN shape)");
    }
  }
  add("fingerprint-surface", "info",
    `cores=${facts.cores} memory=${facts.memory ?? "n/a"} viewport=${facts.viewport.w}x${facts.viewport.h}@dpr${facts.viewport.dpr} screen=${facts.screen.w}x${facts.screen.h}x${facts.screen.depth}`);
  add("audio", facts.audioOffline ? "pass" : "warn",
    facts.audioOffline ? "OfflineAudioContext renders (the audio-fingerprint vector is healthy)" : "OfflineAudioContext construction failed");
  add("notifications", "info", `Notification.permission=${facts.notifications}`);
  if (egress) {
    if (!egress.ip) {
      add("egress", "info", egress.note ?? "echo unreachable");
    } else if (egress.geo?.timezone && wantTz) {
      const geoTz = egress.geo.timezone;
      const same = geoTz === wantTz || geoTz.split("/")[0] === wantTz.split("/")[0];
      add("egress", same ? "pass" : "warn",
        `exit IP ${egress.ip} (${egress.geo.country ?? "?"}) → geo tz ${geoTz} vs AXUSE_TIMEZONE=${wantTz}${same ? "" : " — the clock does not match the exit's geography"}`);
    } else {
      add("egress", "info", `exit IP ${egress.ip}${egress.geo?.country ? ` (${egress.geo.country})` : ""} (geo ${egress.geo?.timezone ? "answered" : "unavailable"})`);
    }
  }
  const failures = checks.filter((c) => c.status === "fail").length;
  const warnings = checks.filter((c) => c.status === "warn").length;
  return { checks, failures, warnings };
}

/** Human (table-ish) or machine (JSON) rendering. */
export function formatPosture(report, { json = false, browser, egress } = {}) {
  if (json) {
    return JSON.stringify({
      ok: report.failures === 0,
      failures: report.failures,
      warnings: report.warnings,
      browser: browser ?? null,
      checks: report.checks,
    }, null, 2);
  }
  const icon = { pass: "✓", fail: "✗", warn: "!", info: "·" };
  const lines = [
    browser ? `browser: ${browser}` : null,
    ...report.checks.map((c) => `${icon[c.status] ?? "?"} ${c.id}: ${c.detail}`),
    "",
    report.failures ? `POSTURE: FAIL (${report.failures} failing, ${report.warnings} warnings)` : `POSTURE: ${report.warnings ? `OK with ${report.warnings} warning(s)` : "OK"}`,
  ].filter((l) => l !== null);
  return lines.join("\n");
}
