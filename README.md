# axuse — browser & computer use, done right

**Accessibility-first browser and computer use for agent harnesses.** One MCP
server plugs into Codex, Claude Code, OpenCode, Antigravity, or any
MCP-capable host. Zero dependencies (Node ≥ 22). Windows desktop driver is
verified; macOS desktop control is available as an explicit opt-in with
native TCC permission checks.

The design is distilled from the two best implementations of this problem —
OpenAI's Codex Chrome extension and the ZCode harness — and exists to prove a
point: **a screenshot loop is not the right interface for a machine**. The
DOM tree, the accessibility tree, and CDP are. axuse instruments the target
instead of photographing it.

If you are a journalist or other non-developer using Codex or ChatGPT Work,
start with the [non-developer onboarding guide](docs/non-developer-onboarding.md).
It explains the one-time connector setup, the first verification prompt, and
copy-and-paste research prompts. The source repository's setup below is for
developers and administrators.

## Proven in production

axuse is maintained by **CTOL Digital Solutions**
([ctol.digital](https://ctol.digital)), a leading AI, Strategy and IT
consultancy. This work has been tested and used at large scale across many
client engagements. CTOL Digital Solutions holds the copyright of all the
code.


## Quick start

You need **Node.js 22+** and a Chromium-family browser (Chrome, Edge,
Chromium, or Chrome for Testing; found automatically, or set
`AXUSE_BROWSER=/path/to/chrome`).
Nothing else: axuse has no npm dependencies.

**1. Get it and check the environment**

```bash
git clone https://github.com/ctoldigital/axuse.git
cd axuse
node src/cli.js doctor        # finds a browser, launches it, takes a snapshot
```

For a daily Chrome/Edge session, use the guided bridge setup. It registers the
native host, opens the extensions page, prints the exact folder to load, and
can print your harness configuration in the same command:

```bash
node src/cli.js setup --browser chrome --harness codex
```

After clicking **Load unpacked** once, verify the connection with:

```bash
node src/cli.js bridge verify
```

**2. Add it to your harness** (use your absolute path to `src/cli.js`)

```bash
# Codex
codex mcp add axuse -- node /abs/path/axuse/src/cli.js mcp

# Claude Code
claude mcp add --scope user axuse -- node /abs/path/axuse/src/cli.js mcp

# OpenCode, Antigravity, any other MCP host: print the config block to paste
node src/cli.js print-config --harness opencode   # or agy | zed | generic
```

**3. Ask for something in the browser**

> Open https://example.com, find the "More information" link and tell me
> where it goes.

The agent calls `ax_open`, reads the accessibility snapshot, and acts on
element refs like `[ref=e7]`. Each action returns only what changed.

**Optional settings** (environment variables for the MCP server)

| variable | effect |
|---|---|
| `AXUSE_HEADLESS=1` | launch the browser without a window (servers, CI) |
| `AXUSE_BROWSER` | path to the browser binary |
| `AXUSE_PROFILE_DIR` | where temporary browser profiles go (default: OS temp; they are deleted on exit) |
| `AXUSE_BRIDGE_DIR` | override the local bridge state directory (default: `~/Library/Application Support/axuse` on macOS) |
| `AXUSE_ENABLE_MACOS_CUA=1` | enable macOS desktop tools (Accessibility + Screen Recording grants are still required) |
| `AXUSE_BRIDGE_DEBUG=1` | trace the extension bridge |

To drive your own logged-in Chrome instead of a fresh browser, see
[Driving your daily browser](#driving-your-daily-browser-the-codex-pattern).

## Benchmark

On 16 server-scored browser tasks, run in the same harness (Codex CLI) with
the same model against Codex's own Chrome extension and
[browser-use](https://github.com/browser-use/browser-use) 0.13:

| | axuse | Codex + Chrome extension | browser-use |
|---|---|---|---|
| tasks passed | 28/28 | 27/27 | 27/27 |
| median time per task | **17 s** | 26 s | 56 s |
| tokens, all runs | **4.7M** | 9.8M | 8.8M |
| median tool calls | **5** | 7 | 12 |

Accuracy tied on those tasks. A harder 7-task extension (checkout flow,
virtualized grid, date pickers, signup errors, nested iframes, file upload,
local dev app) has been run once so far: axuse 7/7; Codex + Chrome failed 2
by stopping to ask for confirmation; browser-use failed 1. Method, anti-cheat
rules and how to reproduce: [`bench/suite/README.md`](bench/suite/README.md).
Install footprint and setup comparison: [`bench/suite/EASE.md`](bench/suite/EASE.md).

## Why this is faster and more reliable than screenshot CUA

| | Screenshot CUA loop | axuse |
|---|---|---|
| Observation | ~1–2MB base64 raster per step, ~1k+ image tokens, vision-encoder latency | YAML-like AX snapshot in KBs; **diff by default** (only removed/added/changed lines) |
| Targeting | pixel coordinates guessed from an image; drifts with resolution/DPR | stable element refs bound to **element identity** (insertion-safe) + Playwright-style semantic lines |
| Input | synthetic DOM events (rejected by sites) or fragile OS injection | trusted CDP `Input.dispatch*` events (`isTrusted`) |
| Failure mode | silent misclick, discovered 2–3 screenshots later | typed errors: `REF_NOT_FOUND` / `STALE_STATE` / `DIALOG_OPEN` with `retry` hints and `action_sent` facts |
| Change detection | re-photograph everything | sequenced event ring buffer + snapshot diffing |
| Cost control | none | budgets enforced in code: snapshot char/node caps, screenshot byte budget with quality fallback, one raster per call |

## Install into a harness

```bash
node src/cli.js print-config --harness codex       # or claude-code | opencode | agy | zed | generic
```

- **Codex** — append the printed `[mcp_servers.axuse]` block to `~/.codex/config.toml`
- **Claude Code** — `claude mcp add --scope user axuse -- node <path>/src/cli.js mcp`
- **OpenCode** — merge the printed `mcp` block into `opencode.json`
- **Zed** — merge the printed `context_servers` block into `settings.json`
- **Antigravity / any MCP host** — the printed `mcpServers` block

Verify the environment: `node src/cli.js doctor`.

The model-facing usage contract lives in [`SKILL.md`](SKILL.md) — point your
harness's skill system (or system prompt) at it.

## Solver plugins (open core ↔ paid solvers)

axuse's core is CAPTCHA-agnostic and ships **no solvers**. Turnstile / hCaptcha /
reCAPTCHA solvers are **separate, commercially licensed packages** that plug in
through a thin, frozen seam (`src/plugins/`):

```bash
# install: drop a solver package into plugins/ (or list dirs in AXUSE_PLUGINS)
node src/cli.js plugins          # → ✓ captcha-pro@1.0.0 — providers: turnstile, recaptcha-v2, hcaptcha
node src/cli.js solve turnstile --url https://demo.turnstile.workers.dev/
```

From any MCP harness: `ax_solve_captcha { provider, url? }` and `ax_plugins`.
Plugins that own capabilities (session vaults, network governors, guarded
actions…) can also contribute their **own MCP tools** through a validated
`tools()` export (`ax_vault`, `ax_net`, `ax_guard`, …) — same seam, same
trust boundary. The ABI, the solver context, the `tools()` contract, and
packaging/licensing guidance:
[`docs/plugins.md`](docs/plugins.md). A complete free (PolyForm Noncommercial
1.0.0) reference solver
lives in [`examples/plugin-turnstile-demo/`](examples/plugin-turnstile-demo/).

## Architecture

```
harness (Codex / Claude Code / OpenCode / Antigravity / …)
        │  MCP over stdio (JSON-RPC 2.0) — the universal integration layer
        ▼
src/mcp/ — thin, stateless-per-call adapter (hand-rolled, zero deps)
        │  one frozen command contract (src/core/commands.js)
        ▼
src/core/ — the state owner (Codex native-host / ZCode host pattern)
   ├─ controller.js  sessions, tabs, state_id, dialogs, auto-observe diffs,
   │                OOPIF frame registry (f-slot namespaced refs)
   ├─ cdp.js         launch/attach Chromium, browser-level WebSocket, flat sessions,
   │                Target.setAutoAttach for OOPIF auto-entry
   ├─ serialize.js   injected AX serializer: element-identity refs, shadow DOM,
   │                same-origin iframes (coordinate-translated), budgets
   ├─ diff.js        per-ref diff + text-line multiset diff + cost-based full fallback
   ├─ actuate.js     trusted Input.dispatch*, live ref resolution (frame-chain
   │                coordinate translation), combos, waits
   ├─ screenshot.js  one raster, byte budget, quality fallback, crop hints
   └─ errors.js     typed codes, retry hints, action_sent, side_effect
        │
        ├─ src/bridge/ — Codex-style extension backend for the user's daily
        │   Chrome/Edge: hub (TCP rendezvous, token auth, NDJSON) ←
        │   native-messaging host (framed stdio relay) ← MV3 extension
        │   (chrome.debugger = real CDP over the user's real browser)
        │   + installer (registry/HKCU, host manifest, launcher) + backend
        │   duck-typed onto the same command contract
        ▼
src/desktop/ — computer use (driver matrix)
   ├─ Windows: resident PowerShell + SendInput/GDI, DPI-aware — VERIFIED
   ├─ macOS:   JXA/CoreGraphics + System Events + screencapture/sips,
   │           display-point coordinates, env-gated with TCC diagnostics
   └─ (the driver interface is the seam for a real AX/UIA tree driver)
```

### The patterns, and where each one comes from

| Pattern | Codex lineage | ZCode lineage | axuse implementation |
|---|---|---|---|
| Diff-first observation | page-state diffing, default-on | "the tree comes back as a diff only… unchanged rows are omitted and their indices stay valid" | per-ref diff; refs bound to element identity so unchanged rows stay valid |
| Element addressing | axState `element_index`/locators | refs `e1,e2…` in page + live resolution | `window.__axuseRefRegistry` (identity-stable across insertions), resolved live at action time |
| Trusted input | `chrome.debugger` → `Input.dispatch*` | `webContents.debugger` | raw CDP `Input.dispatch*` / `Input.insertText` |
| Event buffer | `tab_cdp_events` sequenced ring (limit ≤1000) | event routing with teardown discipline | ring buffer, sequence cursor, filtered methods |
| Context budgets | 64k DOM text cap | 2×200KB images at protocol boundary, one-raster rule | snapshot node/char caps; screenshot 200KB budget + quality fallback + crop hint |
| Error vocabulary | structured locator errors | `STALE_STATE`, `sideEffect:none\|uncertain`, `actionSent` | typed codes + `retry: reobserve\|retry\|never` + `action_sent` |
| Frozen contract | command envelopes w/ zod | one discriminated union for all backends | `Contract` → validation AND MCP schemas generated from it |
| Real user session | attaches to the user's real Chrome profile | `extension` backend via user Chrome | **extension bridge**: MV3 extension + native messaging + hub; `ax_open {backend:"extension"}` |
| Frame entry (OOPIF) | webmcp/extension content merging | IAB merges frames; OOPIF via CDP | `Target.setAutoAttach` per tab; frames serialized in their own sessions, merged under `f<slot>:` identity-stable refs, input dispatched frame-locally |

## Tool surface

**Browser** (always): `ax_open`, `ax_open_many`, `ax_navigate`, `ax_go`, `ax_tabs`, `ax_observe`,
`ax_click`, `ax_fill`, `ax_upload`, `ax_hover`, `ax_type`, `ax_press`,
`ax_scroll`, `ax_select`, `ax_drag`, `ax_screenshot`, `ax_element_info`,
`ax_evaluate`, `ax_wait_for`, `ax_events`, `ax_dialog`, `ax_close`.

The ones that save the most steps:

- `ax_fill` sets a whole form in one call (text, selects, checkboxes, radios,
  switches, custom dropdowns, autocomplete) and can click submit.
- `ax_observe {find:"text"}` returns only the matching lines of a big page.
- `ax_scroll {until:"text"}` scrolls an infinite feed or virtualized list,
  in the page or inside a scrolling panel, until the text appears.
- `ax_upload` sets files on a file input; a click that opens a file chooser
  is reported instead of hanging.
- `ax_open_many` opens a bounded URL list sequentially, returns compact titles
  and accessibility excerpts, and closes temporary tabs by default. It is the
  batch equivalent of `ax_open` and avoids writing a custom MCP client.
- JS dialogs and new tabs opened by an action are reported in its result.

**Desktop** (Windows: verified. macOS: opt-in with
`AXUSE_ENABLE_MACOS_CUA=1` and host Accessibility/Screen Recording grants):
`ax_desktop_screenshot`, `ax_desktop_screen_info`,
`ax_desktop_click`, `ax_desktop_type`, `ax_desktop_press`, `ax_desktop_scroll`,
`ax_desktop_windows`, `ax_desktop_activate`, `ax_desktop_drag`.

## The loop the model is guided into

```
ax_open            → FULL snapshot with refs (e.g. button "Sign in" [ref=e7])
ax_click {e7}      → returns the DIFF (removed/added/changed + text changes)
ax_type {e9, text} → returns the diff (value="…" appears)
…                  → screenshots ONLY for visual/canvas checks (budget-capped)
```

Cross-origin iframes appear in the snapshot as `--- frame f1 (url) ---`
sections with `f1:eN` refs — same loop, same diffs, frame-stable refs.

Errors are facts, not strings:

```json
{ "code": "REF_NOT_FOUND",
  "hint": "Refs are per-observation. Call ax_observe, then act on fresh refs.",
  "retry": "reobserve" }
```

## Driving your daily browser (the Codex pattern)

```bash
axuse bridge install        # registers the native messaging host (Chrome + Edge hives)
axuse bridge                # hub (or it starts on demand with the MCP server)
```

Load `bridge/extension` unpacked in Chrome/Edge (Developer mode → Load
unpacked), then `ax_open { url, backend: "extension" }`. Your browser, your
logged-in sessions, tabs visible to you; `ax_close` closes only axuse's tabs.
The whole chain — native messaging framing, hub auth, CDP-over-chrome.debugger,
diffed snapshots, trusted clicks — is covered by `test/bridge.test.js`.

Field notes (all established live against Edge 155 / Chrome 155):

- Branded Chrome silently ignores `--load-extension` (137+). Automated
  extension loads need Edge / Chromium / Chrome-for-Testing; manual unpacked
  loads work everywhere.
- Edge requires `allowed_origins` in the host manifest — with only
  `allowed_extensions` it reports "native messaging host not found". The
  installer writes both.
- On macOS, `setup` and `bridge install` also accept `chromium` and
  `chrome-for-testing`, and register the host in each browser's own
  `NativeMessagingHosts` directory.
- The Windows native host is a `.bat` launcher: cmd.exe relays the framed
  stdio pipes faithfully, where a compiled C# shim (Console.OpenStandardInput)
  dropped Chrome's stdin writes. Debug any install with
  `AXUSE_BRIDGE_DEBUG=1` (hub + nm-host message tracing) and
  `self.__axuseDiag` inside the extension's service worker (readable via an
  attached DevTools/CDP session).

## Honest scope notes

- **macOS desktop control is opt-in** because it can synthesize OS input. Set
  `AXUSE_ENABLE_MACOS_CUA=1`, grant the host Accessibility and Screen Recording
  in System Settings → Privacy & Security, then run `node src/cli.js doctor`.
  Coordinates are macOS global display points and returned desktop JPEGs are
  resized to the same point dimensions for direct click mapping. Linux has no
  desktop driver yet (browser tools still work).
- **The extension backend has no Target domain** (chrome.debugger limits), so
  OOPIF frames are listed but not entered there; the CDP backend enters them.
- **Anti-bot reality**: axuse runs a real Chromium with a real profile, but
  it does not implement Codex's `x-browser-agent` traffic labeling or
  bot-detection handoff — sites that fingerprint automation may still block
  it. The bridge backend (your daily browser) is the most human-looking path.

For a non-MCP batch workflow, the same bounded operation is available from the
CLI and accepts plain URL lists or Markdown containing links:

```bash
node src/cli.js open-urls sources.md --backend extension --output report.md
```

The generated report is intentionally honest: it contains page titles, access
status, and bounded accessibility-text excerpts, not fabricated semantic
summaries.

## Development

```bash
node --test test/diff.test.js test/browser.test.js test/mcp.test.js test/desktop.test.js test/oopif.test.js test/bridge.test.js test/plugin.test.js
```

On macOS, enable the passive desktop capability gates explicitly:

```bash
AXUSE_ENABLE_MACOS_CUA=1 AXUSE_TEST_MACOS_DESKTOP=1 npm test
```

Tests are gated: browser/MCP tests skip when no Chromium-family browser is
found; the OOPIF test spins up two loopback origins (127.0.0.1 + localhost);
the bridge test registers + uninstalls a native messaging host and uses an
extension-capable browser when one is installed (branded Chrome on current
macOS builds is honestly skipped because it ignores automated
`--load-extension`); desktop tests exercise passive screen/window discovery on
macOS with `AXUSE_TEST_MACOS_DESKTOP=1`. A real macOS screenshot test is
explicit with `AXUSE_TEST_MACOS_SCREENSHOT=1` because Screen Recording is
TCC-protected. No test moves your real mouse or sends keys.

Debugging: `AXUSE_BRIDGE_DEBUG=1` traces hub + native-host messages;
`self.__axuseDiag` in the extension SW reports connect/disconnect history.

Every `# SKIP` in a suite run is explicit, typed, and audited: run TAP output
through `node tools/skip-audit.mjs` and it fails when a skip is missing from
`test/skip-allowlist.json`. CI enforces this on every lane.

macOS production reference — support matrix (browsers × macOS versions ×
arch), TCC permission model, failure taxonomy, the manual TCC validation
lane, benchmarks, and known limitations with evidence:
[docs/macos-support.md](docs/macos-support.md).

## License

Copyright © CTOL Digital Solutions ([ctol.digital](https://ctol.digital)).
All rights reserved.

Licensed under the **PolyForm Noncommercial License 1.0.0** — see
[`LICENSE`](LICENSE). Use, distribution, and modification are free for any
**noncommercial** purpose (personal use, research, education, charities,
public institutions). **Commercial use requires a separate commercial
license** from CTOL Digital Solutions ([ctol.digital](https://ctol.digital)).
