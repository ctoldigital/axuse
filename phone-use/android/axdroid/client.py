"""axdroid — transport to the phone: ADB-forward localhost or direct LAN.

Topologies (this is the part that is *not* an iOS port — the server runs on
the phone itself):

  adb    host ──▶ adb forward tcp:8360 tcp:8360 ──▶ phone 127.0.0.1:8360
         (USB debugging on; loopback needs no token — the USB link is the auth)

  lan    agent ──▶ http://<phone-ip>:8360  (owner enabled LAN in the app;
         every request carries the pairing token shown on the phone)

  sim    same HTTP protocol from axdroid.sim (tests, benchmarks, offline work)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request


class PhoneError(Exception):
    def __init__(self, code: str, message: str, **facts):
        super().__init__(message)
        self.code = code
        self.facts = facts

    def to_dict(self):
        return {"code": self.code, "message": str(self), **self.facts}


def find_adb() -> str | None:
    for env in ("AXDROID_ADB", "ANDROID_HOME", "ANDROID_SDK_ROOT"):
        p = os.environ.get(env)
        if p:
            cand = os.path.join(p, "platform-tools", "adb.exe" if os.name == "nt" else "adb")
            if os.path.exists(cand):
                return cand
    local = os.path.expanduser("~/AppData/Local/Android/Sdk/platform-tools/adb.exe")
    if os.path.exists(local):
        return local
    return shutil.which("adb")


class Adb:
    def __init__(self, adb: str, serial: str | None = None):
        self.adb = adb
        self.serial = serial

    def run(self, *args: str, timeout: float = 20) -> str:
        cmd = [self.adb] + (["-s", self.serial] if self.serial else []) + list(args)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise PhoneError("ADB_ERROR", (r.stderr or r.stdout or "adb failed").strip()[:300])
        return r.stdout.strip()

    @staticmethod
    def autodetect(serial: str | None = None) -> "Adb":
        adb = find_adb()
        if not adb:
            raise PhoneError("NO_ADB", "adb not found — set ANDROID_HOME or install platform-tools")
        a = Adb(adb, serial)
        if serial is None:
            devices = [d for d in a.devices() if d != "offline"]
            if not devices:
                raise PhoneError("NO_DEVICE", "no adb devices attached")
            a.serial = devices[0]
        return a

    def devices(self) -> list[str]:
        out = self.run("devices")
        serials = []
        for line in out.splitlines()[1:]:
            if "\tdevice" in line:
                serials.append(line.split("\t")[0])
        return serials

    def forward(self, port: int = 8360, device_port: int = 8360) -> None:
        self.run("forward", f"tcp:{port}", f"tcp:{device_port}")

    def unforward(self, port: int = 8360) -> None:
        try:
            self.run("forward", "--remove", f"tcp:{port}")
        except PhoneError:
            pass


class Phone:
    """JSON-RPC client for the on-device server (app/src/…/RpcServer.java)."""

    def __init__(self, base: str = "http://127.0.0.1:8360", token: str | None = None,
                 *, adb: Adb | None = None, forward: bool = True):
        self.base = base.rstrip("/")
        self.token = token or os.environ.get("AXDROID_TOKEN")
        self.adb = adb
        self.forwarded = False
        if adb is not None and forward:
            adb.forward()
            self.forwarded = True

    @classmethod
    def via_adb(cls, serial: str | None = None, port: int = 8360) -> "Phone":
        a = Adb.autodetect(serial)
        return cls(f"http://127.0.0.1:{port}", adb=a)

    @classmethod
    def via_lan(cls, host: str, token: str, port: int = 8360) -> "Phone":
        if not token:
            raise PhoneError("NO_TOKEN", "LAN transport needs the pairing token shown in the axdroid app")
        return cls(f"http://{host}:{port}", token=token)

    def healthz(self, timeout: float = 5) -> dict:
        return self._req("GET", "/healthz", timeout=timeout)

    def call(self, method: str, **params) -> dict:
        out = self._req("POST", "/rpc", body={"method": method, "params": params})
        if not out.get("ok"):
            e = out.get("error") or {}
            raise PhoneError(e.get("code", "ERROR"), e.get("message", str(e)),
                             **(e.get("facts") or {}))
        return out.get("data") or {}

    def _req(self, verb: str, path: str, body: dict | None = None, timeout: float = 40) -> dict:
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=verb)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                raise PhoneError("HTTP_" + str(e.code), f"{e} for {url}")
        except urllib.error.URLError as e:
            hint = " — is the axdroid app installed, its accessibility service enabled, and adb forward set?"
            raise PhoneError("BRIDGE_UNREACHABLE", str(e.reason) + hint, retry="later")
        except TimeoutError:
            raise PhoneError("BRIDGE_TIMEOUT", f"{url} timed out", retry="later")

    def close(self):
        if self.forwarded and self.adb is not None:
            self.adb.unforward()


class Latency:
    """Tiny p50/p9x tracker, like axphone's session latencies."""

    def __init__(self):
        self.samples: list[tuple[str, float]] = []

    def record(self, what: str, ms: float):
        self.samples.append((what, ms))

    def summary(self) -> dict:
        out = {}
        for what in {w for w, _ in self.samples}:
            xs = sorted(ms for w, ms in self.samples if w == what)
            out[what] = {"n": len(xs), "p50": round(xs[len(xs) // 2], 1),
                         "p90": round(xs[int(len(xs) * 0.9) - 1 if len(xs) > 1 else 0], 1)}
        return out
