# axdroid — axuse phone-use for Android

axdroid is a **phone-use library for AI agents** such as Codex, Claude Code, ChatGPT Work, Dots, Meta Muse, OpenCode, Devin, Cursor and Antigravity.
It is not an agent itself: it gives the agent you already use a real Android phone to drive through the
phone's **own accessibility tree**: text first, no vision model needed, and **no external hub**: the phone
app *is* the hub.

```
app: com.android.settings · 1080×2340px · changes 1107
- group id=com.android.systemui:id/navigation_bar_frame [ref=e1]
  - image "Recents" id=com.android.systemui:id/recent_apps [ref=e8]
  - image "Back" id=com.android.systemui:id/back [ref=e9]
  - image "Home" id=com.android.systemui:id/home [ref=e11]
      - button "Search settings" [ref=e78]
      - list id=com.android.settings:id/recycler_view [ref=e84]
        - cell [ref=e85]
          - text "Sign in to your Galaxy" id=android:id/title [ref=e88]
        - cell [ref=e93]
          - text "Connections" id=android:id/title [ref=e104]
```
```
> phone_tap {target: "e104"}
phone_tap Connections · 35 ms · via ax-click-ancestor
+ text "Connections" [ref=e147]
+ image-button "Navigate up" [ref=e151]
…
```

* **Read:** every on-screen window's native accessibility tree — text, content
  descriptions, resource ids, bounds, flags — rendered compactly with refs.
* **Act:** semantic first (`performAction` — the TalkBack contract, ~35 ms),
  gesture fallback (`dispatchGesture`) only after a cross-window occlusion
  check; text via `ACTION_SET_TEXT` with a device-side read-back verification.
* **Answer:** a diff keyed by refs that survive list re-binding (signature
  self-healing on both sides of the wire).

Design and measured evidence: [ARCHITECTURE.md](ARCHITECTURE.md).
Measured device compatibility: [COMPATIBILITY.md](COMPATIBILITY.md).
Measured tasks/tokens/latency: [BENCHMARK.md](BENCHMARK.md).

## The shape of it

```
any MCP agent (Codex, Claude Code, OpenCode, Muse …)
        │  MCP stdio (this package)  ·  or Streamable HTTP --listen
        ▼
axdroid host side — render · refs/diff · find · plans · adb forward
        │  HTTP JSON-RPC, token-guarded (loopback exempt)
        ▼
┌──────────── axdroid app (one APK, no dependencies) ────────────┐
│ AccessibilityService: tree · events · performAction · gestures  │
│ RPC server on the phone · guard · custody · journal             │
│ owner UI: token, LAN toggle, stop latch                         │
└──────────────────────────── ▲ ──────────────────────────────────┘
                     no ADB required for consumers:
            agent ──Wi-Fi + token──▶ phone (owner-enabled LAN mode)
```

USB debugging is a *developer* convenience (`adb forward tcp:8360 tcp:8360`),
not a requirement: with the owner's LAN toggle the agent talks straight to the
phone. The screen is read as text. For a screen the tree cannot describe (a game, canvas, map or
photo) there is a fallback, `phone_screenshot`: it returns an image for the agent's own vision,
refuses screens the tree already explains unless `force: true`, and the phone's owner can switch
it off in the app. The image comes with a labelled coordinate grid (`grid`, on by default) and, with `marks: true`, boxes labelled with
the same `eN` refs that `phone_observe` prints, so a vision model can name what to tap. For a blind tap, `phone_tap {x, y, space}` accepts `space: "image"` (pixels of the last screenshot) or `"normalized"` (0-1000
on both axes); blind coordinate taps still need `confirm: true`. Beyond that: `region: [left, top, right, bottom]` zooms into part of the screen at native detail (small text, a game tile), with the grid and tap coordinates mapped to the zoom; a confirmed blind tap reports whether the pixels changed (so a missed point is measured, not guessed); and a point outside the screen is refused with a clear error. The basic ideas (labelled grid, coordinate spaces, element marks) follow what MobileRun's vision path does; the code is our own.

## Quick start (no phone needed)

```sh
cd phone-use/android
python -m unittest discover -s tests     # 24 tests: model, refs/diff, e2e over the sim's real HTTP
python -m axdroid sim --port 8361        # simulated phone speaking the same wire protocol
python -m axdroid observe --url http://127.0.0.1:8361
```

## With an Android phone (6.2 or any phone with AccessibilityService)

```sh
powershell -File tools\build-apk.ps1     # aapt2+javac+d8+apksigner — no Gradle, no downloads
adb install -r axdroid.apk
adb shell settings --user 0 put secure enabled_accessibility_services \
    com.axuse.axdroid/com.axuse.axdroid.AxService   # dev device; on a retail
adb shell settings --user 0 put secure accessibility_enabled 1             # phone the owner
                                                    # enables it in Settings once
adb forward tcp:8360 tcp:8360
python -m axdroid pair                   # bridge · device · guard status
python -m axdroid gates --out gates.json # 9 hardware gates → evidence report
python -m axdroid serve --stdio          # MCP server for local agents
```

Consumers (no ADB): enable the service once in Settings → Accessibility, turn
on **Allow LAN** in the axdroid app, and point the agent at
`http://<phone-ip>:8360` with the pairing token shown on the phone
(`python -m axdroid observe --url http://phone:8360 --token XXXX-XXXX-…`).

### Claude Code / Codex / OpenCode / any MCP client

```json
{ "mcpServers": { "axdroid": { "command": "python", "args": ["-m", "axdroid", "serve", "--stdio"],
                               "cwd": "phone-use/android" } } }
```

`--listen host:port [--token t]` serves the same tools over Streamable HTTP
for LAN-shared hosts.

## Tools

| Tool | What it does |
|---|---|
| `phone_observe` | every on-screen window's tree with refs (`mode: lite` for small models) |
| `phone_find` | by text / role / resource id, with bounds; off-screen rows included |
| `phone_tap` | ref, text or `{x,y}` px; **auto-scrolls off-screen rows into view** (down, then up if a fling overshot); semantic click first, occlusion-checked gesture fallback; blind taps need `confirm: true` |
| `phone_type` | `ACTION_SET_TEXT`, read back on the phone; `submit` presses the IME action |
| `phone_scroll` | direction + `until:`; detects lists that lie about scrolling and switches to flings; re-verifies after momentum |
| `phone_swipe`, `phone_back`, `phone_button` | gestures, system back, home/recents/notifications/quick-settings/lock |
| `phone_launch`, `phone_apps` | open apps by name/package (dev-USB gets a true force-stop for `fresh`) |
| `phone_wait` | device-side, event-driven: text appear/disappear, or `stable:` seconds of quiet |
| `phone_run` | multi-step plan, one model round trip, stops at the first failure |
| `phone_status` | device, bridge, guard policy, custody, measured latencies |

## Owner controls (on the phone, outranking every agent)

* **Stop latch** — notification action, app button, or RPC; persists across
  restarts; acting refuses with `STOPPED_BY_OWNER`, reading keeps working.
* **Pairing token** — regenerated from the app; required for LAN, never needed
  on loopback (the USB link is the auth).
* **Journal** — every action and refusal on the phone (`journal.jsonl`,
  rotating) with actor and target hint; never typed text.
* **Guard** — payment/send/delete/permission labels need `confirm: true`;
  decided *on the device*, so it holds for every transport. Declines
  ("Don't allow", "Not now") are never blocked.

## What has been measured

Samsung Galaxy S21 FE (SM-G990B2, Android 16, One UI) — all numbers from this
device, reproducible with the tools in `tools/`:

* **Hardware gates: 9/9** — tree, semantic click, typed+verified text, gesture
  fallback, event-driven settle, custody, guard, and a 6-app sweep where every
  One UI app exposes a rich tree (Camera 23, Messages 15, Gallery 12, My Files
  and Contacts 11–12, Calendar 10 actionable elements). `gates-samsung-s21fe.json`
* **Task benchmark: 8/8 passed, 1 honest skip, 0 failures** (77.5 s total,
  ~19.1 k tokens of tool output) — including a full contacts round-trip
  *create → guard refuses Delete → confirmed delete*, search-driven Dark-mode
  navigation, Wi-Fi toggle with state verify, data extraction (SM-G990B2/DS),
  and a 5-step `phone_run` plan in one round trip. `tasks-samsung-s21fe.json`
* **Token economy** — median observe **827 tokens** compact vs 1,351 verbose
  (−40%: chrome windows hidden, package-stripped ids, homogeneous-list
  compaction); action diffs ≈ observe-size on page turns, far below full
  re-dumps on partial changes. `tokens-report.json`
* **Latency** — observe 34–56 ms device-side; semantic tap 35 ms raw, **~0.6–0.9 s
  end-to-end including settle+diff** (down from ~3 s with fixed waits); home
  button 610–742 ms; launch ~1.6 s. `BENCHMARK.md`
* **On-device MCP + cloud relay, verified on hardware** — the phone answers MCP
  at `/mcp` with format parity to the host engine, dials out to a relay through
  an HMAC-authenticated tunnel (auto-reconnect), mints its own one-time link
  codes, and full OAuth + observe/tap/home sessions run through the chain:
  `tools/check-ondevice-mcp.py`, `tools/check-relay-device.py`
* **Agent-in-the-loop** — an agent CLI (opencode, Claude Sonnet) drove the task
  suite through the phone's on-device MCP; every outcome device-verified, all
  calls metered on the phone. The run found and forced fixes to three on-device
  tools that the scripted suite had missed. [EVAL.md](EVAL.md)

24 offline tests cover the whole stack against a simulator speaking the
identical wire protocol, including a full relay+OAuth+tunnel e2e
(`tests/test_relay_e2e.py`).

## Three ways an agent can reach the phone

**1 · On-device MCP — zero host software.** The phone answers MCP itself at
`POST /mcp` (same 13 tools, same output format, guard/custody/journal included).
Over USB: `adb forward tcp:8360 tcp:8360`, then point any MCP client at
`http://127.0.0.1:8360/mcp`. On Wi-Fi (owner enabled LAN): the same URL on the
phone's IP with the pairing token.

**2 · Host MCP server (stdio).** For CLIs that prefer local servers:
`python -m axdroid serve --stdio` — identical tools, the same wire protocol.

**3 · Cloud relay — the phone dials out.** Nothing is opened on the phone's
network: the app keeps one outbound connection to a relay (`axdroid relay`,
the same wire protocol as the upcoming axphone relay for iPhone, so one relay can serve both
fleets). Agents reach the phone at `https://relay/h/<phone>/mcp` through
OAuth 2.1 with link-code consent minted *by the phone*:

```sh
# once, on the relay host (prints the phone's secret)
python -m axdroid relay --http 0.0.0.0:8080 --tunnel 0.0.0.0:7000 --add-hub myphone

# in the app: Cloud relay → host:port, id, secret, enable  (or over adb:
adb forward tcp:8360 tcp:8360   # then configure + ask for a link code)
curl -s http://127.0.0.1:8360/rpc -d '{"method":"relay_set","params":{"host":"relay.example.com:7000","id":"myphone","secret":"…","enabled":true}}' -H "Content-Type: application/json"
curl -s http://127.0.0.1:8360/rpc -d '{"method":"relay_link"}' -H "Content-Type: application/json"

# in the agent: https://relay.example.com/h/myphone/mcp — the approval page
# asks for the link code; tokens are audience-bound, phone:read-only grants
# exist, and the owner can revoke live on the relay.
```

Tokens are HS256 JWTs bound to the exact connector URL (RFC 8707); refresh
tokens rotate (reuse revokes the family); read-only approvals can observe but
not act; the scopes are enforced **again on the phone**, so the relay is never
trusted alone. Verified end-to-end on the S21 FE: `tools/check-relay-device.py`
(relay on the host, the phone dialing out through `adb reverse`, link code
minted by the phone, full OAuth, and observe/tap/home answered by the phone
through the whole chain).

## Agent-in-the-loop evaluation

`tools/eval-agent.py` runs the task suite with a real agent CLI (`opencode`,
the user's own model) driving the phone through the **on-device MCP endpoint**
— the exact product path, zero host software — and verifies every outcome from
the device itself. Every call is metered on the phone (journal: ms + output
chars), the CLI's token usage and cost are summed, and verifiers label their
evidence class (state / journal / reply). This is the eval that found and
forced the fixes to on-device `phone_scroll`, `phone_find` and `phone_type`.

## Licences

Host side, build script and app: axuse (same as the repository). No third-party
code is linked or vendored — the app uses only the Android platform, the host
side only the Python standard library.
