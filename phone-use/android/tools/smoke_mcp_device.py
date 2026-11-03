"""MCP stdio smoke against the REAL phone: initialize → observe → launch → tap → diff."""
import json
import subprocess
import sys

msgs = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
     "params": {"name": "phone_launch", "arguments": {"app": "Settings", "fresh": True}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
     "params": {"name": "phone_find", "arguments": {"text": "Connections", "role": "text"}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
     "params": {"name": "phone_tap", "arguments": {"target": "Connections"}}},
    {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
     "params": {"name": "phone_button", "arguments": {"name": "home"}}},
]
p = subprocess.run([sys.executable, "-m", "axdroid", "serve", "--stdio"],
                   input="\n".join(json.dumps(m) for m in msgs),
                   capture_output=True, text=True, timeout=240, encoding="utf-8")
ok = True
for line in p.stdout.strip().splitlines():
    r = json.loads(line)
    rid = r.get("id")
    res = r.get("result", {})
    if rid == 1:
        print("initialize:", res.get("serverInfo", {}).get("name"), res.get("serverInfo", {}).get("version"))
    elif rid == 2:
        print("tools:", len(res.get("tools", [])))
    elif rid == 3:
        head = res["content"][0]["text"].splitlines()[0]
        print("launch:", head[:90])
    elif rid == 4:
        print("find:", res["content"][0]["text"].splitlines()[0][:90])
    elif rid == 5:
        body = res["content"][0]["text"]
        err = res.get("isError")
        print("tap:", ("ERROR " if err else "") + body.replace("\n", " | ")[:150])
        if err:
            ok = False
    elif rid == 6:
        print("button home:", "ok" if not res.get("isError") else res["content"][0]["text"][:120])
print("MCP SMOKE:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
