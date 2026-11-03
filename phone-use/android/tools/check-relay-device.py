"""Real-hardware relay e2e: the phone dials OUT to a relay (reached over adb
reverse, standing in for a cloud relay), then a full agent session goes
through it: link code minted BY THE PHONE → OAuth → MCP observe/tap of the
real device through the whole chain.

Run from phone-use/android:
  adb reverse tcp:18901 tcp:18901
  python tools/check-relay-device.py
"""
import base64
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PY = sys.executable
HTTP_PORT, TUN_PORT = 18900, 18901
HUB_ID = "s21fe"
SECRET = os.environ.get("AXDROID_TEST_RELAY_SECRET") or __import__("secrets").token_urlsafe(18)   # throwaway test secret


def wake_phone():
    """The display sleeps between runs; wake it (debug phone, no keyguard PIN
    assumed — a locked phone is an honest read of the tree, not an error)."""
    subprocess.run(["adb", "shell", "input", "keyevent", "224"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["adb", "shell", "wm", "dismiss-keyguard"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Settings restores its scroll position across non-fresh launches: start
    # from a known state (fresh launch shows the top of the list)
    subprocess.run(["adb", "shell", "am", "force-stop", "com.android.settings"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def req(port, method, path, body=None, ctype="application/json", auth=None):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        r.add_header("Content-Type", ctype)
    if auth:
        r.add_header("Authorization", f"Bearer {auth}")
    try:
        with _opener.open(r, data) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def phone_rpc(method, **params):
    body = {"method": method, "params": params}
    r = urllib.request.Request("http://127.0.0.1:8360/rpc", data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as resp:
        out = json.loads(resp.read().decode())
    if not out.get("ok"):
        raise RuntimeError(f"{method}: {out.get('error')}")
    return out["data"]


def main():
    wake_phone()
    # 1. the relay, on this machine (a cloud relay in production)
    state = os.path.join(tempfile.mkdtemp(), "relay-state.json")
    env = {**os.environ, "PYTHONPATH": ROOT, "PYTHONIOENCODING": "utf-8"}
    relay = subprocess.Popen([PY, "-m", "axdroid", "relay", "--http", f"127.0.0.1:{HTTP_PORT}",
                              "--tunnel", f"127.0.0.1:{TUN_PORT}", "--state", state,
                              "--add-hub", f"{HUB_ID}:{SECRET}"], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert relay.poll() is None, relay.stdout.read()
    time.sleep(1.5)
    print("relay up:", req(HTTP_PORT, "GET", "/healthz")[2].decode())

    # 2. the phone dials out: its localhost:18901 reaches the relay via adb reverse
    subprocess.run(["adb", "reverse", f"tcp:{TUN_PORT}", f"tcp:{TUN_PORT}"], check=False)
    cfg = phone_rpc("relay_set", host=f"127.0.0.1:{TUN_PORT}", id=HUB_ID, secret=SECRET,
                    enabled=True, tls=False)
    print("phone relay cfg:", cfg)
    assert cfg.get("connected"), "the phone's tunnel did not connect to the relay"

    st = req(HTTP_PORT, "GET", f"/h/{HUB_ID}/healthz")
    print("relay sees the phone:", st[2].decode()[:120])

    # 3. the phone mints a one-time link code through its tunnel
    link = phone_rpc("relay_link")
    code = link["code"]
    print("link code (from the phone):", code)

    # 4. OAuth: register → authorize with the code → token
    st, _, b = req(HTTP_PORT, "POST", "/register",
                   {"redirect_uris": ["http://127.0.0.1:49152/cb"], "client_name": "device-e2e"})
    cid = json.loads(b)["client_id"]
    verifier = "D" * 45
    chal = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = urllib.parse.urlencode({"response_type": "code", "client_id": cid,
                                "redirect_uri": "http://127.0.0.1:49152/cb",
                                "scope": "phone:read phone:act", "state": "s1",
                                "code_challenge": chal, "code_challenge_method": "S256",
                                "resource": f"http://127.0.0.1:{HTTP_PORT}/h/{HUB_ID}/mcp"})
    st, _, b = req(HTTP_PORT, "GET", "/authorize?" + q)
    ft = re.search(r'name="form_token" value="([^"]*)"', b.decode()).group(1)
    st, h, _ = req(HTTP_PORT, "POST", "/authorize",
                   urllib.parse.urlencode({**dict(urllib.parse.parse_qsl(q, keep_blank_values=True)),
                                           "form_token": ft, "link_code": code,
                                           "decision": "allow"}).encode(),
                   ctype="application/x-www-form-urlencoded")
    assert st == 302, h
    back = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(h["Location"]).query))
    st, _, b = req(HTTP_PORT, "POST", "/token",
                   urllib.parse.urlencode({"grant_type": "authorization_code",
                                           "code": back["code"], "client_id": cid,
                                           "redirect_uri": "http://127.0.0.1:49152/cb",
                                           "code_verifier": verifier}).encode(),
                   ctype="application/x-www-form-urlencoded")
    access = json.loads(b)["access_token"]
    print("token: audience-bound to", f"/h/{HUB_ID}/mcp")

    def mcp(_id, name, args):
        st, _, b = req(HTTP_PORT, "POST", f"/h/{HUB_ID}/mcp",
                       {"jsonrpc": "2.0", "id": _id, "method": "tools/call",
                        "params": {"name": name, "arguments": args}}, auth=access)
        out = json.loads(b)
        res = out.get("result", {})
        return res.get("content", [{}])[0].get("text", ""), res

    # clean start: escape any leftover overlay (search/IME/dialog), then home
    for i in range(2):
        mcp(90 + i, "phone_button", {"name": "back"})
    mcp(92, "phone_button", {"name": "home"})
    time.sleep(1.0)

    # 5. MCP through the relay — the REAL phone answers
    st, _, b = req(HTTP_PORT, "POST", f"/h/{HUB_ID}/mcp",
                   {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, auth=access)
    tools = {t["name"] for t in json.loads(b)["result"]["tools"]}
    print("tools via relay:", len(tools))

    text, _ = mcp(2, "phone_launch", {"app": "Settings"})
    print("launch via relay:", text.splitlines()[0][:80])

    text, _ = mcp(3, "phone_observe", {"max_lines": 8})
    print("observe via relay:")
    print("\n".join(text.splitlines()[:8]))

    text, res = mcp(4, "phone_tap", {"target": "Connections"})
    assert "ax-click" in text, text[:300]
    print("tap via relay: ok")

    text, res = mcp(5, "phone_button", {"name": "home"})
    print("home via relay: ok")

    phone_rpc("relay_set", enabled=False)
    relay.kill()
    print("\nDEVICE RELAY E2E: PASS")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["adb", "reverse", "--remove", f"tcp:{TUN_PORT}"], check=False)
