"""Run the AndroidWorld 116-task suite with axdroid as the device layer.

  python -m androidworld.run_suite --perform_setup          # first time
  python -m androidworld.run_suite                          # full suite
  python -m androidworld.run_suite --tasks ContactsAddContact,ClockStopWatchRunning
  python -m androidworld.run_suite --retry-failed out/run1  # re-run failures

Per task: official AndroidWorld `initialize_task` → opencode agent (axdroid
MCP) → official `is_successful` reward. Checkpoints after every task.
"""

from __future__ import annotations

import argparse
import html
import importlib
import json
import os
import re
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))              # axdroid package
AW_REPO = os.environ.get("ANDROID_WORLD_REPO") or os.path.join(os.path.expanduser("~"), "android_world")
sys.path.insert(0, AW_REPO)

from android_world import registry as aw_registry          # noqa: E402
from android_world import suite_utils                      # noqa: E402
from android_world.env import setup_device                 # noqa: E402
from android_world.utils import datetime_utils             # noqa: E402

from .agent import CodexAgent, OpencodeAgent                   # noqa: E402
from .axdroid_env import build_env                         # noqa: E402

ADB_DEFAULT = os.environ.get(
    "AW_ADB", __import__("shutil").which("adb") or "adb")
SERIAL_DEFAULT = os.environ.get("AW_SERIAL", "emulator-5554")

# IR tasks are scored from the agent's final reply → interaction_cache.
_IR_PREFIXES = ("SimpleCalendar", "Tasks", "SportsTracker", "Notes")


def is_ir_task(name: str) -> bool:
    return name.startswith(_IR_PREFIXES) and any(
        name.startswith(p) for p in _IR_PREFIXES)


def journal_len(session) -> int:
    try:
        return journal_marker(session)
    except Exception:
        return -1


def _qemu_mb(a) -> int:
    """Host working set of this run's emulator process, MB (0 if unknown).
    With swiftshader the qemu process grows ~12 GB/h on the host whatever
    `-memory` says — twice it pushed the box into low-memory kills."""
    import subprocess as sp
    try:
        out = sp.run(["powershell", "-NoProfile", "-Command",
                      "Get-CimInstance Win32_Process -Filter \"Name like 'qemu%'\" | "
                      f"Where-Object {{ $_.CommandLine -match 'port {a.serial.split('-')[1]}' }} | "
                      "ForEach-Object { [int]($_.WorkingSetSize/1MB) }"],
                     capture_output=True, text=True, timeout=30).stdout.split()
        return max((int(x) for x in out), default=0)
    except Exception:
        return 0


_LOG_PATH: list = [None]


def _log(msg: str):
    """console + run.log (main sets the path); module-level so the reboot
    helpers can log too — they used to NameError on main's local _log."""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if _LOG_PATH[0]:
        with open(_LOG_PATH[0], "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _wait_launcher(a, timeout: float = 60.0) -> bool:
    """After a boot the bridge answers before the device is usable: systemui
    (lock screen / boot UI) keeps focus and every app launch times out
    behind it (OsmAndFavorite, first task after a memory-guard reboot).
    Dismiss the keyguard, go home, and wait until the launcher has focus."""
    import subprocess as sp
    sh = lambda *c: sp.run([a.adb, "-s", a.serial, "shell", *c], capture_output=True,
                           text=True, timeout=30).stdout
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            sh("wm", "dismiss-keyguard")
            sh("input", "keyevent", "3")
            focus = sh("dumpsys", "window", "displays")
            line = next((l for l in focus.splitlines() if "mCurrentFocus" in l), "")
            if "launcher" in line.lower():
                return True
        except Exception:
            pass
        time.sleep(3)
    _log("launcher never took focus after the reboot")
    return False


def _bt_healthy(a) -> bool:
    """Can this boot's Bluetooth adapter switch on? Some cold boots come up
    with an adapter that stays OFF whatever is requested (`svc bluetooth
    enable` -> Success, manager "enabled: false"): Bluetooth tasks are then
    unpassable. The previous state is restored afterwards."""
    import subprocess as sp
    sh = lambda *c: sp.run([a.adb, "-s", a.serial, "shell", *c], capture_output=True,
                           text=True, timeout=30).stdout
    try:
        was_on = "enabled: true" in sh("dumpsys", "bluetooth_manager")
        if was_on:
            return True
        sh("svc", "bluetooth", "enable")
        ok = False
        for _ in range(8):
            time.sleep(1.5)
            if "enabled: true" in sh("dumpsys", "bluetooth_manager"):
                ok = True
                break
        sh("svc", "bluetooth", "disable")
        return ok
    except Exception:
        return True   # unknown: do not loop on reboots


def _reboot_emulator(a, env, bt_retry: bool = True) -> bool:
    """Kill and relaunch this run's emulator, wait for boot, re-prepare.
    Long benchmark grinds wedge the goldfish I/O (adb push timeouts, lost
    /data dirs) — a reboot clears it; the AVD's userdata survives."""
    import subprocess as sp
    # Flush the guest filesystem and quit the emulator cleanly BEFORE any
    # kill: a task setup that had just written an app database (Joplin
    # joplin.sqlite, Broccoli, OpenTracks) left it 0 bytes after the hard
    # kill, and the next setup died with "no such table" — even after a
    # second reboot, since the empty file stays empty.
    try:
        for _ in range(2):
            sp.run([a.adb, "-s", a.serial, "shell", "sync"], capture_output=True, timeout=30)
            time.sleep(1)
        sp.run([a.adb, "-s", a.serial, "emu", "kill"], capture_output=True, timeout=30)
        time.sleep(5)
    except Exception:
        pass
    qemu = sp.run(["powershell", "-NoProfile", "-Command",
                   "Get-CimInstance Win32_Process -Filter \"Name like 'qemu%'\" | "
                   f"Where-Object {{ $_.CommandLine -match 'port {a.serial.split('-')[1]}' }} | "
                   "Select-Object -ExpandProperty ProcessId"],
                  capture_output=True, text=True).stdout.strip().splitlines()
    for pid in qemu:
        sp.run(["taskkill", "/PID", pid.strip(), "/T", "/F"], capture_output=True)
    time.sleep(5)
    exe = a.emulator_exe
    if not os.path.exists(exe):
        _log(f"emulator exe not found: {exe}")
        return False
    envs = dict(os.environ)
    envs["ANDROID_AVD_HOME"] = a.avd_home
    envs["ANDROID_SDK_ROOT"] = os.path.dirname(os.path.dirname(exe))
    envs["ANDROID_HOME"] = envs["ANDROID_SDK_ROOT"]
    sp.Popen([exe, "-avd", a.avd, "-no-snapshot", "-no-window", "-no-audio",
              "-no-boot-anim", "-gpu", "swiftshader_indirect",
              "-port", a.serial.split("-")[1], "-memory", str(a.emulator_mem)],
             env=envs, stdout=sp.DEVNULL, stderr=sp.DEVNULL)
    adb = sp.run(["powershell", "-NoProfile", "-Command",
                  f"& '{a.adb}' -s {a.serial} shell getprop sys.boot_completed"],
                 capture_output=True, text=True)
    deadline = time.time() + 300
    while time.time() < deadline:
        r = sp.run([a.adb, "-s", a.serial, "shell", "getprop", "sys.boot_completed"],
                   capture_output=True, text=True, timeout=30)
        if r.stdout.strip() == "1":
            break
        time.sleep(10)
    else:
        return False
    sp.run([a.adb, "-s", a.serial, "root"], capture_output=True, timeout=30)
    time.sleep(3)
    # the bridge's RPC server starts some seconds after boot_completed:
    # prepare (which calls status) must retry, not fail the next task
    for attempt in range(7):
        try:
            prepare_device(env, a.port)
            _wait_launcher(a)
            if bt_retry and not _bt_healthy(a):
                _log("this boot's Bluetooth adapter cannot switch on — rebooting once more")
                return _reboot_emulator(a, env, bt_retry=False)
            return True
        except BaseException as e:   # prepare_device may SystemExit
            last = e
            _log(f"post-reboot prepare attempt {attempt + 1}/7: {type(e).__name__}: {str(e)[:160]}")
            time.sleep(10)
    _log(f"post-reboot prepare failed: {last}")
    return False


def _revive_bridge(env, a=None) -> bool:
    """The on-device RPC server can die while its accessibility service
    stays bound (observed after emulator wedges): connections are accepted
    and dropped. Restart the app + toggle its service; fall back to a full
    emulator reboot if that doesn't bring it back."""
    b = env.controller.env
    xml = ('<?xml version="1.0" encoding="utf-8" standalone="yes" ?> '
           '<map> <boolean name="server_enabled" value="true" /> </map>')
    for attempt in range(3):
        try:
            env.controller.session.p.call("status")
            return True
        except Exception:
            pass
        try:
            # an agent can flip the app's own persisted server_enabled kill
            # switch through its settings UI — force it back on (root)
            b.shell("sh", "-c", f"echo '{xml}' > "
                    "/data/data/com.axuse.axdroid/shared_prefs/axdroid.xml")
            b.shell("am", "force-stop", "com.axuse.axdroid")
        except Exception:
            pass
        time.sleep(1.5)
        try:
            b.shell("settings", "--user", "0", "put", "secure",
                    "enabled_accessibility_services", "''")
            time.sleep(0.8)
            b.shell("settings", "--user", "0", "put", "secure",
                    "enabled_accessibility_services",
                    "com.axuse.axdroid/com.axuse.axdroid.AxService")
            b.shell("settings", "--user", "0", "put", "secure",
                    "accessibility_enabled", "1")
        except Exception:
            pass
        time.sleep(4)
        try:
            b._adb("forward", "tcp:8360", "tcp:8360")
        except Exception:
            pass
    if a is not None and _reboot_emulator(a, env):
        return True
    return False


def journal_marker(session) -> int:
    """Timestamp of the newest journal event (device clock — monotonic
    within a run; the length-based diff saturates once the on-device journal
    hits its cap)."""
    try:
        evs = session.p.call("journal", n=1).get("events", [])
        return evs[-1].get("ts", 0) if evs else 0
    except Exception:
        return -1


def journal_calls(session, before: int) -> int:
    if before < 0:
        return -1
    try:
        evs = session.p.call("journal", n=999999).get("events", [])
        return sum(1 for e in evs if e.get("ts", 0) >= before and
                   e.get("type") == "mcp_tool")
    except Exception:
        return -1


def ensure_osmand_map(b) -> bool:
    """The benchmark's OsmAnd setup pushes Liechtenstein_europe.obf; on this
    AVD it has vanished more than once (emulator wedge/kill loses recent
    sdcard writes) — town searches then find nothing. Re-assert it from the
    framework's own app_data cache and verify it landed."""
    maps = "/storage/emulated/0/Android/data/net.osmand/files/"
    obf = "Liechtenstein_europe.obf"
    try:
        if obf in b.shell("ls", maps):
            return True
        src = os.path.join(tempfile.gettempdir(), "android_world", "app_data", obf)
        if not os.path.exists(src):
            print(f"osmand map missing and no local copy at {src}")
            return False
        b.shell("am", "force-stop", "net.osmand")
        b._adb("push", src, maps + obf, timeout=120)
        b.shell("chcon", "u:object_r:media_rw_data_file:s0", maps + obf)
        b.shell("sync")
        ok = obf in b.shell("ls", maps)
        print(f"restored OsmAnd map {obf}: {'ok' if ok else 'FAILED'}")
        return ok
    except Exception as e:
        print(f"osmand map check skipped: {e}")
        return False


def prepare_device(env, port: int = 8360) -> None:
    b = env.controller.env
    # cold boot: wait for the a11y service to actually BIND (dumpsys is
    # cheap and doesn't disturb the app), then wait for the RPC to answer.
    # Force-stopping on every attempt restarts the binding clock, so only
    # escalate to app restarts after generous plain waiting.
    def _service_bound() -> bool:
        try:
            out = b.shell("dumpsys", "accessibility", timeout=15)
            return "com.axuse.axdroid/com.axuse.axdroid.AxService" in out and \
                "Bound services" in out
        except Exception:
            return False

    # a reboot can come back WITHOUT the app (seen after a crashed reboot:
    # the runner then retried "bridge not ready" for 40 min) — reinstall it
    try:
        if "com.axuse.axdroid" not in b.shell("pm", "list", "packages", "com.axuse.axdroid", timeout=30):
            apk = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axdroid.apk")
            print(f"axdroid is not installed — installing {apk}", flush=True)
            b._adb("install", "-r", "-t", "-g", apk, timeout=180)
            b.shell("settings", "--user", "0", "put", "secure", "enabled_accessibility_services",
                    "com.axuse.axdroid/com.axuse.axdroid.AxService")
            b.shell("settings", "--user", "0", "put", "secure", "accessibility_enabled", "1")
    except Exception as e:
        print(f"axdroid install check failed: {e}", flush=True)
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            try:
                b._adb("forward", f"tcp:{port}", "tcp:8360")
            except Exception:
                pass
            env.controller.session.p.call("status")
            break
        except Exception as e:
            if not _service_bound() and time.time() > deadline - 120:
                # not bound and time is running out — re-enable the service
                try:
                    b.shell("settings", "--user", "0", "put", "secure",
                            "enabled_accessibility_services",
                            "com.axuse.axdroid/com.axuse.axdroid.AxService")
                    b.shell("settings", "--user", "0", "put", "secure",
                            "accessibility_enabled", "1")
                except Exception:
                    pass
            time.sleep(6)
    else:
        raise RuntimeError("axdroid bridge did not come up after boot")
    b.ensure_root()
    # reboots/pm-clear can revert the default SMS app; the SMS tasks need
    # Simple SMS Messenger to hold the role (their sends must persist)
    try:
        b.shell("settings", "put", "secure", "sms_default_application",
                "com.simplemobiletools.smsmessenger")
    except Exception:
        pass
    ensure_osmand_map(b)
    # clipper (clipboard bridge for 3 tasks) must complete its onboarding
    # once: a permission dialog + an old-version alert; the benchmark's own
    # setup clicks ("Continue"/"OK") never matched these screens
    try:
        from axdroid.client import PhoneError
        sess = env.controller.session
        sess.launch("clipper")
        time.sleep(1.5)
        for label in ("Allow", "Continue", "OK"):
            for _ in range(2):
                try:
                    sess.tap(label, confirm=True)
                    time.sleep(1.0)
                    break
                except PhoneError:
                    break
        txt = sess.observe()["text"]
        if "This app was built for an older" in txt:
            try:
                sess.tap("OK", confirm=True)
                time.sleep(1.0)
            except PhoneError:
                pass
        sess.button("home")
    except Exception as e:
        _dbg = getattr(env.controller, "session", None)
        print(f"clipper onboarding skipped: {e}")
    # the demo scoreboard app (com.example.androidworld) starts a persistent
    # overlay that shows up as an extra window in every a11y tree and can
    # occlude taps — it is not part of scoring; disable it
    try:
        b.shell("am", "force-stop", "com.example.androidworld")
        b.shell("pm", "disable-user", "--user", "0", "com.example.androidworld")
    except Exception:
        pass
    # wake + unlock + keep awake while plugged
    b.shell("wm", "dismiss-keyguard")
    b.shell("svc", "power", "stayon", "true")
    # install + enable the axdroid accessibility service
    r = b._adb("shell", "pm", "list", "packages")
    if b"com.axuse.axdroid" not in (r.stdout or b""):
        apk = os.path.join(os.path.dirname(HERE), "axdroid.apk")
        rr = b._adb("install", "-r", "-t", apk, timeout=120)
        if rr.returncode != 0:
            raise SystemExit(f"axdroid.apk install failed: {rr.stderr!r}")
    b.shell("settings", "--user", "0", "put", "secure",
            "enabled_accessibility_services", "com.axuse.axdroid/com.axuse.axdroid.AxService")
    b.shell("settings", "--user", "0", "put", "secure", "accessibility_enabled", "1")
    time.sleep(1.5)
    env.controller.ensure_service()
    info = env.controller.session.p.call("status")
    dev = info.get("device") or {}
    print(f"axdroid bridge: {dev.get('model', '?')} · {dev.get('os', '?')} · "
          f"keys={sorted(info)[:6]}")


def main() -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--adb", default=ADB_DEFAULT)
    ap.add_argument("--serial", default=SERIAL_DEFAULT)
    ap.add_argument("--port", type=int, default=8360,
                    help="host port forwarded to this device's axdroid RPC")
    ap.add_argument("--tasks", default=None, help="comma list; default all")
    ap.add_argument("--perform-setup", action="store_true",
                    help="install benchmark apps + one-time device setup")
    ap.add_argument("--setup-only", action="store_true",
                    help="do perform_setup then exit")
    ap.add_argument("--out", default=None, help="run dir (default out/run-<ts>)")
    ap.add_argument("--retry-failed", default=None,
                    help="run dir whose failures should be retried")
    ap.add_argument("--retry-rounds", type=int, default=1,
                    help="with --retry-failed: keep retrying failures this "
                         "many rounds while progress is being made")
    ap.add_argument("--rerun", action="store_true",
                    help="ignore existing results in --out and run everything")
    ap.add_argument("--model", default=None)
    ap.add_argument("--effort", default="xhigh",
                    help="codex model_reasoning_effort (low/medium/high/xhigh/max)")
    ap.add_argument("--screenshots", choices=["on", "off"], default=None,
                    help="set the phone owner's screenshot switch before every task (default: leave it as it is)")
    ap.add_argument("--lean", action="store_true",
                    help="codex: our rules as the system prompt, codex's coding "
                         "prefix/tools/skills off (fixed tokens per request)")
    ap.add_argument("--backend", default="opencode", choices=["opencode", "codex"],
                    help="agent CLI backend (codex = gpt-5.6-luna fallback)")
    ap.add_argument("--avd", default="AndroidWorldAx")
    ap.add_argument("--avd-home",
                    default=os.environ.get("ANDROID_AVD_HOME") or os.path.join(os.path.expanduser("~"), ".android", "avd"))
    ap.add_argument("--emulator-exe",
                    default=__import__("shutil").which("emulator") or "emulator")
    ap.add_argument("--emulator-mem", type=int, default=4096)
    ap.add_argument("--qemu-max-mb", type=int, default=10000,
                    help="reboot the emulator BETWEEN tasks when its host "
                         "working set exceeds this (0 = never)")
    ap.add_argument("--timeout-base", type=int, default=360,
                    help="agent wall-time cap per task = base + 45s*max_steps")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--family", default=aw_registry.TaskRegistry.ANDROID_WORLD_FAMILY)
    ap.add_argument("--seed", type=int, default=8)
    ap.add_argument("--shard", default=None, help="i/n to run subset i of n")
    a = ap.parse_args()

    env = build_env(a.adb, a.serial, a.port)
    # right after a boot the bridge RPC starts some seconds late
    for attempt in range(7):
        try:
            prepare_device(env, a.port)
            break
        except BaseException as e:
            if attempt == 6:
                raise
            print(f"bridge not ready ({type(e).__name__}) — retrying in 10s", flush=True)
            time.sleep(10)
    if not _bt_healthy(a):
        print("this boot's Bluetooth adapter cannot switch on — rebooting the emulator", flush=True)
        _reboot_emulator(a, env)

    # host-side OCR MCP server (in-process, HTTP — stdio spawns proved flaky)
    import threading
    from .host_mcp import serve_http
    host_port = 8369 + (a.port - 8360)
    threading.Thread(target=serve_http, args=(host_port, a.adb, a.serial),
                     daemon=True).start()

    if a.perform_setup:
        print("performing benchmark app setup (one time)…")
        datetime_utils.setup_datetime(env.controller.env)
        setup_device.setup.setup_apps(env)
        print("app setup done")
        if a.setup_only:
            return 0

    # Patch: find_and_click_element matches case-SENSITIVELY with distance 1
    # — the benchmark's "SAVE" doesn't match this Contacts build's "Save",
    # so every framework add_contact (SMS setups) died in setup. Wrap it
    # with a case-insensitive fallback.
    from android_world.env import actuation as aw_act
    from android_world.env import json_action as aw_json
    _orig_find_click = aw_act.find_and_click_element

    def _ci_find_and_click(element_text, env, case_sensitive=False):
        try:
            return _orig_find_click(element_text, env, case_sensitive)
        except Exception:
            els = env.get_ui_elements()
            low = str(element_text).lower()
            for i, e in enumerate(els):
                if ((e.text or "").strip().lower() == low
                        or (e.content_description or "").strip().lower() == low):
                    aw_act.execute_adb_action(
                        aw_json.JSONAction(action_type="click", index=i),
                        els, (0, 0), env)
                    return None
            raise

    aw_act.find_and_click_element = _ci_find_and_click
    import android_world.env.tools as aw_tools
    aw_tools.actuation.find_and_click_element = _ci_find_and_click

    task_registry = aw_registry.TaskRegistry().get_registry(family=a.family)
    names = sorted(task_registry)
    if a.shard:
        i, n = (int(x) for x in a.shard.split("/"))
        names = [t for idx, t in enumerate(names) if idx % n == i]
    if a.tasks:
        keep = {t.strip() for t in a.tasks.split(",")}
        unknown = keep - set(names)
        if unknown:
            print(f"unknown tasks: {sorted(unknown)}")
            return 2
        names = [t for t in names if t in keep]

    out_dir = a.out or os.path.join(HERE, "out", "run-" + time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    results_path = os.path.join(out_dir, "results.json")

    results = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "tasks": {}, "ok": False}
    if a.retry_failed:
        with open(os.path.join(a.retry_failed, "results.json"), encoding="utf-8") as f:
            prev = json.load(f)
        names = [n for n, r in prev["tasks"].items() if not r.get("ok")]
        results["retry_of"] = os.path.abspath(a.retry_failed)
    elif os.path.exists(results_path) and not a.rerun:
        with open(results_path, encoding="utf-8") as f:
            results = json.load(f)
        print(f"resuming: {len(results['tasks'])} already done")

    if a.backend == "codex":
        agent = CodexAgent(model=a.model or "gpt-5.6-luna", effort=a.effort,
                           port=a.port, host_mcp_port=host_port, lean=a.lean)
        results["agent"] = {"backend": "codex", "model": a.model or "gpt-5.6-luna",
                            "effort": a.effort, "lean": a.lean}
    else:
        agent = OpencodeAgent(model=a.model, port=a.port, host_mcp_port=host_port)
    _LOG_PATH[0] = os.path.join(out_dir, "run.log")

    todo = [n for n in names if n not in results["tasks"]]
    if a.limit:
        todo = todo[:a.limit]
    _log(f"suite: {len(names)} tasks ({len(todo)} to run) → {out_dir}")

    attempted_prev: list[str] = []
    rounds_left = max(1, a.retry_rounds)
    while todo and rounds_left >= 1:
        if rounds_left < max(1, a.retry_rounds):
            _log(f"retry round ({max(1, a.retry_rounds) - rounds_left + 1}): "
                 f"{len(todo)} still failing")
            for n in todo:
                results["tasks"].pop(n, None)   # re-attempt fresh
        rounds_left -= 1
        attempted = list(todo)
        infra_requeued: set = set()
        for idx, name in enumerate(todo):
            if a.qemu_max_mb:
                mb = _qemu_mb(a)
                if mb > a.qemu_max_mb:
                    _log(f"emulator host memory {mb} MB > {a.qemu_max_mb} — rebooting between tasks")
                    _reboot_emulator(a, env)
            try:
                cls = task_registry[name]
                try:
                    params = cls.generate_random_params()
                    params["seed"] = a.seed
                    task = cls(params)
                    task.set_device_time(env)
                except Exception as e:
                    results["tasks"][name] = {"ok": False, "error": f"construct: {e}"}
                    _save(results, results_path)
                    continue

                entry = {"goal": task.goal, "complexity": task.complexity}
                t0 = time.time()
                max_steps = max(10, int(10 * (task.complexity or 1)))
                timeout = a.timeout_base + 45 * max_steps
                # SMS tasks resolve contacts by name ("text Leo Gonzalez") —
                # stale contacts from earlier contacts tasks create duplicate
                # wrong-number matches. Clear BEFORE initialize_task: the SMS
                # setups seed their own contacts (contacts_utils.add_contact)
                # and a later clear would wipe them.
                if name.startswith(("SimpleSms", "MarkorCreateNoteAndSms")):
                    try:
                        # framework deletion (NOT pm clear: clearing the
                        # provider breaks account state and the SMS app's
                        # contact search stops seeing intent-added contacts)
                        from android_world.utils import contacts_utils as _cu
                        _cu.delete_contacts(env.controller)
                        time.sleep(1.0)
                    except Exception:
                        pass
                if name.startswith("OsmAnd"):
                    ensure_osmand_map(env.controller.env)
                # Retro tasks: the playing queue survives task setups (they
                # reset playlists, not the queue), so songs from earlier
                # attempts made RetroPlayingQueue unpassable. Re-run the
                # benchmark's OWN app setup (clear data, grant its permissions,
                # first launch) before initialize_task.
                if name.startswith("Retro"):
                    try:
                        from android_world.env.setup_device import apps as _aw_apps
                        _aw_apps.RetroMusicApp.setup(env)
                    except Exception as e:
                        _log(f"Retro app reset failed: {type(e).__name__}: {str(e)[:120]}")
                # VLC tasks: their setup deletes the Media rows and writes new
                # files, but VLC only indexes them on a scan of its library
                # folders — and on this AVD Internal memory is no longer one,
                # so the library stayed "No media" and adds were dropped (both
                # VLC tasks failed at the same slot on repeated runs). Same
                # remedy: the benchmark's own VlcApp.setup first.
                if name.startswith("Vlc"):
                    try:
                        from android_world.env.setup_device import apps as _aw_apps
                        _aw_apps.VlcApp.setup(env)
                    except Exception as e:
                        # "GRANT PERMISSION not found": the uid keeps "manage
                        # all files" across the data clear, so VLC skips that
                        # onboarding screen — the reset itself is done
                        note = " (expected: permission already granted)" if "not found" in str(e) else ""
                        _log(f"VLC app reset: {type(e).__name__}: {str(e)[:120]}{note}")
                # (after the setup, below: SimpleSmsResend baseline re-read)
                # SimpleSms* setups send a text by opening the SMS app on a
                # SENDTO intent, waiting a fixed 5 s and tapping its send button.
                # On the first launch after a reboot the app is slower than that,
                # the tap misses and the text stays a draft in the compose box
                # (SimpleSmsResend: no sent message to resend).
                # Start the app once and let it settle so the setup meets a warm app.
                if name.startswith("SimpleSms"):
                    try:
                        env.controller.env.shell("monkey", "-p", "com.simplemobiletools.smsmessenger",
                                                 "-c", "android.intent.category.LAUNCHER", "1")
                        time.sleep(8)
                        env.controller.env.shell("input", "keyevent", "3")
                        time.sleep(1)
                        # the setups text the emulator and wait a fixed 5 s; right
                        # after a boot the telephony stack takes much longer to
                        # deliver (setup: "most recent message is not what is
                        # expected" / empty inbox). Wait until a probe text
                        # actually arrives before the setup starts.
                        b = env.controller.env
                        from android_world.env import adb_utils as _adb_p
                        _adb_p.text_emulator(env.controller, "5550100", "axuse readiness probe")
                        for _ in range(20):
                            time.sleep(3)
                            got = b.shell("content", "query", "--uri", "content://sms/inbox",
                                          "--projection", "body")
                            if "axuse readiness probe" in str(got):
                                break
                        else:
                            _log("SMS stack: probe text not delivered after 60 s")
                    except Exception as e:
                        _log(f"SMS app warm-up failed: {type(e).__name__}: {str(e)[:120]}")
                try:
                    task.initialize_task(env)
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    # "no such table": the app's database file is empty (0 bytes
                    # after a hard emulator kill; it never repairs itself, a
                    # second reboot does not help). Re-run the benchmark's OWN
                    # setup for that app (clear data, first launch) so the app
                    # re-creates its database, then retry the task setup once.
                    if "no such table" in err or "app_db does not exist" in err or "databases does not exist" in err or "has no column named" in err:
                        app_cls = next((c for p, c in _DB_APPS if name.startswith(p)), None)
                        if app_cls:
                          # up to 3 repairs: a first start after a wipe can need a
                          # long time (and a second launch) before the app has
                          # created every table (Retro's PlaylistEntity, Joplin's
                          # migrated schema); each round waits longer
                          for rep in range(3):
                            try:
                                from android_world.env.setup_device import apps as _aw_apps
                                _log(f"setup found an empty app database ({err[:60]}) — "
                                     f"re-running the benchmark's {app_cls} setup (repair {rep + 1}/3)")
                                app_setup = getattr(_aw_apps, app_cls)
                                try:
                                    app_setup.setup(env)
                                except ValueError as e_s:
                                    # "GRANT PERMISSION not found": the uid keeps
                                    # "manage all files" across the data clear, so
                                    # VLC skips that screen; the clear itself is
                                    # done. Aborting here left VLC never relaunched
                                    # and its database missing (VlcCreateTwoPlaylists)
                                    if "not found" not in str(e_s):
                                        raise
                                    _log(f"{app_cls} setup: {str(e_s)[:60]} (expected, permission already granted)")
                                # the official setup opens the app for only ~2 s,
                                # too short on a cold boot for the app to create
                                # its database: open it again and give it time
                                from android_world.env import adb_utils as _adb_u
                                pkg = _adb_u.extract_package_name(_adb_u.get_adb_activity(app_setup.app_name))
                                env.controller.env.shell("monkey", "-p", pkg, "-c",
                                                         "android.intent.category.LAUNCHER", "1")
                                time.sleep(12 + 15 * rep)
                                for _ in range(12):
                                    ls = env.controller.env.shell("ls", f"/data/data/{pkg}/databases")
                                    if ls and "No such file" not in ls and "does not exist" not in ls:
                                        break
                                    time.sleep(5)
                                env.controller.env.shell("input", "keyevent", "3")
                                time.sleep(1)
                                task = cls(params)
                                task.initialize_task(env)
                                err = None
                                break
                            except Exception as e3:
                                err = f"{type(e3).__name__}: {e3}"
                                if not any(s_ in err for s_ in ("no such table", "does not exist",
                                                                "has no column named")):
                                    break
                    # The benchmark's clipboard setup reads the clipboard through
                    # the Clipper app and raises when it is not in the foreground
                    # at that instant (MarkorCreateNoteFromClipboard):
                    # go home, give it a moment, and run the task's setup again.
                    # (same family: a setup that drives the UI by button label —
                    # `Target text "SAVE" not found`, SimpleSmsSendReceivedAddress
                    # right after a reboot — missed its screen; and
                    # SimpleSmsReplyMostRecent "Unexpected initial state": the
                    # setup texts the emulator and waits a fixed 5 s, but on a
                    # freshly booted emulator the texts arrive late/out of order)
                    for _try in range(3):
                        if not (err and ("must be in the foreground" in err or "Target text" in err
                                         or "Unexpected initial state" in err
                                         or (name.startswith("SimpleSms") and "list index out of range" in err))):
                            break
                        try:
                            _log(f"setup transient ({err[:70]}) — retrying the setup ({_try + 1}/3)")
                            env.controller.env.shell("input", "keyevent", "3")
                            # SMS: the failed attempt's texts may still be in flight;
                            # let them land first or they arrive AFTER the retry's
                            # and become the "most recent" message
                            time.sleep(4 + 8 * _try + (30 if name.startswith("SimpleSms") else 0))
                            task = cls(params)
                            task.initialize_task(env)
                            err = None
                        except Exception as e4:
                            err = f"{type(e4).__name__}: {e4}"
                    # "no such table": the setup read an app database that the
                    # app had not created yet (first start after a reboot)
                    # emulator-wedge signature: reboot and retry the setup, up to
                    # 3 times (one reboot was not enough: the next adb call timed
                    # out again right after the boot, SimpleSmsSendClipboardContent)
                    for _rb in range(3):
                        if not (err and any(s in err for s in ("timeout", "AdbControllerError",
                                                  "does not exist", "did not take effect",
                                                  "no such table"))):
                            break
                        _log(f"setup transient ({err[:90]}) — rebooting emulator ({_rb + 1}/3)")
                        if _reboot_emulator(a, env):
                            try:
                                task = cls(params)
                                task.initialize_task(env)
                                err = None
                            except Exception as e2:
                                err = f"post-reboot: {type(e2).__name__}: {e2}"
                    if err:
                        entry.update(ok=False, error=f"setup: {err}",
                                     trace=traceback.format_exc()[-2000:])
                        results["tasks"][name] = entry
                        _save(results, results_path)
                        _log(f"SETUP-FAIL {name}: {err[:110]}")
                        continue

                # SimpleSmsResend counts the sent texts 3 s after the setup's own
                # send. Right after a reboot the text can reach the SMS store after
                # that count, and the final check (count + 1) then reads one too
                # high though the agent did the task. Re-read the baseline, with the
                # benchmark's own counter, once the SMS stack has settled.
                # The setup's send also silently misses its tap on a cold boot:
                # the text then stays a "Draft" in the conversation list, nothing
                # was sent and there is nothing to resend (4 of 8 reruns after a
                # reboot). Look for the draft marker and redo the setup.
                if name == "SimpleSmsResend":
                    b = env.controller.env
                    fails, rebooted, setup_ok = 0, False, True
                    for attempt in range(6):
                        if fails >= 3 and not rebooted:
                            rebooted = True
                            # three redos did not help (or one crashed): the SMS
                            # stack is wedged — reboot, then set up from scratch
                            _log("SimpleSmsResend: setup still broken - rebooting emulator")
                            try:
                                task.tear_down(env)
                            except Exception:
                                pass
                            if _reboot_emulator(a, env):
                                try:
                                    setup_ok = False
                                    task = cls(params)
                                    task.initialize_task(env)
                                    setup_ok = True
                                except Exception as e:
                                    _log(f"SMS setup after reboot failed: {type(e).__name__}: {str(e)[:100]}")
                        try:
                            # the setup leaves the app on the compose screen: an
                            # unsent text is still in its (EditText) message box
                            b.shell("uiautomator", "dump", "/sdcard/aw_sms.xml")
                            ui = b.shell("cat", "/sdcard/aw_sms.xml")
                            # an empty box reports its hint as text, so match the
                            # setup's own message, not "any text"
                            boxes = re.findall(r'<node [^>]*class="android\.widget\.EditText"[^>]*>', ui)
                            unsent = any(str(params.get("message", "@@")) in html.unescape(bx) for bx in boxes)
                            if not unsent and setup_ok:
                                break
                            fails += 1
                            _log(f"SimpleSmsResend setup left the text unsent in the compose box "
                                 f"(attempt {attempt + 1}) - redoing setup")
                            task.tear_down(env)
                            time.sleep(2)
                            setup_ok = False   # a redo that crashes leaves an empty box too
                            task = cls(params)
                            task.initialize_task(env)
                            setup_ok = True
                        except Exception as e:
                            # a failed check or redo leaves the setup in doubt: go on
                            # to the next attempt (the reboot at attempt 3)
                            _log(f"SMS draft check failed: {type(e).__name__}: {str(e)[:100]}")
                            fails += 1
                    # after a reboot the first text can sit in the outbox ("Sending…")
                    # for a while; a baseline taken then undercounts it and the final
                    # count comes out one too high (2 of 10 reruns). Wait until the
                    # thread no longer shows it, then take the baseline.
                    try:
                        for _ in range(15):
                            b.shell("uiautomator", "dump", "/sdcard/aw_sms.xml")
                            if "Sending" not in b.shell("cat", "/sdcard/aw_sms.xml"):
                                break
                            time.sleep(3)
                        else:
                            _log("SimpleSmsResend: first text still 'Sending…' after 45 s")
                        time.sleep(2)
                        task.before_messages = task.get_sent_messages(env.controller)
                    except Exception as e:
                        _log(f"sent-message baseline re-read failed: {type(e).__name__}: {str(e)[:100]}")
                # The setup of a task that works on a RECEIVED text sends it to the
                # emulator and waits a fixed second. On a rough boot the text is
                # sometimes never delivered (SimpleSmsSendReceivedAddress: the app
                # opened empty and the agent searched for 20 min, even switching the
                # default SMS app). Look for the task's own message text in the
                # inbox (a task parameter, not the checker); redo the setup if absent.
                if name == "SimpleSmsSendReceivedAddress" and params.get("message"):
                    for _try in range(3):
                        got = ""
                        # the contacts the setup adds must be there, once each, and no
                        # leftover contact may share a name with them: a half-failed
                        # contact add (or a survivor of the previous task) made the
                        # SMS app list other people (the agent texted the wrong one)
                        names, contacts_ok = [], False
                        want = {str(params.get("name1")), str(params.get("name2"))}
                        # poll: both the contact and the text land a little after
                        # the setup returns
                        for _ in range(10):
                            try:
                                got = env.controller.env.shell("content", "query", "--uri", "content://sms/inbox",
                                                               "--projection", "body")
                                cq = env.controller.env.shell("content", "query", "--uri",
                                                              "content://com.android.contacts/data/phones",
                                                              "--projection", "display_name")
                                names = [m.strip() for m in re.findall(r"display_name=(.+)", str(cq))]
                            except Exception:
                                got = ""
                            contacts_ok = (all(names.count(w) == 1 for w in want)
                                           and not [n for n in names if n not in want])
                            if contacts_ok and str(params["message"]) in str(got):
                                break
                            time.sleep(3)
                        if str(params["message"]) in str(got) and contacts_ok:
                            break
                        _log(f"{name}: setup inconsistent (text arrived: {str(params['message']) in str(got)}, "
                             f"contacts as expected: {contacts_ok}, saw {names}) - redoing setup ({_try + 1}/3)")
                        try:
                            task.tear_down(env)
                            time.sleep(5)
                            task = cls(params)
                            task.initialize_task(env)
                        except Exception as e:
                            _log(f"{name}: setup redo failed: {type(e).__name__}: {str(e)[:100]}")
                # SMS tasks resolve contacts by name ("text Leo Gonzalez") — stale
                # contacts from earlier contacts tasks create duplicate wrong-number
                # matches. Clear BEFORE the setup: the SMS setups seed their own
                # contacts (contacts_utils.add_contact) and this clear must not
                # wipe them — that is how SimpleSmsSendReceivedAddress failed.

                # mirror agent.reset(start_on_home_screen=True): home + unlock
                env.controller.env.shell("wm", "dismiss-keyguard")
                env.reset(go_home=True)
                # freeze the clock: some setups (browser) re-enable auto time for a
                # moment, and the emulator's NITZ then pushes host time — undo that
                # before the agent runs so time-anchored state stays consistent
                b = env.controller.env
                b.shell("settings", "put", "global", "auto_time", "0")
                b.shell("settings", "put", "global", "auto_time_zone", "0")
                # setups write bluetooth_on, but after a cold emulator boot the
                # adapter can stay OFF (settings 1, manager "enabled: false"):
                # the UI the agent sees then contradicts the requested state.
                # Make the setup's own request take effect before the agent runs.
                if not _sync_bluetooth(b) and name.startswith("SystemBluetooth"):
                    # a boot whose adapter never reaches the requested state
                    # (setting 1, adapter OFF): the agent's switch-off then
                    # finds nothing to turn off and the task fails whatever it
                    # does (SystemBluetoothTurnOff).
                    # Reboot and redo the task's own setup once.
                    _log("Bluetooth adapter did not reach the setup's state — "
                         "rebooting and redoing the setup")
                    if _reboot_emulator(a, env):
                        try:
                            task = cls(params)
                            task.initialize_task(env)
                            env.reset(go_home=True)
                            b = env.controller.env
                            b.shell("settings", "put", "global", "auto_time", "0")
                            b.shell("settings", "put", "global", "auto_time_zone", "0")
                            _sync_bluetooth(b)
                        except Exception as e:
                            _log(f"Bluetooth setup redo failed: {type(e).__name__}: {str(e)[:100]}")
                time.sleep(0.5)
                if a.screenshots:
                    try:
                        import urllib.request as _ur
                        _ur.urlopen(_ur.Request(f"http://127.0.0.1:{a.port}/rpc", method="POST",
                                                headers={"Content-Type": "application/json"},
                                                data=json.dumps({"method": "policy_set",
                                                                 "params": {"screenshots": a.screenshots == "on"}}).encode()),
                                    timeout=20).read()
                    except Exception as e:
                        _log(f"could not set the screenshot switch: {type(e).__name__}: {str(e)[:80]}")
                # journal marker AFTER the task setup: initialize_task resets the
                # device clock (possibly backwards), so a marker taken earlier would
                # sit in the agent's future and count nothing
                j0 = journal_len(env.controller.session)

                goal = task.goal
                extra = ""
                if name.startswith(_IR_PREFIXES):
                    extra = ("When you have the answer, stop and reply with ONLY the "
                             "answer (a number, name, or short value) — no explanation.")
                _log(f"start {name}: {task.goal[:110]}")
                raw_log = os.path.join(out_dir, "logs", f"{name}.jsonl")
                os.makedirs(os.path.dirname(raw_log), exist_ok=True)
                run = agent.run(goal, timeout=timeout, extra=extra, log_path=raw_log,
                                max_llm_steps=max_steps)
                # self-heal on provider-side errors: don't burn the task on a quota
                # window — sleep and retry the agent on the same task setup
                attempt = 0
                reply = run["reply"] or ""
                while reply.startswith("[agent error]") and attempt < 80:
                    provider_err = "provider." in reply[:40]
                    # account usage limit: the agent never ran — wait for the
                    # reset it names instead of burning the task (and every
                    # task after it) on 5 one-minute retries
                    if "out of credits" in reply:
                        # no reset time to wait for: stop the pass instead of
                        # recording every remaining task as a 0-token failure
                        _log(f"FATAL agent account: {reply[:120]!r} — stopping; "
                             f"rerun with the same --out to resume")
                        _save(results, results_path)
                        return 3
                    quota_s = _quota_wait(reply)
                    if quota_s is None and not provider_err and attempt >= 5:
                        break
                    attempt += 1
                    wait = quota_s if quota_s is not None else (600 if provider_err else 60)
                    _log(f"agent error ({reply[:90]!r}) — sleeping {wait}s, retry {attempt}")
                    time.sleep(wait)
                    run = agent.run(goal, timeout=timeout, extra=extra, log_path=raw_log,
                                    max_llm_steps=max_steps)
                    reply = run["reply"] or ""
                calls = journal_calls(env.controller.session, j0)
                _log(f"agent done {name}: {run['wall_s']}s llm_steps={run['llm_steps']} "
                     f"phone={calls} flag={run['flag']} reply={(run['reply'] or '')[:90]!r}")

                if name.startswith(_IR_PREFIXES):
                    env.interaction_cache = run["reply"]

                # OsmAnd keeps favorites in memory and flushes favorites.gpx
                # on app stop; rewards read the file, so background the app
                # first or the just-added waypoint is invisible to the check
                if name.startswith("OsmAnd"):
                    try:
                        b.shell("input", "keyevent", "3")
                        time.sleep(4)
                    except Exception:
                        pass

                try:
                    time.sleep(5)   # let async provider writes (sms sends) settle
                    if name.startswith("SystemBluetooth"):
                        # the adapter can take >10 s to finish a switch-off on a
                        # slow boot (the switch read "on, disabled" for 13 s);
                        # scoring while it is still TURNING_OFF read it as on
                        _settle_bluetooth(b)
                    score = task.is_successful(env)
                except Exception as e:
                    score = 0.0
                    entry["verify_error"] = f"{type(e).__name__}: {e}"
                entry.update(ok=bool(score >= 1.0), score=float(score),
                             reply=(run["reply"] or "")[:400], flag=run["flag"],
                             wall_s=run["wall_s"], tokens=run["tokens"],
                             cost=run["cost"], llm_steps=run["llm_steps"],
                             phone_calls=calls, max_steps=max_steps)
                if not entry["ok"]:
                    # debugging evidence: what the device showed after the agent
                    try:
                        txt = env.controller.session.observe()["text"]
                        entry["post_run_screen"] = "\n".join(txt.splitlines()[:60])
                    except Exception:
                        pass
                    try:
                        entry["expected"] = {
                            k: (str(v)[:200] if not hasattr(v, "save") else "<img>")
                            for k, v in (task.params or {}).items()}
                    except Exception:
                        pass
                results["tasks"][name] = entry
                try:
                    task.tear_down(env)
                except Exception:
                    pass
                _save(results, results_path)
                _log(f"{'PASS' if entry['ok'] else 'FAIL':4} [{idx + 1}/{len(todo)}] {name} "
                     f"score={entry.get('score')}")

                if a.retry_rounds > 1 and idx == len(todo) - 1:
                    still = [n for n in attempted if not results["tasks"].get(n, {}).get("ok")]
                    if len(still) == len(attempted) and attempted == attempted_prev:
                        _log("no progress in the last round — ending retry rounds")
                        todo = []
                        break
                    attempted_prev = attempted
                    todo = still

            except Exception as e:
                # bridge drop / wedge mid-task: revive the bridge (app
                # restart, then emulator reboot if needed), record a
                # retryable failure, keep the round going
                _log(f"runner error in {name}: {type(e).__name__}: "
                     f"{str(e)[:90]} - reviving bridge")
                try:
                    _revive_bridge(env, a)
                except Exception:
                    pass
                # the phone's bridge was down after a reboot (four tasks in a
                # row failed in seconds, pass H): no agent ever ran, so it is
                # not a failed attempt — queue the task once more at the end
                # of this pass instead of recording it
                if name not in infra_requeued and any(
                        s in str(e) for s in ("no accessibility content", "ADB_ERROR",
                                              "adb timeout", "Connection")):
                    infra_requeued.add(name)
                    todo.append(name)
                    _log(f"{name}: no agent attempt (bridge down) — queued to run again at the end")
                    continue
                results["tasks"][name] = {
                    "ok": False, "error": f"runner: {type(e).__name__}: {str(e)[:200]}"}
                _save(results, results_path)
    _finalize(results, out_dir)
    print(f"\n{results['passed']}/{results['total']} passed · "
          f"success rate {results['success_rate'] * 100:.1f}% → {out_dir}")
    return 0 if results["failed"] == 0 else 1


# task-name prefix -> the benchmark's own AppSetup class for apps whose task
# setups read the app's database
_DB_APPS = (("Notes", "JoplinApp"), ("SportsTracker", "OpenTracksApp"),
            ("Recipe", "RecipeApp"), ("Retro", "RetroMusicApp"), ("Expense", "ExpenseApp"),
            ("Vlc", "VlcApp"))


def _settle_bluetooth(b, timeout: float = 25.0) -> None:
    """Wait until the adapter is in a final state (ON / OFF), i.e. no
    TURNING_ON / TURNING_OFF / BLE_* transition is under way."""
    try:
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = b.shell("dumpsys", "bluetooth_manager")
            if "state: ON" in st or "state: OFF" in st:
                return
            time.sleep(1.0)
    except Exception:
        pass


def _sync_bluetooth(b, timeout: float = 15.0) -> bool:
    """Bring the Bluetooth adapter to the state the setting says (setup's
    intent); no-op when they already agree."""
    # The setup's `svc bluetooth enable` is asynchronous: for a few seconds
    # the manager says "enabled: true" (TURNING_ON) while the setting still
    # reads 0. Reading the setting at once and "correcting" the manager sent
    # `disable` and UNDID the setup's "on" — SystemBluetoothTurnOff started
    # with Bluetooth off and failed at the same spot on repeated runs.
    # Now: let the adapter settle, then fix only a disagreement that stays.
    try:
        t0 = time.time()
        kicked = False
        while time.time() - t0 < timeout:
            st = b.shell("dumpsys", "bluetooth_manager")
            want = b.shell("settings", "get", "global", "bluetooth_on").strip() == "1"
            on = "enabled: true" in st
            if on == want and "TURNING" not in st:
                return True
            if not kicked and "TURNING" not in st and time.time() - t0 > 4.0:
                b.shell("cmd", "bluetooth_manager", "enable" if want else "disable")
                kicked = True
            time.sleep(1.0)
        return False   # the adapter never reached the requested state
    except Exception:
        return True    # unknown: do not trigger a reboot on a probe error


def _quota_wait(reply: str):
    """Seconds until the usage-limit reset named in a codex error ("…try
    again at 9:08 AM"), +90 s slack; None if the reply is not a usage limit."""
    import datetime
    import re
    if "usage limit" not in reply:
        return None
    m = re.search(r"try again at (\d{1,2}):(\d{2})\s*([AP]M)?", reply)
    if not m:
        return 900
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        h = h % 12 + (12 if m.group(3) == "PM" else 0)
    now = datetime.datetime.now()
    at = now.replace(hour=h, minute=mi, second=0, microsecond=0)
    if at <= now:
        at += datetime.timedelta(days=1)
    return int((at - now).total_seconds()) + 90


def _save(results, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1, ensure_ascii=False)


def _finalize(results, out_dir):
    ran = results["tasks"]
    results["total"] = len(ran)
    results["passed"] = sum(1 for r in ran.values() if r.get("ok"))
    results["failed"] = sum(1 for r in ran.values() if not r.get("ok"))
    # official AndroidWorld metric: mean of per-task scores
    scores = [float(r.get("score", 1.0 if r.get("ok") else 0.0)) for r in ran.values()]
    results["success_rate"] = round(sum(scores) / max(1, len(scores)), 4)
    results["ok"] = results["failed"] == 0
    _save(results, os.path.join(out_dir, "results.json"))
    lines = ["# AndroidWorld × axdroid — run report", "",
             f"started {results['started']} · {results['passed']}/{results['total']} passed "
             f"· success rate {results['success_rate'] * 100:.1f}%", "",
             "| task | score | wall s | llm steps | phone calls | reply/note |",
             "|---|---|---|---|---|---|"]
    for name in sorted(ran):
        r = ran[name]
        note = (r.get("error") or r.get("verify_error") or (r.get("reply") or "")[:60]).replace("|", "/")
        lines.append(f"| {name} | {r.get('score', '')} | {r.get('wall_s', '')} "
                     f"| {r.get('llm_steps', '')} | {r.get('phone_calls', '')} | {note} |")
    with open(os.path.join(out_dir, "REPORT.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
