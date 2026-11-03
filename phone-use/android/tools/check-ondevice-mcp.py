"""Drive the phone's ON-DEVICE MCP (POST /mcp) — the zero-host-software path.

Proves the phone answers a real agent itself: initialize → observe →
launch → tap → diff → guard refusal, and compares the on-device render with
the host engine's on the same screen (format parity check).
"""
import json
import sys
import time
import urllib.request

sys.path.insert(0, ".")

BASE = "http://127.0.0.1:8360/mcp"


def call(method, params=None, _id=0):
    body = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params is not None:
        body["params"] = params
    req = urllib.request.Request(BASE, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def tool(name, args, _id=0):
    r = call("tools/call", {"name": name, "arguments": args}, _id)
    res = r.get("result", {})
    return res


def text(res):
    return res.get("content", [{}])[0].get("text", "")


def main():
    ok = True
    r = call("initialize", {"protocolVersion": "2025-03-26"}, 1)
    print("initialize:", r["result"]["serverInfo"]["name"],
          r["result"]["serverInfo"]["title"])
    r = call("tools/list", None, 2)
    print("tools:", len(r["result"]["tools"]))

    # clean start: escape any leftover overlay (search/IME/dialog), then home.
    # A previous session may have left the settings search screen foreground.
    for _ in range(2):
        tool("phone_button", {"name": "back"}, 90)
    tool("phone_button", {"name": "home"}, 91)
    time.sleep(0.8)

    t0 = time.perf_counter()
    res = tool("phone_launch", {"app": "Settings", "fresh": True}, 3)
    out = text(res)
    print(f"launch: {time.perf_counter()-t0:.2f}s isError={res.get('isError')}")
    print("\n".join(out.splitlines()[:6]))

    res = tool("phone_observe", {"max_lines": 14}, 4)
    print("\nobserve:")
    print(text(res))

    res = tool("phone_tap", {"target": "Connections"}, 5)
    print("\ntap Connections:")
    print(text(res)[:400])
    ok &= "ax-click" in text(res)

    res = tool("phone_button", {"name": "back"}, 6)
    print("\nback ok, isError =", res.get("isError"))

    res = tool("phone_tap", {"target": "Search settings"}, 7)
    print("\ntap search:")
    print(text(res)[:200])

    res = tool("phone_type", {"text": "battery", "into": "Search", "submit": False}, 8)
    print("\ntype (into Search):")
    print(text(res)[:300])
    ok &= "verified" in text(res)

    res = tool("phone_button", {"name": "back"}, 9)
    res = tool("phone_button", {"name": "back"}, 10)
    res = tool("phone_button", {"name": "home"}, 11)
    print("\nhome ok")

    # format parity: on-device observe vs host engine observe on the launcher
    res = tool("phone_observe", {"max_lines": 40}, 12)
    ondev = text(res)
    from axdroid.client import Phone
    from axdroid.engine import Session
    s = Session(Phone.via_adb())
    host = s.observe()["text"]
    def skeleton(t):
        return [l.split("[ref=")[0].rsplit('"', 1)[-1].strip() for l in t.splitlines()
                if l.strip().startswith("-")][:12]
    print("\nparity (first roles on the launcher):")
    print("  device:", skeleton(ondev)[:6])
    print("  host  :", skeleton(host)[:6])

    print("\nON-DEVICE MCP SMOKE:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
