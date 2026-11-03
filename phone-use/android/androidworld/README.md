# AndroidWorld results with axdroid

axdroid is a phone-use library, not an agent. This folder is the adapter that runs an AI agent (here the Codex CLI with a model) on [AndroidWorld](https://github.com/google-research/android_world), the 116-task benchmark for phone agents, using axdroid to operate the phone. The agent reads the phone's accessibility tree through the axdroid app (no screenshots, no vision model); a few host-side helpers handle the screens that have no text (photos, drawings, mazes, video).

## Result

The full suite of 116 tasks, repeated three times, each task attempted once per run, scored by the benchmark's own checker (initialize the task, run the agent, check the device):

| | Codex + gpt-6.1-terra (low) | Codex + gpt-5.6-luna (max) |
|---|---|---|
| Tasks passed | **116 / 116** | **116 / 116** |
| Input tokens per task, median | 223k | 308k |
| Input tokens per task, 90th percentile | 682k | 1.07M |
| Input tokens, total | 39.3 M | 72.3 M |
| Agent time per task, median | 66 s | 78 s |
| Agent time per task, 90th percentile | 193 s | 259 s |
| Agent time, total | 3.2 h | 4.0 h |
| Tool calls, total | 1,576 | 2,497 |

Every task: [RESULTS_per_task.md](RESULTS_per_task.md).

How to read it:
- Tokens are the model's input tokens per task, cached tokens included (each request re-sends its context). Time is the agent's wall time per task; setup, checking and emulator restarts are excluded.
- These are our own runs on an Android emulator (Pixel 6, seed 8, fresh task parameters each time), with the Codex CLI as the agent loop. The full suite was run three times.
- Before each task the harness may re-create an app's database, re-send a setup text or restart the emulator; the agent itself is never retried. Those recoveries live in `run_suite.py`.
- For comparison, MobileRun reports 116/116 on AndroidWorld with gpt-6-astra (low reasoning), a screenshot on every step plus the accessibility tree, and 1,419 recorded interactions; it does not publish tokens or wall time.

## Run it yourself

Requirements: an AndroidWorld checkout (set `ANDROID_WORLD_REPO`, default `~/android_world`) and its Python environment, `adb` and the emulator on your PATH (or `AW_ADB`, `--emulator-exe`; `ANDROID_AVD_HOME` or `--avd-home` for the AVD folder), an Android emulator (AVD) with the benchmark's apps installed, the axdroid APK (`../axdroid.apk`, installed with `adb install -r` and enabled as an accessibility service), and the Codex CLI logged in.

```
python -m androidworld.run_suite --backend codex --model gpt-6.1-terra --effort low --lean --out out/run1
python -m androidworld.run_suite --tasks ContactsAddContact,ClockStopWatchRunning --out out/try   # a subset
```

Useful flags: `--tasks a,b,c` (subset), `--qemu-max-mb N` (restart the emulator between tasks when its host memory exceeds N MB), `--effort`, `--lean` (use the rules in `agent.py` as the whole system prompt), `--out DIR`.

`out/` holds run logs and a copy of your Codex login for the isolated agent home. It is git-ignored; never commit it.

## What is in here

| File | Purpose |
|---|---|
| `run_suite.py` | The runner: task setup, agent, official check, emulator recovery, per-task results |
| `agent.py` | The agent backends and the rules prompt (`AGENT_MD`) |
| `host_mcp.py`, `scan.py`, `ocr.py` | Host-side helpers: OCR, color and grid readers, video text, list scanning |
| `adb_bridge.py`, `axdroid_env.py` | Connect AndroidWorld's environment to the axdroid app |
