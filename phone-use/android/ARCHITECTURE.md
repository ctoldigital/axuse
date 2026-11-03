# axuse phone-use · Android — engineered architecture

Status: app (AccessibilityService + on-device RPC server, guard, custody,
journal), host engine (refs/diff/verify/settle), MCP server (stdio + HTTP),
simulator, offline suite (24 tests) and hardware gates are implemented and
**measured on a real retail Samsung phone (Galaxy S21 FE, Android 16 / One UI,
2026-10-25): 9/9 gates pass**, plus an end-to-end MCP session (initialize →
launch → find → 35 ms semantic tap → diff).

---

## 1. The answer in one picture

```
 Meta Muse · Claude · ChatGPT · Gemini · Codex / Claude Code / OpenCode
        │ MCP stdio (axdroid serve)   │ MCP HTTP      │ MCP via relay (OAuth 2.1)
        ▼                             ▼               ▼
  axdroid host engine            any MCP client   https://relay/h/<phone>/mcp
  (render · refs/diff · find · plans · scopes)        │ outbound tunnel
        │ HTTP JSON-RPC /rpc             │              │ (phone dials out,
        ▼                               ▼              │  nothing opened at home)
 ┌──────────── axdroid app — ONE APK, zero dependencies ────────────┐
 │ AccessibilityService (public API, no ADB, no root, no runner)     │
 │   tree dump (windows, ids, sigs) · performAction · dispatchGesture│
 │   event counter (settle signal) · global actions · apps/launch    │
 │ RPC server (plain sockets): /rpc · /healthz · /events (SSE)       │
 │   └─ /mcp — the phone answers MCP itself (same tools, same format) │
 │ Guard (label risk) · Custody (stop latch) · Journal (jsonl+meter)  │
 │ RelayTunnel (outbound, HMAC, backoff reconnect, link codes)       │
 │ owner UI: token · LAN toggle · stop/resume · relay config         │
 └───────────────────────────────────────────────────────────────────┘
```

No hub hardware, no pairing, no Developer Mode, no USB for consumers — and
since 0.2, **no host software either**: the phone speaks MCP directly
(`/mcp` locally, or through the relay for cloud agents), with the guard,
custody latch, journal and tool metering on the device for every transport.

## 2. What the research established (and where it corrects `android.md`)

| Question | Finding | Evidence |
|---|---|---|
| Can an app read and drive any other app? | **Yes, one APK**: `AccessibilityService` + `canPerformGestures`; no ADB, no root, works on a retail phone | Android docs; measured here on SM-G990B2 |
| How do the reference projects do it? | mobile-mcp: host TS server + ADB + USB debugging, screenshot fallback. mobilerun: host Python framework + AGPL Portal app + subscription cloud. OpenMinis: GPL agent-*client* app (agent runs on-phone, BYO keys) — a different product | their READMEs/licenses, 2026-10-25 |
| Text input | `ACTION_SET_TEXT` works in One UI's search overlay, **and the value reads back** (after a 180 ms settle; an immediate read races) | G4 + probes |
| The IME "enter" | `AccessibilityAction.ACTION_IME_ENTER` exists from API 34 and works on Android 16; older: click the IME window's action button | Core.imeSubmit |
| Do semantic scrolls always work? | **No — measured**: One UI's main Settings list accepts `ACTION_SCROLL_FORWARD` (returns true) and *does not move*. The engine now verifies visible-content movement per swipe and switches to a real `dispatchGesture` fling | probes; G3 |
| …and flings | a fling's momentum keeps scrolling after the swipe: an `until:` hit found mid-fling can be gone by the time the agent taps. The engine re-verifies after 0.6 s of stability before declaring found | G3 flake → fix |
| Stable element identity | window ids are stable at idle, but list re-binding during/after scrolling churns path ids. Both sides self-heal now: the device falls back to a content-signature match (`resolveSmart`), the host ref registry inherits refs across recycled identities | G3 STALE_REF → fix |
| Multi-window correctness | the status bar / nav bar / edge panels are separate windows *above* the app; occlusion checks must be layer-aware across windows, and text search must be window-scoped (the status-bar "Battery 68 per cent." icon otherwise answers for Settings' Battery row) | G3/G5 probes |
| Settle signal | `onAccessibilityEvent` counters are a quiet signal that works (1000+ events during a gates run; quiet in seconds at idle) — but Samsung's activity transitions can *start* after a 2.5 s settle window: an empty diff right after a successful act is re-checked after a stable wait | G3 diff-empty → fix |
| Samsung's freezer kills background agents | **Measured**: One UI's `FreecessController` cached *and froze* the accessibility-service process mid-session (binder calls queue; every request times out). Fix: run as a **foreground service** (`specialUse`, like TalkBack) + an unbounded worker pool + a 30 s watchdog that answers `BRIDGE_TIMEOUT` | logcat `UFZ`; G9 0/6 → 6/6 |
| Even `resume` was broken by the latch | the custody check refused `resume` itself — a self-lockout. Owner controls are now exempt by construction | G2 STOPPED → fix |
| Retail shell enabling | `settings put secure enabled_accessibility_services` silently no-ops without `--user 0` on this device; on a retail phone the owner enables the service once via Settings (restricted-settings flow for sideloaded APKs) | pairing log |
| Package visibility | Android 11+ hides other packages: `queryIntentActivities` saw **15 apps** until a `<queries>` MAIN/LAUNCHER declaration (→ **142**, Contacts included) | apps() before/after |
| Substring matching lies | 'Display' matched **'Always On Display'** (the Lock-screen row's summary) while the real Display row was below the fold → ranked matching (exact name > prefix > substring) in resolve, and `until:` refuses substring hits when the exact row exists off-screen | task-suite flake → fix |
| Unicode punctuation | 'Wi‑Fi' (U+2011) ≠ 'Wi-Fi' broke expect/wait — both sides of the wire now fold hyphens/quotes to ASCII for matching | plan step 3 → fix |
| Omitted search boxes | the Play Store's search box exposes **no editable node and no resource id** on this build — recorded as a skip; vision-driven stacks would screenshot-guess, we report | task probe |
| The eval earns its keep immediately | the LLM-in-the-loop run (agent CLI → on-device MCP) found what the scripted suite missed: on-device `phone_scroll` sent no scroller id (every scroll STALE_REF in milliseconds) and `phone_find` ignored role/rid-only queries — the agent fell back to raw swipes and wandered. Both fixed; the scripted suite then re-verified | eval journal diff |
| App state persists across non-fresh launches | a `phone_launch` without `fresh` restores the activity *where it was* — Settings left scrolled to the bottom has no 'Connections' in the viewport, and a rigid `phone_run` plan cannot adapt. The engine now answers a truthful `NOT_FOUND … (scrolled down and up)` instead of falling into a blind tap; the right agent move is `fresh:true` or search | phone_run eval task → fix |
| The display sleeps mid-run | accessibility actions do **not** reset the display timer — long agent tasks can lock the screen under a moving agent. Harnesses keep it awake (`svc power stayon usb` while on USB) | eval harness |
| Windows subtleties | a `.cmd` CLI shim spawns a node child that survives timeout: evals must kill the process *tree*; and the phone's display sleeps between runs — harnesses wake it first (`KEYCODE_WAKEUP` + `wm dismiss-keyguard`) | eval harness |
| Topology gotcha | `adb reverse` makes the *device* listen (and stole port 8360 from the app). The server runs on the phone, so the host uses `adb forward` | first bring-up |
| Building without Gradle | aapt2 compile/link + javac `--release 8` + d8 + zipalign + apksigner from the SDK alone builds the APK in seconds; org.json and every API used ship with the platform | tools/build-apk.ps1 |

Corrections to the directional guide:

1. **"Technically straightforward" is true; "much easier" needs qualifiers.**
   The API is public and powerful, but One UI ignores semantic scrolls on its
   main list, lies about it, and freezes your process if you are not a
   foreground service. The engine's verify-everything posture is what caught each of these.
2. **The app must be the policy point.** Guard, custody and journal run on the
   phone, so every transport (USB, LAN, anything speaking `/rpc`) gets them —
   the host cannot bypass them even by bug.
3. **Text first, pixels only as a guarded fallback**: observation is the accessibility tree.
   `phone_screenshot` exists for screens the tree cannot describe; it refuses screens the tree
   already explains (unless forced), obeys an owner switch in the app, and cannot capture secure windows.

## 3. Why this is state of the art

| | axdroid (this) | mobilerun / Portal | mobile-mcp | OpenMinis |
|---|---|---|---|---|
| Observation | all on-screen windows' native trees | native tree + screenshots | native tree + screenshots | n/a (client) |
| Input | **semantic-first** (performAction), occlusion-checked gesture fallback | gestures/coords | coords from tree | n/a |
| Text | `ACTION_SET_TEXT` + device-side read-back verification | keyboard/IME | keyboard | n/a |
| Per-action output | ref-keyed diff, refs survive re-binding | full re-dump | full dump | n/a |
| Install for consumers | one APK, no ADB, no subscription | APK + subscription cloud | host + USB debugging + SDK | store app |
| Agent interface | MCP stdio + HTTP, 13 tools, the same tool names as the iPhone product | own CLI/SDK | MCP | in-app models |
| Consequential-action guard | on-device, every transport | — | — | — |
| Owner custody | stop latch outranking every token, survives restarts | — | — | — |
| Settle | device event counter + stable-waits + momentum re-verify | sleeps | sleeps | n/a |
| **Measured observe cost** | **34–56 ms · median 827 tokens** | full re-dump each step | uiautomator XML dump ~1–3 s + full element list | n/a |
| **Measured action latency** | **35 ms raw click · 0.6–0.9 s incl. settle+diff** | gesture + re-dump | gesture + re-dump | n/a |
| **Reproducible device task suite** | 8/8 tasks, token-metered (`axdroid tasks`) | claims, no published harness | — | — |
| Licences | axuse, zero deps (platform + stdlib only) | MIT framework + **AGPL** Portal | Apache-2.0 | GPL-3.0 |

The engine contributions (verify-before-trust, everywhere):

* **Verify the list actually moved.** A scroll that returns success and does
  nothing is detected per-swipe by comparing visible-content signatures, and
  the next swipe is a real fling.
* **Verify the tap point.** A gesture fallback runs only if the topmost window
  at the point, layer-ordered, holds the target or a relative of it — else
  `OCCLUDED` naming the coverer. Cross-window: the nav bar verifies against
  its own window.
* **Verify the text landed.** `ACTION_SET_TEXT` is read back on the phone
  (with a self-heal if the IME re-layout moved the field).
* **Verify the diff.** An empty diff right after a successful act is re-checked
  after a stability wait — Samsung transitions can start late.
* **Blind taps are a policy question.** A coordinate tap where no *named*
  element exists is refused with `NEEDS_CONFIRMATION` until the agent asks.
* **Verify the match is the right one.** Ranked matching (exact name > prefix
  > substring) with foreground-app preference: 'Display' resolves to the
  Display row, never to 'Always On Display', and background windows cannot
  manufacture false AMBIGUOUS.
* **Uncertain outcomes are errors, never guesses.** `STALE_REF` retries once
  with fresh resolution (host and device, signature-based), then surfaces.

## 4. Code map

| Piece | Files | Responsibility |
|---|---|---|
| App · service | `app/src/…/AxService.java` | tree access, resolve (+signature self-heal), gestures, global actions, event counter, occlusion check, foreground-service notification, owns Core/Mcp/RelayTunnel |
| App · dump | `app/src/…/TreeDump.java` | every window's nodes: path ids, flags, bounds, sigs |
| App · core | `app/src/…/Core.java` | the `/rpc` method table: semantic-first click, type+verify, scroll, wait, launch, clipboard, guard/custody/journal enforcement, relay_set/relay_get/relay_link |
| App · server | `app/src/…/RpcServer.java` | plain-socket HTTP/1.1, keep-alive, token auth (loopback exempt), SSE events, 30 s watchdog, unbounded pool; routes /rpc, /healthz, /events, **/mcp** |
| App · on-device MCP | `app/src/…/Mcp.java`, `McpTools.java` (generated) | the phone answers MCP itself: roles/refs/render/diff (format-identical to the host), ranked resolve, movement-aware scroll, adaptive settle, 13 tools; every call metered to the journal |
| App · relay | `app/src/…/RelayTunnel.java` | outbound tunnel to the relay (HMAC handshake, frames, backoff reconnect), link codes minted by the phone |
| App · policy | `Guard.java` `Custody.java` `Journal.java` `PolicyStore.java` | label-risk classification, stop latch + session boxes, rotating journal, token + toggles + relay config |
| App · UI | `MainActivity.java` | token display/copy, LAN + server toggles, stop/resume, cloud-relay config |
| Host · engine | `axdroid/engine.py` | observe/render, refs (+recycled-identity inheritance), diff, resolve, tap/type/scroll (movement-aware)/swipe/back/button/launch/apps/wait/run, settle |
| Host · model | `axdroid/model.py` | flags, roles from Android classes, find (window-scoped, ranked), framework detection |
| Host · transport | `axdroid/client.py` | adb forward / LAN+token / any URL; typed errors |
| Host · MCP | `axdroid/mcp.py` | stdio + Streamable HTTP, 13 tools, scopes + actor (relay-enforced) |
| Host · relay | `axdroid/relay.py`, `tunnel.py`, `journal.py` | cloud relay + OAuth 2.1 AS (shared wire protocol with the upcoming axphone for iPhone, so one relay can serve both fleets), outbound tunnel client, rotating journal |
| Host · sim | `axdroid/sim.py` | the same wire protocol over real HTTP; dialog occlusion, IME, guard, custody, change counters |
| Host · gates | `axdroid/gates.py` | G1–G9 + app sweep; `publish_gates` renders COMPATIBILITY.md |
| Host · tasks | `axdroid/tasks.py` | the multi-step device task benchmark (token-metered) |
| Build | `tools/build-apk.ps1`, `tools/gen-mcp-tools.py` | aapt2 + javac + d8 + apksigner (no Gradle); tool-table codegen from the Python TOOLS (parity guarantee) |
| Eval | `tools/eval-agent.py` | agent-CLI-in-the-loop eval over the on-device MCP; per-task verdicts, LLM cost/tokens, phone-side metering |

### 4.1 Wire protocol (v1)

`POST /rpc {"method","params"}` → `{"ok":true,"data":…}` | `{"ok":false,
"error":{code,message,facts}}`. Methods: `status observe changes act type wait
apps launch open_uri clipboard stop resume journal policy_get policy_set
reset_token screenshot(→NO_SCREENSHOTS)`. Node ids are `w<windowId>:<i>/<j>…`
paths; every observe also returns per-node `sig` (content identity) used by
both self-heals. `GET /healthz` (no auth), `GET /events` (SSE change ticks).
Coordinates are screen pixels.

### 4.2 Security model

* Loopback (adb forward, on-device) needs no token — possession of the USB
  link is the authorisation. LAN binds `0.0.0.0` only on the owner's switch
  and then requires the pairing token for every non-loopback connection.
* The guard, custody latch and journal live **on the phone** and gate the
  dispatch table itself — a host bug cannot skip them.
* `policy_set` / `reset_token` accept loopback only (the owner's device).
* The app requests no storage, location, contacts or phone permissions —
  only INTERNET, notifications, the specialUse foreground service and
  clipboard access it already has as an accessibility tool.
* Google Play distribution of an autonomous agent remains policy-constrained
  (see Google Play's policy for accessibility tools); this ships as a sideloaded/fleet APK, honestly
  labelled `isAccessibilityTool=false`.

## 5. Performance & economy (measured, Samsung S21 FE · Android 16, 2026-10-25)

| Dimension | Measured | vs the field |
|---|---|---|
| Observe (RPC + dump) | 34–56 ms (median 55) | mobile-mcp's uiautomator dump path costs ~1–3 s per read (adb + XML) |
| Semantic click (`ax-click-ancestor`) | **35 ms** raw · **0.6–0.9 s** end-to-end incl. settle + diff | coordinate-tap drivers pay gesture time + screenshot/tree re-read |
| Action settle | event-driven, early-exits on quiet (~120 ms) — home-button action 610–742 ms total | fixed sleeps (1–2 s) in most stacks |
| Typed text | ~210 ms + read-back verification | keyboard walking or IME injection |
| Observe tokens (median screen) | **827** compact vs 1,351 verbose (−40%) | full element lists: mobile-mcp/mobilerun re-dump everything each step |
| Task suite | **8/8 passed, 0 failed** in 77.5 s; 19.1 k tokens of tool output total | no competitor publishes a reproducible device task suite with token counts |
| Round trips | `phone_run` plans: a 5-step flow in one model call (3.5 s) | one tool call per step elsewhere |

Token economy, engineered: chrome windows (status/nav bar ≈ 50 nodes on One
UI) are hidden behind one note line; resource ids lose their package prefix;
runs of ≥4 homogeneous leaf rows render 2 + `×N more` (phone_find/scroll
address the rest); `phone_find` returns ranked matches (exact name > prefix >
substring) so agents stop paying for ambiguity lists.

Latency, engineered: settle early-exits when the device event counter goes
quiet (late One UI transitions are caught by an empty-diff re-check), and
taps/typing observe immediately after acting instead of waiting out a fixed
window.

**Known honest limitation (by design):** Google Play Store's search box is
not accessibility-exposed on this build (no editable node, no resource id) —
the task suite records the skip rather than screenshotting a guess. Opaque
surfaces are where the guarded `phone_screenshot` fallback applies.

## 6. Task benchmark (the complex-task evidence)

`python -m axdroid tasks --out tasks-report.json` — nine multi-step,
cross-app tasks, each verified from the device's own tree, metering the tool
output an agent would have paid:

| Task | Result | ms | tokens |
|---|---|---|---|
| Wi-Fi toggle round-trip (state verified twice) | PASS | 4,530 | 1,780 |
| Settings search + filter (typed, verified) | PASS | 6,263 | 2,613 |
| About-phone data extraction (SM-G990B2/DS) | PASS | 10,961 | 2,138 |
| Dark mode via search (One UI 8 has no Display row) | PASS | 5,079 | 2,978 |
| Contacts: create → guard refuses Delete → confirmed delete | PASS | 15,557 | 3,736 |
| Guard refuses 'Reset' (never confirmed) | PASS | 18,967 | 1,977 |
| 5-step `phone_run` plan, one round trip | PASS | 3,762 | 700 |
| Messages typed draft (search variant: not default SMS app) | PASS | 6,486 | 1,894 |
| Play Store search | skip | 5,863 | 1,323 |

Rendered live: [BENCHMARK.md](BENCHMARK.md), evidence `tasks-samsung-s21fe.json`.

## 7. Hardware gates (what must pass on a real phone)

`python -m axdroid gates --out gates.json [--apps N]` writes measured
evidence; `publish_gates` renders the public list.

| # | Gate | S21 FE result |
|---|---|---|
| G1 | bridge: healthz + service connected + policy | PASS 36.9 ms |
| G2 | Settings tree: ≥40 nodes, ≥5 clickable, resource ids | PASS 133 nodes / 10 clickable |
| G3 | semantic click on a real row (scroll-into-view, title-scoped, stable-waited) | PASS ax-click-ancestor, "About phone" |
| G4 | search: open overlay, `ACTION_SET_TEXT`, device read-back == "battery" | PASS |
| G5 | gesture fallback: blind tap that names a real element proceeds; coordinate path | PASS via gesture |
| G6 | settle: counter advances on navigation, then quiets | PASS |
| G7 | custody: stop refuses acting, reads work, resume re-enables | PASS |
| G8 | guard: risk-labelled control refuses without confirm (never confirmed) | PASS "Delete screen mirror" → NEEDS_CONFIRMATION |
| G9 | app sweep: N apps classified rich/partial/opaque + framework | PASS 6/6 rich |

## 8. What is deliberately not built yet

* **QR pairing** (token shown as a scannable code), **AppFunctions** as an
  additional executor, multi-device routing, and a Play-compliant "restricted"
  distribution build (static workflow automation only).
* In-app link-code display (the code is minted by the phone via `relay_link`
  today; a companion screen showing it live is cosmetic).
* Vision: never — an opaque screen is reported as opaque, by design.
