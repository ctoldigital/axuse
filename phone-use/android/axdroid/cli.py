"""axdroid — CLI.

  axdroid pair          connect over adb (forward + status + service check)
  axdroid observe       print the current screen as the agent sees it
  axdroid serve         MCP server (--stdio default, or --listen host:port)
  axdroid gates         hardware/emulator gates → evidence report (JSON+MD)
  axdroid sim           run the simulated phone on a port (development)
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def main(argv=None):
    try:
        sys.stdout.reconfigure(errors="replace")   # cp1252 consoles must not crash on · → ×
    except Exception:
        pass
    if argv is None:
        argv = sys.argv[1:]
    # the relay is a full CLI of its own; bypass the subparser so its flags
    # (--http, --add-hub …) are not eaten by argparse
    if argv and argv[0] == "relay":
        from .relay import main as relay_main
        return relay_main(argv[1:])
    ap = argparse.ArgumentParser(prog="axdroid", description="axuse phone-use for Android")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pair", help="connect over adb and verify the bridge")
    p.add_argument("--serial", default=None)
    p.add_argument("--token", default=None, help="pairing token (LAN mode only)")

    o = sub.add_parser("observe", help="observe the current screen")
    _transport(o)
    o.add_argument("--mode", default="full", choices=["full", "lite"])
    o.add_argument("--find", default=None, help="find text instead of printing the tree")

    s = sub.add_parser("serve", help="MCP server (stdio by default)")
    _transport(s)
    s.add_argument("--stdio", action="store_true", default=True)
    s.add_argument("--listen", default=None, metavar="HOST:PORT", help="Streamable HTTP instead of stdio")
    s.add_argument("--relay", default=None, metavar="HOST:PORT",
                   help="dial OUT to the relay and serve the phone through it (nothing opened locally)")
    s.add_argument("--relay-tls", action="store_true", default=bool(os.environ.get("AXDROID_RELAY_TLS")),
                   help="dial the relay over TLS (default: plaintext for local relays)")
    s.add_argument("--relay-tls-ca", default=os.environ.get("AXDROID_RELAY_TLS_CA"), metavar="FILE")
    s.add_argument("--relay-tls-insecure", action="store_true", help="testing only: skip certificate checks")
    s.add_argument("--hub-id", default=None, help="phone id on the relay (default AXDROID_HUB_ID)")
    s.add_argument("--secret", default=None, help="relay secret (default AXDROID_HUB_SECRET)")

    lk = sub.add_parser("link", help="mint a one-time agent link code through the relay")
    lk.add_argument("--relay-tls-insecure", action="store_true", help="testing only: skip TLS checks")
    r = sub.add_parser("relay", help="run the cloud relay + authorization server (own CLI)")

    st = sub.add_parser("status", help="device + bridge status")
    _transport(st)

    g = sub.add_parser("gates", help="run device gates, write evidence")
    _transport(g)
    g.add_argument("--out", default="gates.json")
    g.add_argument("--apps", type=int, default=8, help="app-sweep count (G9)")

    tsk = sub.add_parser("tasks", help="run the multi-step task benchmark")
    _transport(tsk)
    tsk.add_argument("--out", default="tasks-report.json")
    tsk.add_argument("--only", default=None, help="comma-separated task names")

    sub.add_parser("sim", help="run the simulated phone").add_argument("--port", type=int, default=8361)

    sub.add_parser("test", help="run the offline suite against the simulator")

    a = ap.parse_args(argv)
    if a.cmd == "pair":
        from .client import Phone
        ph = Phone.via_adb(a.serial)
        d = ph.call("status")
        print(f"phone: {d['device']['model']} · {d['device']['os']} · {d['device']['display']['w']}×{d['device']['display']['h']}px")
        print(f"foreground: {d.get('foreground')}")
        print(f"bridge {d['version']} · changes {d['service']['changes']} · custody {d['custody']}")
        print("pair OK — transport: adb forward → 127.0.0.1:8360 (loopback, no token needed)")
    elif a.cmd == "observe":
        from .engine import Session
        ses = _session(a)
        if a.find:
            from .model import find
            snap = ses.current()
            for n in find(snap, text=a.find):
                print(f"{n.ref or '-'} {n.role} \"{(n.name or '')[:60]}\" bounds={n.bounds}")
        else:
            print(ses.observe(mode=a.mode)["text"])
    elif a.cmd == "status":
        from .engine import Session
        print(json.dumps(_session(a).status(), indent=1, ensure_ascii=False))
    elif a.cmd == "serve":
        _serve(a)
    elif a.cmd == "link":
        _link(a)
    elif a.cmd == "gates":
        from .gates import run_gates
        rep = run_gates(_phone(a), sweep=a.apps)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1, ensure_ascii=False)
        print(f"gates: {rep['summary']}  -> {a.out}")
        sys.exit(0 if rep["ok"] else 1)
    elif a.cmd == "tasks":
        from .tasks import run_tasks
        only = a.only.split(",") if a.only else None
        rep = run_tasks(_phone(a), only=only)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1, ensure_ascii=False)
        print(f"tasks: {rep['summary']}  -> {a.out}")
        sys.exit(0 if rep["ok"] else 1)
    elif a.cmd == "sim":
        from .sim import serve_sim
        _, _, shutdown = serve_sim(port=a.port)
        print(f"axdroid sim on 127.0.0.1:{a.port} (Ctrl-C to stop)")
        try:
            import threading
            threading.Event().wait()
        except KeyboardInterrupt:
            shutdown()
    elif a.cmd == "test":
        import unittest
        suite = unittest.defaultTestLoader.discover("tests" if __package__ is None else "phone-use/android/tests")
        unittest.TextTestRunner(verbosity=2).run(suite)
    return 0


def _transport(p):
    p.add_argument("--serial", default=None, help="adb serial")
    p.add_argument("--adb", action="store_true", help="force adb transport (default when --url absent)")
    p.add_argument("--url", default=None, help="direct http://ip:port (LAN; needs --token)")
    p.add_argument("--token", default=None)


def _phone(a):
    from .client import Phone
    url = getattr(a, "url", None)
    if url:
        host = url.split("//", 1)[-1].split("/", 1)[0]
        if ":" in host:                       # the URL already carries its port
            return Phone(url if "://" in url else "http://" + host,
                         token=a.token or os.environ.get("AXDROID_TOKEN"))
        return Phone.via_lan(host, a.token or "")
    return Phone.via_adb(getattr(a, "serial", None))


def _session(a):
    from .engine import Session
    return Session(_phone(a))


def _serve(a):
    import asyncio
    import os
    from .engine import Session
    from .mcp import McpCore, serve_http, serve_stdio
    ses = Session(_phone(a))
    core = McpCore(ses)
    if a.relay:
        from .tunnel import Tunnel, parse_addr, ssl_context
        hub_id = a.hub_id or os.environ.get("AXDROID_HUB_ID")
        secret = a.secret or os.environ.get("AXDROID_HUB_SECRET")
        if not (hub_id and secret):
            raise SystemExit("serve --relay needs --hub-id/--secret or AXDROID_HUB_ID/AXDROID_HUB_SECRET")
        addr = parse_addr(a.relay)
        ssl_ctx = None
        if a.relay_tls or os.environ.get("AXDROID_RELAY_TLS"):
            ssl_ctx = ssl_context(ca=a.relay_tls_ca or os.environ.get("AXDROID_RELAY_TLS_CA"),
                                 insecure=a.relay_tls_insecure)
        tunnel = Tunnel(addr, hub_id, secret, core=core, ssl_ctx=ssl_ctx)
        print(f"axdroid: tunnel to relay {addr[0]}:{addr[1]} as {hub_id}", file=sys.stderr)
        asyncio.run(tunnel.run())
    elif a.listen:
        host, _, port = a.listen.partition(":")
        print(f"axdroid MCP on http://{host}:{port}/mcp", file=sys.stderr)
        asyncio.run(serve_http(core, host, int(port or 8362), token=a.token))
    else:
        asyncio.run(serve_stdio(core))


def _link(a=None):
    import asyncio
    import os
    from .tunnel import fetch_link_code, parse_addr, ssl_context
    relay = os.environ.get("AXDROID_RELAY")
    hub = os.environ.get("AXDROID_HUB_ID")
    secret = os.environ.get("AXDROID_HUB_SECRET")
    if not (relay and hub and secret):
        raise SystemExit("link needs AXDROID_RELAY, AXDROID_HUB_ID, AXDROID_HUB_SECRET")
    insecure = getattr(a, "relay_tls_insecure", False) or os.environ.get("AXDROID_RELAY_TLS_INSECURE") == "1"
    ssl_ctx = ssl_context(insecure=insecure) if (os.environ.get("AXDROID_RELAY_TLS")
                                                 or getattr(a, "relay_tls_insecure", False)) else None
    info = asyncio.run(fetch_link_code(parse_addr(relay), hub, secret, ssl_ctx=ssl_ctx))
    print(f"link code: {info['code']}  (valid {info.get('expires_in', 300)} s, one use, "
          f"bound to this phone)")


if __name__ == "__main__":
    sys.exit(main())
