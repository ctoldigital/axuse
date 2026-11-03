"""Relay end-to-end (offline): relay + tunnel(sim phone) + OAuth link-code flow
+ MCP through the relay, and scope enforcement through the whole chain.

Run:  python tests/test_relay_e2e.py
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


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.4):
                return True
        except OSError:
            time.sleep(0.2)
    return False


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


def oauth_access(http_port, hub_id, scope, link_code):
    """register → authorize (with the link code) → token → access token"""
    st, _, b = req(http_port, "POST", "/register",
                   {"redirect_uris": ["http://127.0.0.1:49152/cb"], "client_name": "relay-e2e"})
    assert st == 201, b
    cid = json.loads(b)["client_id"]
    verifier = "V" * 45
    chal = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = urllib.parse.urlencode({"response_type": "code", "client_id": cid,
                                "redirect_uri": "http://127.0.0.1:49152/cb",
                                "scope": scope, "state": "s1",
                                "code_challenge": chal, "code_challenge_method": "S256",
                                "resource": f"http://127.0.0.1:{http_port}/h/{hub_id}/mcp"})
    st, _, b = req(http_port, "GET", "/authorize?" + q)
    assert st == 200 and b"form_token" in b, (st, b[:200])
    ft = re.search(r'name="form_token" value="([^"]*)"', b.decode()).group(1)
    st, h, _ = req(http_port, "POST", "/authorize",
                   urllib.parse.urlencode({**dict(urllib.parse.parse_qsl(q, keep_blank_values=True)),
                                           "form_token": ft, "link_code": link_code,
                                           "decision": "allow"}).encode(),
                   ctype="application/x-www-form-urlencoded")
    assert st == 302, h
    back = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(h["Location"]).query))
    assert "code" in back and back["state"] == "s1", back
    st, _, b = req(http_port, "POST", "/token",
                   urllib.parse.urlencode({"grant_type": "authorization_code",
                                           "code": back["code"], "client_id": cid,
                                           "redirect_uri": "http://127.0.0.1:49152/cb",
                                           "code_verifier": verifier}).encode(),
                   ctype="application/x-www-form-urlencoded")
    assert st == 200, b
    return json.loads(b)["access_token"]


def main():
    # ── the simulated phone, speaking the app's wire protocol ──────────
    from axdroid.sim import serve_sim
    _, sim_client, sim_shutdown = serve_sim()
    sim_port = sim_client.base.rsplit(":", 1)[1]

    http_port, tun_port = free_port(), free_port()
    tmp = tempfile.mkdtemp()
    state = os.path.join(tmp, "state.json")
    env = {**os.environ, "PYTHONPATH": ROOT, "PYTHONIOENCODING": "utf-8"}

    relay = hub = None
    try:
        relay = subprocess.Popen([PY, "-m", "axdroid", "relay", "--http", f"127.0.0.1:{http_port}",
                                  "--tunnel", f"127.0.0.1:{tun_port}", "--state", state,
                                  "--add-hub", "simphone:simsecret"], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        assert wait_port(http_port) and wait_port(tun_port), "relay did not come up"
        print("relay up:", req(http_port, "GET", "/healthz")[2].decode())

        hub_env = {**env, "AXDROID_RELAY": f"127.0.0.1:{tun_port}",
                   "AXDROID_HUB_ID": "simphone", "AXDROID_HUB_SECRET": "simsecret"}
        hub = subprocess.Popen([PY, "-m", "axdroid", "serve",
                                "--url", f"http://127.0.0.1:{sim_port}", "--token", "sim",
                                "--relay", f"127.0.0.1:{tun_port}"],
                               env=hub_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        time.sleep(2.0)
        assert hub.poll() is None, "hub serve died:\n" + hub.stdout.read()

        link = subprocess.run([PY, "-m", "axdroid", "link"], env=hub_env,
                              capture_output=True, text=True, timeout=20)
        assert link.returncode == 0, link.stderr
        code = link.stdout.split("link code:")[1].split()[0]
        print("link code:", code)

        access = oauth_access(http_port, "simphone", "phone:read phone:act", code)
        ro_access = None  # a second link for the read-only approval
        link2 = subprocess.run([PY, "-m", "axdroid", "link"], env=hub_env,
                              capture_output=True, text=True, timeout=20)
        code2 = link2.stdout.split("link code:")[1].split()[0]
        ro_access = oauth_access(http_port, "simphone", "phone:read", code2)

        # ── MCP through the relay, full scopes ──────────────────────────
        st, _, b = req(http_port, "POST", "/h/simphone/mcp",
                       {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "phone_observe", "arguments": {}}},
                       auth=access)
        assert st == 200, (st, b[:300])
        text = json.loads(b)["result"]["content"][0]["text"]
        assert "com.android.settings" in text, text[:200]
        print("observe via relay:", text.splitlines()[0])

        st, _, b = req(http_port, "POST", "/h/simphone/mcp",
                       {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "phone_tap", "arguments": {"target": "Battery"}}},
                       auth=access)
        assert st == 200 and b"ax-click" in b, (st, b[:300])
        print("tap via relay: ax-click ok")

        # ── scope enforcement through the whole chain ──────────────────
        st, _, b = req(http_port, "POST", "/h/simphone/mcp",
                       {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, auth=ro_access)
        names = {t["name"] for t in json.loads(b)["result"]["tools"]}
        assert "phone_observe" in names and "phone_tap" not in names, names
        print("read-only token sees", len(names), "tools")

        st, _, b = req(http_port, "POST", "/h/simphone/mcp",
                       {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "phone_tap", "arguments": {"target": "Battery"}}},
                       auth=ro_access)
        assert b"INSUFFICIENT_SCOPE" in b, (st, b[:300])
        print("read-only tap refused: INSUFFICIENT_SCOPE")

        # ── guard holds through the relay too ─────────────────────────
        st, _, b = req(http_port, "POST", "/h/simphone/mcp",
                       {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "phone_button", "arguments": {"name": "back"}}},
                       auth=access)
        st, _, b = req(http_port, "POST", "/h/simphone/mcp",
                       {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "phone_scroll",
                                   "arguments": {"direction": "down", "until": "Reset"}}},
                       auth=access)
        st, _, b = req(http_port, "POST", "/h/simphone/mcp",
                       {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                        "params": {"name": "phone_tap", "arguments": {"target": "Reset"}}},
                       auth=access)
        assert b"NEEDS_CONFIRMATION" in b, (st, b[:300])
        print("guard through relay: NEEDS_CONFIRMATION")

        print("RELAY E2E OK")
        return 0
    finally:
        for p in (hub, relay):
            if p and p.poll() is None:
                p.kill()
        sim_shutdown()


if __name__ == "__main__":
    sys.exit(main())
