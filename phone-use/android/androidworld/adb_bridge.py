"""Local adb bridge: AndroidEnvInterface.execute_adb_call over a real adb
binary, targeting one emulator serial.

AndroidWorld funnels *every* device interaction (task setup, rewards, app
installs, actuation) through `env.execute_adb_call(AdbRequest) -> AdbResponse`
protos; upstream that lands in android_env's emulator coordinator. Here it
lands in a plain `adb -s <serial> …` subprocess — same wire semantics, no
emulator gRPC stack needed, so the axdroid app can be the a11y provider.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time

from android_env.components import errors
from android_env.proto import adb_pb2

_OK = adb_pb2.AdbResponse.Status.OK
_ERROR = adb_pb2.AdbResponse.Status.ADB_ERROR

# one adb channel per device: concurrent agent tool calls (codex fires
# several MCP calls per turn) saturate adbd and wedge the emulator —
# serialize every adb subprocess per serial
_LOCKS: dict[str, "threading.Lock"] = {}


def _lock_for(serial: str) -> "threading.Lock":
    import threading
    if serial not in _LOCKS:
        _LOCKS[serial] = threading.Lock()
    return _LOCKS[serial]
# proto enum value -> settings namespace string
_NS = {
    adb_pb2.AdbRequest.SettingsRequest.Namespace.GLOBAL: "global",
    adb_pb2.AdbRequest.SettingsRequest.Namespace.SYSTEM: "system",
    adb_pb2.AdbRequest.SettingsRequest.Namespace.SECURE: "secure",
    adb_pb2.AdbRequest.SettingsRequest.Namespace.UNKNOWN: "unknown",
}
_KEYCODE = {  # AdbRequest.PressButton.Button -> keycode
    adb_pb2.AdbRequest.PressButton.HOME: 3,
    adb_pb2.AdbRequest.PressButton.BACK: 4,
    adb_pb2.AdbRequest.PressButton.ENTER: 66,
}

_ACTIVITY_RE = re.compile(r"ACTIVITY\s+(\S+)\s")


def _resp(**kw) -> adb_pb2.AdbResponse:
    return adb_pb2.AdbResponse(status=_OK, **kw)


def _err(msg: str) -> adb_pb2.AdbResponse:
    return adb_pb2.AdbResponse(status=_ERROR, error_message=msg)


class LocalAdbEnv:
    """Duck-typed AndroidEnvInterface backed by subprocess adb."""

    def __init__(self, adb_path: str, serial: str, connect_timeout: float = 120):
        self.adb_path = adb_path
        self.serial = serial
        self.connect_timeout = connect_timeout
        self._root_attempted = False

    # ── process plumbing ──────────────────────────────────────────────

    def _adb(self, *args: str, timeout: float | None = None, binary: bool = False,
             check: bool = False) -> subprocess.CompletedProcess:
        cmd = [self.adb_path, "-s", self.serial, *args]
        with _lock_for(self.serial):
            r = subprocess.run(
                cmd, capture_output=True,
                timeout=timeout if timeout is not None else 60,
            )
        if check and r.returncode != 0:
            raise errors.AdbControllerError(
                (r.stderr or r.stdout).decode("utf-8", "replace").strip()[:500])
        return r

    def shell(self, *args: str, timeout: float | None = None) -> str:
        """Run `adb shell <args>`; returns stdout (exit code ignored, matching
        android_env's generic-request semantics where callers parse text)."""
        r = self._adb("shell", *args, timeout=timeout)
        return (r.stdout or b"").decode("utf-8", "replace")

    def screenshot_png(self) -> bytes:
        last = None
        for attempt in range(3):
            try:
                r = self._adb("exec-out", "screencap", "-p", timeout=90)
                if r.returncode != 0:
                    raise errors.AdbControllerError("screencap failed")
                return r.stdout
            except (subprocess.TimeoutExpired, errors.AdbControllerError) as e:
                last = e
                time.sleep(2)
        raise errors.AdbControllerError(f"screencap failed after retries: {last}")

    def ensure_root(self) -> None:
        r = self._adb("shell", "id")
        if b"uid=0" in (r.stdout or b""):
            return
        self._adb("root", timeout=30)
        # adbd restarts; wait for it to come back
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                r = self._adb("shell", "id", timeout=10)
                if b"uid=0" in (r.stdout or b""):
                    return
            except (subprocess.TimeoutExpired, errors.AdbControllerError):
                pass
            time.sleep(1)
        raise errors.AdbControllerError("adb root did not take effect")

    # ── the AndroidEnvInterface entry point ───────────────────────────

    def execute_adb_call(self, request: adb_pb2.AdbRequest) -> adb_pb2.AdbResponse:
        timeout = request.timeout_sec if request.timeout_sec > 0 else 120.0
        kind = request.WhichOneof("command")
        try:
            try:
                return self._dispatch(request, kind, timeout)
            except subprocess.TimeoutExpired:
                # upstream treats timeout_sec as a soft deadline; pm/am can
                # occasionally overrun on a busy device — one retry, then raise
                time.sleep(1.0)
                return self._dispatch(request, kind, timeout + 30.0)
        except subprocess.TimeoutExpired as e:
            raise errors.AdbControllerError(f"adb timeout ({kind}): {e}") from e
        except errors.AdbControllerError:
            raise
        except Exception as e:  # pragma: no cover - defensive
            return _err(f"{type(e).__name__}: {e}")

    def _dispatch(self, request, kind, timeout) -> adb_pb2.AdbResponse:
        if kind == "generic":
            return self._generic(list(request.generic.args), timeout)
        if kind == "start_activity":
            args = ["shell", "am", "start", *request.start_activity.extra_args,
                    "-n", request.start_activity.full_activity]
            r = self._adb(*args, timeout=timeout)
            out = (r.stdout or b"") + (r.stderr or b"")
            if r.returncode != 0:
                return _err(out.decode("utf-8", "replace"))
            return _resp(generic=adb_pb2.AdbResponse.GenericResponse(output=(r.stdout or b"")))
        if kind == "tap":
            self.shell("input", "tap", str(request.tap.x), str(request.tap.y), timeout=timeout)
            return _resp()
        if kind == "press_button":
            kc = _KEYCODE[request.press_button.button]
            self.shell("input", "keyevent", str(kc), timeout=timeout)
            return _resp()
        if kind == "input_text":
            from android_world.env import adb_utils
            text = adb_utils._adb_text_format(request.input_text.text)  # pylint: disable=protected-access
            self.shell("input", "text", text, timeout=timeout)
            return _resp()
        if kind == "get_current_activity":
            # `dumpsys activity top` lists STOPPED back-stack activities too —
            # the first ACTIVITY line is frequently not the resumed one (this
            # silently failed every foreground-activity reward). Parse the
            # resumed activity instead, with the old heuristic as fallback.
            out = self.shell("dumpsys", "activity", "activities", timeout=timeout)
            m = re.search(r"(?:topResumedActivity|ResumedActivity)=ActivityRecord\{[^}]*\s(\S+/\S+?)[\s}]", out)
            if m:
                return _resp(get_current_activity=adb_pb2.AdbResponse.GetCurrentActivityResponse(
                    full_activity=m.group(1)))
            out2 = self.shell("dumpsys", "activity", "top", timeout=timeout)
            for block in out2.split("ACTIVITY ")[1:]:
                head = block.splitlines()[0].strip()
                pkg_act = head.split()[0] if head else ""
                if "mResumed=true" in block[:3000]:
                    return _resp(get_current_activity=adb_pb2.AdbResponse.GetCurrentActivityResponse(
                        full_activity=pkg_act))
            m2 = _ACTIVITY_RE.search(out2)
            if not m2:
                return _resp(get_current_activity=adb_pb2.AdbResponse.GetCurrentActivityResponse(full_activity=""))
            return _resp(get_current_activity=adb_pb2.AdbResponse.GetCurrentActivityResponse(
                full_activity=m2.group(1)))
        if kind == "get_orientation":
            out = self.shell("dumpsys", "window", "policy", timeout=timeout)
            m = re.search(r"orientation=(\d)", out) or re.search(r"mLastOrientation=(\d)", out)
            val = int(m.group(1)) if m else 0
            return _resp(get_orientation=adb_pb2.AdbResponse.GetOrientationResponse(orientation=val))
        if kind == "settings":
            return self._settings(request.settings, timeout)
        if kind == "package_manager":
            return self._package_manager(request.package_manager, timeout)
        if kind == "install_apk":
            return self._install(request.install_apk, timeout)
        if kind == "push":
            return self._push(request.push, timeout)
        if kind == "pull":
            return self._pull(request.pull, timeout)
        if kind == "force_stop":
            out = self.shell("am", "force-stop", request.force_stop.package, timeout=timeout)
            return _resp(generic=adb_pb2.AdbResponse.GenericResponse(output=out.encode()))
        if kind == "uninstall_package":
            r = self._adb("uninstall", request.uninstall_package.package, timeout=timeout)
            out = (r.stdout or b"") + (r.stderr or b"")
            return _resp(generic=adb_pb2.AdbResponse.GenericResponse(output=out))
        if kind == "send_broadcast":
            args = ["shell", "am", "broadcast", "-a", request.send_broadcast.action]
            r = self._adb(*args, timeout=timeout)
            return _resp(generic=adb_pb2.AdbResponse.GenericResponse(output=(r.stdout or b"")))
        if kind == "dumpsys":
            args = ["shell", "dumpsys", *request.dumpsys.args]
            out = self.shell(*args[2:], timeout=timeout)
            return _resp(dumpsys=adb_pb2.AdbResponse.DumpsysResponse(output=out.encode()))
        return _err(f"unsupported AdbRequest type: {kind}")

    def _generic(self, args: list[str], timeout) -> adb_pb2.AdbResponse:
        if not args:
            return _err("empty generic request")
        head = args[0]
        if head == "root":
            self.ensure_root()
            return _resp(generic=adb_pb2.AdbResponse.GenericResponse(output=b"root"))
        if head in ("wait-for-device", "wait-for-usb", "remount"):
            self._adb(*args, timeout=timeout)
            return _resp()
        # 'shell …' / 'emu …' / anything else: pass straight to adb
        r = self._adb(*args, timeout=timeout)
        out = (r.stdout or b"") + ((r.stderr or b"") if head == "emu" else b"")
        return _resp(generic=adb_pb2.AdbResponse.GenericResponse(output=out))

    def _settings(self, s, timeout) -> adb_pb2.AdbResponse:
        verb = s.WhichOneof("verb")
        ns = _NS.get(s.namespace, "global")
        if verb == "put":
            self.shell("settings", "put", ns, s.put.key, s.put.value, timeout=timeout)
            return _resp(settings=adb_pb2.AdbResponse.SettingsResponse())
        if verb == "get":
            out = self.shell("settings", "get", ns, s.get.key, timeout=timeout)
            return _resp(settings=adb_pb2.AdbResponse.SettingsResponse(output=out.encode()))
        return _err(f"unsupported settings verb {verb}")

    def _package_manager(self, pm, timeout) -> adb_pb2.AdbResponse:
        verb = pm.WhichOneof("verb")
        if verb != "list":
            return _err(f"unsupported package_manager verb {verb}")
        what = pm.list.WhichOneof("what")
        if what == "packages":
            out = self.shell("pm", "list", "packages", *pm.list.packages.options, timeout=timeout)
            items = [ln.replace("package:", "").strip()
                     for ln in out.splitlines() if ln.strip()]
            return _resp(package_manager=adb_pb2.AdbResponse.PackageManagerResponse(
                list=adb_pb2.AdbResponse.PackageManagerResponse.List(items=items)))
        return _err(f"unsupported package_manager list {what}")

    def _install(self, inst, timeout) -> adb_pb2.AdbResponse:
        loc = inst.WhichOneof("location")
        if loc != "filesystem":
            return _err(f"unsupported install location {loc}")
        path = inst.filesystem.path
        if not os.path.exists(path):
            return _err(f"APK does not exist: {path}")
        r = self._adb("install", "-r", "-t", path, timeout=max(timeout, 120))
        if r.returncode != 0:
            return _err((r.stderr or r.stdout).decode("utf-8", "replace"))
        return _resp()

    def _push(self, push, timeout) -> adb_pb2.AdbResponse:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(push.content)
            local = f.name
        try:
            r = self._adb("push", local, push.path, timeout=timeout)
            if r.returncode != 0:
                return _err((r.stderr or r.stdout).decode("utf-8", "replace"))
            return _resp(push=adb_pb2.AdbResponse.PushResponse())
        finally:
            os.unlink(local)

    def _pull(self, pull, timeout) -> adb_pb2.AdbResponse:
        with tempfile.TemporaryDirectory() as td:
            r = self._adb("pull", pull.path, td, timeout=timeout)
            if r.returncode != 0:
                return _err((r.stderr or r.stdout).decode("utf-8", "replace"))
            # adb pull <path> <dir> keeps the basename
            base = os.path.basename(pull.path.rstrip("/"))
            with open(os.path.join(td, base), "rb") as f:
                data = f.read()
            return _resp(pull=adb_pb2.AdbResponse.PullResponse(content=data))

    # android_env parity no-ops
    def close(self):
        pass
