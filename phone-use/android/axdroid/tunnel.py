"""axdroid tunnel — the hub side of the relay connection (HANDOFF task 1).

The hub dials OUT to the relay over TLS (nothing is opened on the home
network), proves its identity with a challenge/response HMAC keyed by
sha256(AXDROID_HUB_SECRET) — the plaintext secret never leaves the hub — and
then serves MCP requests forwarded by the relay, with the caller's scopes.

Wire protocol (frames: 4-byte big-endian length + UTF-8 JSON, ≤ 4 MiB):

    hub → relay  {"t":"hello","hub":id,"proto":1,"nonce":n,"serves":bool}
    relay → hub  {"t":"challenge","salt":s}          (or {"t":"error","code":…})
    hub → relay  {"t":"auth","mac":hmac_sha256(sha256(secret), hub|nonce|salt)}
    relay → hub  {"t":"welcome","hub":id,"base":…,"issuer":…}
    relay → hub  {"t":"req","id":n,"scopes":[…],"msg":{jsonrpc}}   → hub → relay {"t":"res","id":n,"reply":…}
    relay → hub  {"t":"ping","id":n}                 → hub → relay {"t":"pong","id":n}
    hub → relay  {"t":"link_create","id":q}          → relay → hub {"t":"link_ok","id":q,"code":…}
    hub → relay  {"t":"status","status":{…}}         (relay shows it at /h/<hub>/healthz)

`serves:false` marks a link-only connection (axdroid link): the relay will not
route MCP traffic to it. Reconnects back off 1 s → 30 s; the backoff resets
after a connection has stayed up for 30 s. AUTH_FAILED/UNKNOWN_HUB are
configuration errors and raise TunnelAuthError instead of retrying forever.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import ssl
import time

from .relay import FrameError, read_frame, send_frame

log = logging.getLogger("axdroid.tunnel")


class TunnelAuthError(Exception):
    """The relay rejected this hub's identity — fix the config, don't retry."""


def parse_addr(spec: str) -> tuple[str, int]:
    host, _, port = spec.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"expected HOST:PORT, got {spec!r}")
    return host.strip("[]"), int(port)


def _auth_key(secret: str) -> bytes:
    return hashlib.sha256(secret.encode()).digest()


def ssl_context(*, ca: str | None = None, insecure: bool = False) -> ssl.SSLContext | None:
    """TLS for the tunnel connection. None = plaintext (local testing)."""
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if ca:
        ctx = ssl.create_default_context(cafile=ca)
        return ctx
    return ssl.create_default_context()


def next_backoff(attempt: int, cap: float = 30.0) -> float:
    return min(cap, 1.0 * (2 ** max(0, attempt - 1)))


async def _handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                     hub_id: str, secret: str, *, serves: bool, timeout: float = 15.0) -> dict:
    nonce = secrets.token_hex(16)
    await send_frame(writer, {"t": "hello", "hub": hub_id, "proto": 1, "nonce": nonce, "serves": serves})
    f = await asyncio.wait_for(read_frame(reader), timeout)
    if f.get("t") == "error":
        raise TunnelAuthError(f.get("code") or "AUTH_FAILED")
    if f.get("t") != "challenge":
        raise FrameError(f"expected challenge, got {f.get('t')!r}")
    mac = hmac.new(_auth_key(secret), f"{hub_id}|{nonce}|{f['salt']}".encode(), hashlib.sha256).hexdigest()
    await send_frame(writer, {"t": "auth", "mac": mac})
    f = await asyncio.wait_for(read_frame(reader), timeout)
    if f.get("t") == "error":
        raise TunnelAuthError(f.get("code") or "AUTH_FAILED")
    if f.get("t") != "welcome":
        raise FrameError(f"expected welcome, got {f.get('t')!r}")
    return f


class Tunnel:
    """Keeps one authenticated connection to the relay and answers forwarded
    MCP requests through `core.handle(msg, scopes=…)` (scopes enforced again
    hub-side: the relay's token grants are never trusted alone)."""

    def __init__(self, addr: tuple[str, int], hub_id: str, secret: str, core=None, *,
                 ssl_ctx: ssl.SSLContext | None = None):
        self.addr, self.hub_id, self.secret, self.core = addr, hub_id, secret, core
        self.ssl_ctx = ssl_ctx if ssl_ctx != "plain" else None
        self.ready = asyncio.Event()
        self.stopping = False
        self.welcome: dict = {}
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future] = {}

    async def run(self):
        attempt = 0
        while not self.stopping:
            started = time.time()
            try:
                await self._once()
            except TunnelAuthError:
                self.ready.clear()
                raise  # config error: surface it, never hammer the relay
            except (OSError, EOFError, ConnectionError, FrameError, asyncio.TimeoutError,
                    json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
                # EOFError: a reset/closed connection mid-handshake
                # (IncompleteReadError) must retry with backoff, not crash serve.
                if self.stopping:
                    break
                log.info("relay connection ended (%s); reconnecting", e)
            if self.stopping:
                break
            attempt = 0 if time.time() - started >= 30 else attempt + 1
            await asyncio.sleep(next_backoff(attempt))
        self.ready.clear()

    async def _once(self):
        reader, writer = await asyncio.open_connection(self.addr[0], self.addr[1], ssl=self.ssl_ctx)
        self._writer = writer
        try:
            self.welcome = await _handshake(reader, writer, self.hub_id, self.secret,
                                            serves=self.core is not None)
        except BaseException:
            writer.close()
            self._writer = None
            raise
        self.ready.set()
        log.info("hub %s connected to relay (serves=%s)", self.hub_id, self.core is not None)
        try:
            while not self.stopping:
                f = await read_frame(reader)
                t = f.get("t")
                if t == "req":
                    reply = await self._handle(f)
                    if not self.stopping:
                        await send_frame(writer, {"t": "res", "id": f.get("id"), "reply": reply})
                elif t == "ping":
                    await send_frame(writer, {"t": "pong", "id": f.get("id")})
                elif t == "status_req":
                    await send_frame(writer, {"t": "status", "status": {}})
                elif isinstance(f.get("id"), str):  # reply to a hub-initiated request
                    fut = self._pending.pop(f["id"], None)
                    if fut and not fut.done():
                        fut.set_result(f)
                # unknown frames ignored (forward compatibility)
        finally:
            self.ready.clear()
            self._writer = None
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("relay connection lost"))
            self._pending.clear()
            writer.close()

    async def link_code(self, timeout: float = 15.0) -> dict:
        """Ask the relay for a one-time link code over the live connection
        (what the companion app and `axdroid link` show)."""
        if self._writer is None or not self.ready.is_set():
            raise ConnectionError("relay tunnel is not connected")
        qid = f"q-{secrets.token_hex(4)}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[qid] = fut
        await send_frame(self._writer, {"t": "link_create", "id": qid})
        try:
            f = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(qid, None)
            raise
        if f.get("t") == "error":
            raise TunnelAuthError(f.get("code") or "LINK_DENIED")
        return {"code": f["code"], "expires_in": f.get("expires_in", 300)}

    async def _handle(self, f: dict) -> dict | None:
        msg = f.get("msg") or {}
        scopes = frozenset(s for s in (f.get("scopes") or []) if s in ("phone:read", "phone:act"))
        actor = str(f.get("actor") or "relay")[:80]
        if self.core is None:
            return {"jsonrpc": "2.0", "id": msg.get("id"),
                    "error": {"code": -32601, "message": "this hub connection is link-only"}}
        try:
            return await self.core.handle(msg, scopes=scopes, actor=actor)
        except Exception as e:  # protocol layer never dies on a tool bug
            return {"jsonrpc": "2.0", "id": msg.get("id"),
                    "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}

    def stop(self):
        """Stop serving and close the current connection so the relay sees EOF."""
        self.stopping = True
        if self._writer is not None:
            self._writer.close()
        self.ready.set()  # wake waiters


async def fetch_link_code(addr, hub_id: str, secret: str, *, ssl_ctx=None, timeout: float = 15.0) -> dict:
    """One-shot connection: prove hub possession and get a one-time link code
    (what `axdroid link` prints and the companion app shows)."""
    reader, writer = await asyncio.open_connection(addr[0], addr[1], ssl=ssl_ctx)
    try:
        info = await _handshake(reader, writer, hub_id, secret, serves=False, timeout=timeout)
        qid = "link-" + secrets.token_hex(4)
        await send_frame(writer, {"t": "link_create", "id": qid})
        while True:
            f = await asyncio.wait_for(read_frame(reader), timeout)
            if f.get("t") == "error":
                raise TunnelAuthError(f.get("code") or "LINK_DENIED")
            if f.get("t") == "link_ok" and f.get("id") == qid:
                return {"code": f["code"], "expires_in": f.get("expires_in", 300),
                        "base": info.get("base"), "issuer": info.get("issuer")}
    finally:
        writer.close()


def env_config() -> tuple[tuple[str, int], str, str]:
    """(relay_addr, hub_id, secret) from the AXDROID_* environment."""
    relay = os.environ.get("AXDROID_RELAY")
    hub = os.environ.get("AXDROID_HUB_ID")
    secret = os.environ.get("AXDROID_HUB_SECRET")
    if not secret and os.environ.get("AXDROID_HUB_SECRET_FILE"):
        with open(os.environ["AXDROID_HUB_SECRET_FILE"], encoding="utf-8") as f:
            secret = f.read().strip()
    missing = [n for n, v in (("AXDROID_RELAY", relay), ("AXDROID_HUB_ID", hub), ("AXDROID_HUB_SECRET", secret)) if not v]
    if missing:
        raise SystemExit(f"missing env: {', '.join(missing)}")
    return parse_addr(relay), hub, secret
