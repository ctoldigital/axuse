"""axdroid relay — cloud relay + authorization server (HANDOFF task 1).

One process, two listeners, pure stdlib:

    public HTTPS (behind Caddy/nginx in production)      hub tunnel port
  ┌───────────────────────────────────────────────┐    ┌─────────────────────┐
  │ OAuth 2.1 authorization server (RFC 8414)     │    │ hubs dial OUT over  │
  │   /authorize  consent bound to a *link code*  │    │ TLS and speak      │
  │   /token      PKCE S256, rotating refresh     │    │ length-prefixed    │
  │   /register   RFC 7591 DCR (+ CIMD clients)   │    │ JSON frames:       │
  │   /revoke     RFC 7009                        │    │ hello/challenge/   │
  │ resource proxy  POST /h/<hub>/mcp (RFC 9728,  │◄───┤ auth, req/res,     │
  │                 audience = exact URL, RFC8707)│    │ ping/pong, link_   │
  └───────────────────────────────────────────────┘    │ create, status     │
                                                       └─────────────────────┘

The link code is what ties a consent click to physical possession of the hub:
it can only be created *through* a hub's authenticated tunnel (`axdroid link`
or the companion app), lives 5 minutes, works once, and is bound to one hub.

Token model: access tokens are compact HS256 JWTs signed with a server key,
`aud` = the exact resource URL (`…/h/<hub>/mcp`, RFC 8707), `scope` carries
phone:read / phone:act, `sub` = grant id so a revocation kills the family.
Refresh tokens are opaque, stored hashed, rotated on every use; reuse of a
used token revokes the whole family (RFC 6749 §10.5 / OAuth BCP §4.14.2).

State lives in one JSON file (atomic writes, 0600); link codes and
authorization codes are memory-only on purpose — a relay restart invalidates
in-flight approvals, never long-lived access.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import struct
import sys
import time
import urllib.parse

__all__ = ["Relay", "RelayStore", "serve_relay", "read_frame", "send_frame",
           "HubOffline", "HubTimeout", "FrameError"]

# ── wire framing (shared with axdroid/tunnel.py) ─────────────────────────────
MAX_FRAME = 4 << 20  # 4 MiB


class FrameError(ConnectionError):
    pass


async def read_frame(reader: asyncio.StreamReader) -> dict:
    raw = await reader.readexactly(4)
    (n,) = struct.unpack(">I", raw)
    if not 0 < n <= MAX_FRAME:
        raise FrameError(f"bad frame length {n}")
    return json.loads((await reader.readexactly(n)).decode("utf-8"))


async def send_frame(writer: asyncio.StreamWriter, obj: dict) -> None:
    data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    writer.write(struct.pack(">I", len(data)) + data)
    await writer.drain()


# ── small crypto helpers ─────────────────────────────────────────────────────
def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64ud(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def jwt_sign(payload: dict, key: bytes) -> str:
    h = b64u(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    p = b64u(json.dumps(payload, separators=(",", ":")).encode())
    sig = b64u(hmac.new(key, f"{h}.{p}".encode(), hashlib.sha256).digest())
    return f"{h}.{p}.{sig}"


def jwt_verify(token: str, key: bytes) -> dict | None:
    try:
        h, p, sig = token.split(".")
        want = b64u(hmac.new(key, f"{h}.{p}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, want):
            return None
        if json.loads(b64ud(h)).get("alg") != "HS256":
            return None
        return json.loads(b64ud(p))
    except Exception:
        return None


HUB_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
LINK_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L
PHONE_SCOPES = ("phone:read", "phone:act")
ALL_SCOPES = ("phone:read", "phone:act", "offline_access")
VERIFIER_RE = re.compile(r"^[A-Za-z0-9-._~]{43,128}$")

# Redirect URIs the agent platforms actually use (HANDOFF task 1), plus the
# OAuth loopback convention (any port, any path) for local clients.
PLATFORM_REDIRECTS = {
    "https://muse.ai/connect/oauth-callback",
    "https://agent.meta.ai/api/hatch/oauth/callback",
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "https://chatgpt.com/connector_platform_oauth_redirect",
    "https://vertexaisearch.cloud.google.com/oauth-redirect",
}
LOOPBACK_RE = re.compile(r"^http://(?:127\.0\.0\.1|\[::1\]|localhost)(?::\d+)?(?:/.*)?$")

SCOPE_WORDS = {
    "phone:read": "See what is on the phone screen (read-only)",
    "phone:act": "Control the phone: tap, type, scroll, open apps",
    "offline_access": "Stay connected until you revoke this access",
}


def redirect_allowed(uri: str) -> bool:
    return uri in PLATFORM_REDIRECTS or bool(LOOPBACK_RE.match(uri or ""))


def same_origin(a: str, b: str) -> bool:
    pa, pb = urllib.parse.urlsplit(a), urllib.parse.urlsplit(b)
    return (pa.scheme, pa.hostname, pa.port) == (pb.scheme, pb.hostname, pb.port)


def pkce_matches(verifier: str | None, challenge: str) -> bool:
    if not verifier or not VERIFIER_RE.match(verifier):
        return False
    got = b64u(hashlib.sha256(verifier.encode("ascii")).digest())
    return hmac.compare_digest(got, challenge)


def canonical_code(code: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", code or "").upper()


# ── persistent state ─────────────────────────────────────────────────────────
class RelayStore:
    """One JSON file: hubs (auth keys), registered clients, grants, hashed
    refresh tokens, revocations, server keys. Link/auth codes are runtime-only."""

    def __init__(self, path: str | None):
        self.path = path
        self.d: dict = {"hubs": {}, "clients": {}, "grants": {}, "refresh": {},
                        "revoked_jti": {}, "keys": {}}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.d.update(json.load(f))
        if not self.d["keys"].get("server_key"):
            self.d["keys"]["server_key"] = secrets.token_hex(32)
            self.save()
        if not self.d["keys"].get("admin_token"):
            self.d["keys"]["admin_token"] = secrets.token_urlsafe(24)
            self.save()
        for k, v in self.d["refresh"].items():  # drop expired at boot
            if v.get("exp", 0) < time.time():
                self.d["refresh"].pop(k, None)

    def save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.d, f, ensure_ascii=False, separators=(",", ":"))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    # keys derived from the persisted server key
    @property
    def jwt_key(self) -> bytes:
        return hashlib.sha256(("jwt:" + self.d["keys"]["server_key"]).encode()).digest()

    @property
    def form_key(self) -> bytes:
        return hashlib.sha256(("form:" + self.d["keys"]["server_key"]).encode()).digest()

    @property
    def admin_token(self) -> str:
        return self.d["keys"]["admin_token"]

    # ── owner-side administration (which agent may connect) ────────────────
    def grants(self) -> dict:
        return self.d["grants"]

    def grants_of(self, client_id: str) -> list[str]:
        return [gid for gid, g in self.d["grants"].items() if g["client_id"] == client_id]

    def revoke_grants(self, gids) -> int:
        """Revoke grants and every refresh family behind them."""
        gids = [g for g in gids if g in self.d["grants"] and not self.d["grants"][g].get("revoked")]
        for gid in gids:
            self.d["grants"][gid]["revoked"] = True
        for rec in self.d["refresh"].values():
            if rec.get("grant") in gids:
                rec["used"] = True  # kills the family: reuse detection fires on next use
        self.save()
        return len(gids)

    # hubs ------------------------------------------------------------------
    def add_hub(self, hub_id: str, secret: str | None = None) -> str:
        """Register a hub; returns the plaintext secret (generated if absent).
        Only sha256(secret) is stored — enough for the HMAC challenge/response."""
        if not HUB_RE.match(hub_id or ""):
            raise ValueError(f"bad hub id: {hub_id!r}")
        if not secret:
            secret = secrets.token_urlsafe(24)
        self.d["hubs"][hub_id] = {"key": hashlib.sha256(secret.encode()).hexdigest(),
                                  "created": time.time()}
        self.save()
        return secret

    def hub_key(self, hub_id: str) -> bytes | None:
        rec = self.d["hubs"].get(hub_id)
        return bytes.fromhex(rec["key"]) if rec else None

    # clients (RFC 7591 DCR) --------------------------------------------------
    def add_client(self, meta: dict) -> str:
        cid = "cli-" + secrets.token_hex(8)
        self.d["clients"][cid] = {**meta, "issued_at": time.time()}
        self.save()
        return cid

    def client(self, client_id: str) -> dict | None:
        return self.d["clients"].get(client_id)

    # grants ------------------------------------------------------------------
    def new_grant(self, hub: str, client_id: str, scope: set, resource: str) -> str:
        gid = "gr-" + secrets.token_hex(10)
        self.d["grants"][gid] = {"hub": hub, "client_id": client_id,
                                 "scope": sorted(scope), "resource": resource,
                                 "created": time.time(), "revoked": False}
        self.save()
        return gid

    def grant(self, gid: str) -> dict | None:
        return self.d["grants"].get(gid)

    def revoke_grant(self, gid: str) -> None:
        if gid in self.d["grants"]:
            self.d["grants"][gid]["revoked"] = True
            self.save()

    # refresh tokens ------------------------------------------------------------
    def add_refresh(self, gid: str, ttl: float, family: str | None = None) -> str:
        tok = "axr_" + secrets.token_urlsafe(32)
        self.d["refresh"][_hash(tok)] = {"grant": gid,
                                         "family": family or secrets.token_hex(8),
                                         "exp": time.time() + ttl, "used": False}
        self.save()
        return tok

    def get_refresh(self, tok: str) -> dict | None:
        return self.d["refresh"].get(_hash(tok))

    def mark_refresh_used(self, tok: str) -> None:
        rec = self.d["refresh"].get(_hash(tok))
        if rec is not None:
            rec["used"] = True
            self.save()

    def revoke_refresh_family(self, family: str) -> None:
        for rec in self.d["refresh"].values():
            if rec.get("family") == family:
                rec["used"] = True
        self.save()

    # access-token revocation -----------------------------------------------------
    def revoke_jti(self, jti: str, exp: float) -> None:
        self.d["revoked_jti"][jti] = exp
        now = time.time()
        for j, e in list(self.d["revoked_jti"].items()):
            if e < now:
                del self.d["revoked_jti"][j]
        self.save()

    def jti_revoked(self, jti: str) -> bool:
        return jti in self.d["revoked_jti"]


def _hash(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


# ── runtime hub connections ──────────────────────────────────────────────────
class HubOffline(Exception):
    pass


class HubTimeout(Exception):
    pass


class HubConn:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, serves: bool):
        self.reader, self.writer, self.serves = reader, writer, serves
        self.dead = False
        self.n = 0
        self.pending: dict[int, asyncio.Future] = {}

    def next_id(self) -> int:
        self.n += 1
        return self.n

    def resolve(self, frame: dict) -> None:
        fut = self.pending.pop(frame.get("id"), None)
        if fut and not fut.done():
            fut.set_result(frame)

    def fail_all(self) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("hub connection lost"))
        self.pending.clear()


class HubSlot:
    def __init__(self):
        self.lock = asyncio.Lock()   # one phone, one actor at a time
        self.conn: HubConn | None = None
        self.status: dict | None = None
        self.since: float | None = None


# ── the relay ────────────────────────────────────────────────────────────────
class _RateBucket:
    """Sliding-minute window per key (client IP)."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        if self.per_minute <= 0:
            return True
        now = time.time()
        window = [t for t in self.hits.get(key, []) if t > now - 60]
        if len(window) >= self.per_minute:
            self.hits[key] = window
            return False
        window.append(now)
        self.hits[key] = window
        if len(self.hits) > 4096:  # prune idle keys
            self.hits = {k: v for k, v in self.hits.items() if v[-1] > now - 60}
        return True


class Relay:
    def __init__(self, store: RelayStore, base_url: str, *, issuer: str | None = None,
                 fetch_client_metadata=None, link_ttl: float = 300, code_ttl: float = 300,
                 access_ttl: float = 3600, refresh_ttl: float = 30 * 86400,
                 request_timeout: float = 90, ping_every: float = 30,
                 audit=None, rate_auth_per_min: int = 30, rate_mcp_per_min: int = 300):
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.issuer = (issuer or self.base_url).rstrip("/")
        self.link_ttl, self.code_ttl = link_ttl, code_ttl
        self.access_ttl, self.refresh_ttl = access_ttl, refresh_ttl
        self.request_timeout, self.ping_every = request_timeout, ping_every
        self.audit = audit                  # axdroid.journal.Journal or None
        self.rl_auth = _RateBucket(rate_auth_per_min)
        self.rl_mcp = _RateBucket(rate_mcp_per_min)
        self.slots: dict[str, HubSlot] = {}
        self.link_codes: dict[str, dict] = {}     # canonical → {hub, exp}
        self.auth_codes: dict[str, dict] = {}     # code → record (memory only)
        self.used_codes: dict[str, tuple] = {}    # code → (grant, exp): replay detection
        self.cimd_cache: dict[str, tuple] = {}    # url → (doc, fetched_at)
        if fetch_client_metadata is not None:
            self.fetch_client_metadata = fetch_client_metadata

    def log(self, event: str, **facts):
        if self.audit is not None:
            self.audit.append(event, **facts)

    # ── metadata ────────────────────────────────────────────────────────────
    def as_metadata(self) -> dict:
        i = self.issuer
        return {
            "issuer": i,
            "authorization_endpoint": f"{i}/authorize",
            "token_endpoint": f"{i}/token",
            "registration_endpoint": f"{i}/register",
            "revocation_endpoint": f"{i}/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": True,
            "scopes_supported": list(ALL_SCOPES),
        }

    def resource_metadata(self, resource: str, hub: str | None = None) -> dict:
        return {"resource": resource, "authorization_servers": [self.issuer],
                "scopes_supported": list(PHONE_SCOPES), "bearer_methods_supported": ["header"],
                "resource_name": f"axdroid phone hub {hub}" if hub else "axdroid phone-use"}

    # ── link codes ──────────────────────────────────────────────────────────
    def new_link_code(self, hub: str) -> str:
        now = time.time()
        self.link_codes = {c: v for c, v in self.link_codes.items() if v["exp"] > now}
        self.used_codes = {c: v for c, v in self.used_codes.items() if v[1] > now}
        code = "-".join("".join(secrets.choice(LINK_ALPHABET) for _ in range(4)) for _ in range(2))
        self.link_codes[canonical_code(code)] = {"hub": hub, "exp": now + self.link_ttl}
        return code

    def take_link_code(self, code: str, hub: str) -> bool:
        rec = self.link_codes.pop(canonical_code(code), None)
        return bool(rec and rec["exp"] > time.time() and rec["hub"] == hub)

    # ── client resolution (DCR registry + CIMD) ─────────────────────────────
    async def fetch_client_metadata(self, url: str) -> dict | None:
        """Fetch a Client ID Metadata Document (CIMD). Overridable for tests.
        Default: plain HTTPS GET, 5 s timeout, 64 KiB cap."""
        def _get():
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.read(65536)
        try:
            loop = asyncio.get_running_loop()
            doc = json.loads(await loop.run_in_executor(None, _get))
        except Exception:
            return None
        return doc if isinstance(doc, dict) else None

    async def _resolve_client(self, client_id: str) -> dict | None:
        rec = self.store.client(client_id)
        if rec:
            return rec
        if client_id.startswith("https://"):  # CIMD: client_id IS the doc URL
            cached = self.cimd_cache.get(client_id)
            if cached and time.time() - cached[1] < 300:
                return cached[0]
            doc = await self.fetch_client_metadata(client_id)
            if not doc:
                return None
            uris = doc.get("redirect_uris")
            if not isinstance(uris, list) or not uris:
                return None
            if not all(redirect_allowed(u) for u in uris):
                return None
            if not all(same_origin(u, client_id) or LOOPBACK_RE.match(u) for u in uris):
                return None
            meta = {"client_name": doc.get("client_name") or urllib.parse.urlsplit(client_id).netloc,
                    "redirect_uris": uris, "via": "cimd"}
            self.cimd_cache[client_id] = (meta, time.time())
            return meta
        return None

    # ── authorize ───────────────────────────────────────────────────────────
    async def _authorize_ctx(self, q: dict) -> tuple[dict | None, tuple | None]:
        """Validate an authorization request. Returns (ctx, None) or (None, error)
        where error = ("page", status, title, detail) or ("redirect", uri)."""
        client_id = q.get("client_id", "")
        client = await self._resolve_client(client_id) if client_id else None
        redirect_uri = q.get("redirect_uri", "")
        redirect_ok = bool(client) and redirect_uri in (client.get("redirect_uris") or []) \
            and redirect_allowed(redirect_uri)
        if not redirect_ok:  # never redirect to an unvalidated URI
            return None, ("page", 400, "Unknown client or redirect URI",
                          f"client_id={client_id!r} redirect_uri={redirect_uri!r}")
        if q.get("response_type") != "code":
            return None, ("redirect", _add_query(redirect_uri, {"error": "unsupported_response_type",
                                                                "state": q.get("state", ""),
                                                                "iss": self.issuer}))
        scopes = set((q.get("scope") or " ".join(PHONE_SCOPES)).split())
        if not scopes or not scopes <= set(ALL_SCOPES) or not scopes & set(PHONE_SCOPES):
            return None, ("redirect", _add_query(redirect_uri, {"error": "invalid_scope",
                                                                "state": q.get("state", ""),
                                                                "iss": self.issuer}))
        if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
            return None, ("page", 400, "PKCE S256 required",
                          "send code_challenge (S256 of a 43–128 char verifier) and code_challenge_method=S256")
        resource = q.get("resource", "")
        m = re.match(r"^.*/h/([a-z0-9-]+)/mcp$", resource)
        if not m or not resource.startswith(self.base_url + "/") or not self.store.hub_key(m.group(1)):
            return None, ("page", 400, "Unknown resource",
                          f"resource must be {self.base_url}/h/<hub>/mcp for a registered hub")
        return {"client_id": client_id, "client": client, "redirect_uri": redirect_uri,
                "scope": scopes, "resource": resource, "hub": m.group(1),
                "code_challenge": q["code_challenge"], "state": q.get("state", "")}, None

    def _form_token(self, ctx: dict) -> str:
        payload = json.dumps({k: (sorted(ctx[k]) if isinstance(ctx[k], (set, frozenset)) else ctx[k])
                              for k in ("client_id", "redirect_uri", "scope", "resource",
                                        "code_challenge", "state")}, separators=(",", ":"),
                             ensure_ascii=False)
        body = b64u(payload.encode()) + "." + str(int(time.time()) + 600)
        sig = b64u(hmac.new(self.store.form_key, body.encode(), hashlib.sha256).digest())
        return f"{body}.{sig}"

    def _check_form_token(self, ctx: dict, token: str) -> bool:
        try:
            body, sig = token.rsplit(".", 1)
            blob, exp = body.rsplit(".", 1)
            want = b64u(hmac.new(self.store.form_key, body.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(sig, want) or float(exp) < time.time():
                return False
            saved = json.loads(b64ud(blob))
            return all(saved.get(k) == (sorted(ctx[k]) if isinstance(ctx[k], (set, frozenset)) else ctx[k])
                       for k in ("client_id", "redirect_uri", "scope", "resource", "code_challenge", "state"))
        except Exception:
            return False

    def _consent_page(self, ctx: dict, form_token: str, q: dict) -> bytes:
        esc = html.escape
        scopes = "".join(f"<li><code>{esc(s)}</code> — {esc(SCOPE_WORDS[s])}</li>"
                         for s in sorted(ctx["scope"]))
        hidden = "".join(f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">'
                         for k, v in q.items() if k != "form_token")
        return _PAGE.format(
            title="Connect an agent to your phone hub",
            body=f"""
<h1>Connect “{esc(ctx['client'].get('client_name') or ctx['client_id'])}”?</h1>
<p>This agent wants access to your phone hub
<strong>{esc(ctx['hub'])}</strong> at <code>{esc(ctx['resource'])}</code>.</p>
<ul class="scopes">{scopes}</ul>
<p>To approve, enter the <strong>link code</strong> your hub is showing
(<code>axdroid link</code> or the companion app). The code proves you are
physically holding the hub; it expires in 5 minutes and works once.</p>
<form method="post" action="{esc(self.issuer)}/authorize">
  <input type="hidden" name="form_token" value="{esc(form_token)}">{hidden}
  <label>Link code
    <input name="link_code" autocomplete="off" autofocus required
           placeholder="XXXX-XXXX" pattern="[A-Za-z0-9 -]{{6,14}}" maxlength="14">
  </label>
  <div class="btns">
    <button class="allow" name="decision" value="allow">Allow</button>
    <button class="deny" name="decision" value="deny" formnovalidate>Deny</button>
  </div>
</form>""").encode()

    # ── tunnel side ─────────────────────────────────────────────────────────
    async def tunnel_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        conn = None
        hub = None
        try:
            hello = await asyncio.wait_for(read_frame(reader), 15)
            hub = str(hello.get("hub") or "")
            key = self.store.hub_key(hub)
            if hello.get("t") != "hello" or hello.get("proto") != 1 or key is None:
                await send_frame(writer, {"t": "error", "code": "UNKNOWN_HUB" if key is None else "BAD_HELLO"})
                self.log("tunnel_rejected", hub=hub, code="UNKNOWN_HUB" if key is None else "BAD_HELLO")
                return
            salt = secrets.token_hex(16)
            await send_frame(writer, {"t": "challenge", "salt": salt})
            auth = await asyncio.wait_for(read_frame(reader), 15)
            want = hmac.new(key, f"{hub}|{hello.get('nonce', '')}|{salt}".encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(want, str(auth.get("mac") or "")):
                await send_frame(writer, {"t": "error", "code": "AUTH_FAILED"})
                self.log("tunnel_auth_failed", hub=hub)
                return
            conn = HubConn(reader, writer, serves=bool(hello.get("serves", True)))
            if conn.serves:  # only a serving connection owns the hub slot;
                slot = self.slots.setdefault(hub, HubSlot())  # link-only probes never evict it
                old = slot.conn
                slot.conn, slot.since = conn, time.time()
                if old is not None and old is not conn:  # reconnect race: newest wins
                    old.dead = True
                    old.fail_all()
                    old.writer.close()
            self.log("tunnel_up", hub=hub, serves=conn.serves)
            await send_frame(writer, {"t": "welcome", "hub": hub, "base": self.base_url,
                                      "issuer": self.issuer, "serves": conn.serves})
            slot = self.slots.get(hub)
            ping = asyncio.create_task(self._pinger(hub, conn)) if conn.serves else None
            try:
                while True:
                    f = await read_frame(reader)
                    t = f.get("t")
                    if t == "res" or t == "pong":
                        conn.resolve(f)
                    elif t == "link_create":
                        code = self.new_link_code(hub)
                        self.log("link_create", hub=hub)
                        await send_frame(writer, {"t": "link_ok", "id": f.get("id"), "code": code,
                                                  "expires_in": self.link_ttl})
                    elif t == "status":
                        slot.status = f.get("status") if isinstance(f.get("status"), dict) else slot.status
                    # unknown frames are ignored (forward compatibility)
            finally:
                if ping:
                    ping.cancel()
        except (FrameError, asyncio.IncompleteReadError, asyncio.TimeoutError,
                ConnectionError, json.JSONDecodeError, UnicodeDecodeError, ValueError, OSError):
            pass
        finally:
            if conn is not None:
                conn.dead = True
                conn.fail_all()
                slot = self.slots.get(hub or "")
                if slot and slot.conn is conn:
                    slot.conn = None
            writer.close()

    async def _pinger(self, hub: str, conn: HubConn):
        try:
            while not conn.dead:
                await asyncio.sleep(self.ping_every)
                i = conn.next_id()
                fut = asyncio.get_running_loop().create_future()
                conn.pending[i] = fut
                await send_frame(conn.writer, {"t": "ping", "id": i})
                await asyncio.wait_for(fut, 10)
        except Exception:
            conn.writer.close()

    async def forward(self, hub: str, msg: dict, scopes, actor: str | None = None) -> dict | None:
        slot = self.slots.get(hub)
        conn = slot.conn if slot else None
        if conn is None or conn.dead or not conn.serves:
            raise HubOffline(hub)
        async with slot.lock:
            i = conn.next_id()
            fut = asyncio.get_running_loop().create_future()
            conn.pending[i] = fut
            frame = {"t": "req", "id": i, "scopes": sorted(scopes), "msg": msg}
            if actor:
                frame["actor"] = actor
            await send_frame(conn.writer, frame)
            try:
                f = await asyncio.wait_for(fut, self.request_timeout)
            except asyncio.TimeoutError:
                conn.pending.pop(i, None)
                raise HubTimeout(hub)
        return f.get("reply")

    # ── HTTP side ──────────────────────────────────────────────────────────
    async def route(self, method: str, path: str, query: dict, headers: dict, body: bytes, ip: str = ""):
        """Returns (status:int, headers:dict, body:bytes). Pure enough to unit-test."""
        def js(obj, status=200, extra=None):
            return status, {"Content-Type": "application/json", "Cache-Control": "no-store", **(extra or {})}, \
                json.dumps(obj, ensure_ascii=False).encode()

        if path == "/" and method == "GET":
            return 200, {"Content-Type": "text/plain; charset=utf-8"}, \
                f"axdroid relay. AS: {self.issuer}/.well-known/oauth-authorization-server\n".encode()
        if path == "/healthz":
            online = sorted(h for h, s in self.slots.items() if s.conn and not s.conn.dead and s.conn.serves)
            return js({"ok": True, "issuer": self.issuer, "hubs_online": online})

        # ── owner administration: which agent may connect, which hub exists ──
        if path.startswith("/admin/"):
            auth = headers.get("authorization", "")
            ok_admin = auth.lower().startswith("bearer ") and \
                hmac.compare_digest(auth[7:].strip(), self.store.admin_token)
            if not ok_admin:
                return js({"error": "unauthorized",
                           "error_description": "relay admin token required "
                           "(axdroid relay --print-admin-token)"}, 401)
            if path == "/admin/grants" and method == "GET":
                return js({"grants": [{"id": gid, "client_id": g["client_id"], "hub": g["hub"],
                                       "scope": g["scope"], "resource": g["resource"],
                                       "created": round(g["created"], 1), "revoked": g.get("revoked", False)}
                                      for gid, g in sorted(self.store.grants().items(),
                                                           key=lambda kv: -kv[1]["created"])]})
            if path == "/admin/hubs" and method == "GET":
                return js({"hubs": [{"id": h, "online": bool(self.slots.get(h) and self.slots[h].conn
                                                              and not self.slots[h].conn.dead
                                                              and self.slots[h].conn.serves)}
                                    for h in sorted(self.store.d["hubs"])]})
            if path == "/admin/revoke" and method == "POST":
                try:
                    body_json = json.loads(body or b"{}")
                except json.JSONDecodeError:
                    return js({"error": "invalid_request"}, 400)
                grant, client, hub = body_json.get("grant"), body_json.get("client"), body_json.get("hub")
                if grant:
                    gids = [grant] if grant in self.store.grants() else []
                elif client:
                    gids = self.store.grants_of(client)
                elif hub:
                    gids = [gid for gid, g in self.store.grants().items() if g["hub"] == hub]
                else:
                    return js({"error": "invalid_request",
                               "error_description": "pass grant, client or hub"}, 400)
                n = self.store.revoke_grants(gids)
                self.log("admin_revoke", grant=grant or "", client=client or "", hub=hub or "", revoked=n)
                return js({"ok": True, "revoked": n})
            return js({"error": "not found"}, 404)

        # rate limits: the two endpoints an attacker can hammer
        if path in ("/token", "/authorize", "/register", "/revoke") and method == "POST":
            if not self.rl_auth.allow(ip):
                self.log("rate_limited", ip=ip, path=path)
                return js({"error": "slow_down", "error_description": "too many requests; retry later"},
                          429, {"Retry-After": "30"})
        elif re.match(r"^/h/[a-z0-9-]+/mcp/?$", path):
            if not self.rl_mcp.allow(ip):
                self.log("rate_limited", ip=ip, path=path)
                return js({"error": "slow_down", "error_description": "too many requests; retry later"},
                          429, {"Retry-After": "10"})
        if path == "/.well-known/oauth-authorization-server":
            return js(self.as_metadata())

        wm = re.match(r"^/\.well-known/oauth-protected-resource(/.*)?$", path)
        if wm:
            rest = (wm.group(1) or "/mcp").rstrip("/")
            hm = re.match(r"^/h/([a-z0-9-]+)/mcp$", rest)
            resource = f"{self.base_url}{rest}"
            return js(self.resource_metadata(resource, hm.group(1) if hm else None))

        if path == "/authorize":
            if method == "GET":
                return await self._authorize_get(query)
            if method == "POST":
                form = _form_of(headers, body)
                return await self._authorize_post(form, ip)
            return 405, {"Allow": "GET, POST"}, b""

        if path == "/token" and method == "POST":
            return await self._token(_form_of(headers, body), ip)
        if path == "/revoke" and method == "POST":
            return self._revoke(_form_of(headers, body), ip)
        if path == "/register" and method == "POST":
            return self._register(headers, body, ip)

        m = re.match(r"^/h/([a-z0-9-]+)/mcp/?$", path)
        if m:
            return await self._proxy(m.group(1), method, headers, body, ip)
        m = re.match(r"^/h/([a-z0-9-]+)/healthz$", path)
        if m:
            slot = self.slots.get(m.group(1))
            online = bool(slot and slot.conn and not slot.conn.dead and slot.conn.serves)
            return js({"hub": m.group(1), "online": online,
                       "status": slot.status if slot else None,
                       "since": slot.since if slot else None})
        return js({"error": "not_found"}, 404)

    # authorize: consent page + decision
    async def _authorize_get(self, q: dict):
        ctx, err = await self._authorize_ctx(q)
        if err:
            return _err_page(err)
        return 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}, \
            self._consent_page(ctx, self._form_token(ctx), q)

    async def _authorize_post(self, form: dict, ip: str = ""):
        ctx, err = await self._authorize_ctx(form)
        if err:
            if err[0] == "page":
                self.log("authorize_rejected", ip=ip, client_id=form.get("client_id", ""), reason=err[2])
            return _err_page(err)
        redirect = lambda extra: (302, {"Location": _add_query(ctx["redirect_uri"], {
            **extra, "state": ctx["state"], "iss": self.issuer}), "Cache-Control": "no-store"}, b"")
        if not self._check_form_token(ctx, form.get("form_token", "")):
            self.log("authorize_rejected", ip=ip, client_id=ctx["client_id"], reason="bad form token")
            return _err_page(("page", 400, "Form expired", "start the approval again from the agent"))
        if form.get("decision") != "allow":
            self.log("authorize_denied", ip=ip, client_id=ctx["client_id"], hub=ctx["hub"],
                     scopes=sorted(ctx["scope"]))
            return redirect({"error": "access_denied"})
        if not self.take_link_code(form.get("link_code", ""), ctx["hub"]):
            self.log("authorize_rejected", ip=ip, client_id=ctx["client_id"], hub=ctx["hub"], reason="link code")
            return _err_page(("page", 403, "Link code not accepted",
                              "ask the hub for a fresh code (axdroid link) and try again"))
        code = secrets.token_urlsafe(32)
        self.auth_codes[code] = {**{k: (sorted(v) if isinstance(v, (set, frozenset)) else v)
                                    for k, v in ctx.items()}, "exp": time.time() + self.code_ttl}
        self.log("authorize_allowed", ip=ip, client_id=ctx["client_id"], hub=ctx["hub"],
                 scopes=sorted(ctx["scope"]))
        return redirect({"code": code})

    # token endpoint
    async def _token(self, form: dict, ip: str = ""):
        def err(code, desc, status=400):
            self.log("token_error", ip=ip, error=code, grant_type=form.get("grant_type", ""))
            return status, {"Content-Type": "application/json", "Cache-Control": "no-store"}, \
                json.dumps({"error": code, "error_description": desc}).encode()

        gt = form.get("grant_type", "")
        if gt == "authorization_code":
            code = form.get("code", "")
            rec = self.auth_codes.pop(code, None)
            if rec is None:
                replay = self.used_codes.get(code)
                if replay:  # code replay → kill everything it issued (RFC 6749 §4.1.2)
                    self.store.revoke_grant(replay[0])
                    self.log("code_replay", ip=ip, grant=replay[0])
                return err("invalid_grant", "unknown authorization code")
            if rec["exp"] < time.time():
                return err("invalid_grant", "authorization code expired")
            if form.get("client_id", "") != rec["client_id"] \
                    or form.get("redirect_uri", "") != rec["redirect_uri"]:
                return err("invalid_grant", "client_id/redirect_uri mismatch")
            if not pkce_matches(form.get("code_verifier"), rec["code_challenge"]):
                return err("invalid_grant", "PKCE verification failed")
            gid = self.store.new_grant(rec["hub"], rec["client_id"], set(rec["scope"]), rec["resource"])
            self.used_codes[code] = (gid, time.time() + 600)
            self.log("token_issue", ip=ip, grant=gid, client_id=rec["client_id"],
                     hub=rec["hub"], scopes=sorted(rec["scope"]), grant_type=gt)
            return self._token_response(gid, set(rec["scope"]))
        if gt == "refresh_token":
            rec = self.store.get_refresh(form.get("refresh_token", ""))
            if rec is None:
                return err("invalid_grant", "unknown refresh token")
            if rec.get("used"):
                # reuse → revoke the whole family and the grant behind it
                self.store.revoke_refresh_family(rec["family"])
                self.store.revoke_grant(rec["grant"])
                self.log("refresh_reuse", ip=ip, grant=rec["grant"], client_id=form.get("client_id", ""))
                return err("invalid_grant", "refresh token reuse detected; access revoked")
            if rec.get("exp", 0) < time.time():
                return err("invalid_grant", "refresh token expired")
            g = self.store.grant(rec["grant"])
            if not g or g.get("revoked"):
                return err("invalid_grant", "grant revoked")
            self.store.mark_refresh_used(form["refresh_token"])
            self.log("token_issue", ip=ip, grant=rec["grant"], client_id=g["client_id"],
                     hub=g["hub"], scopes=sorted(g["scope"]), grant_type=gt)
            return self._token_response(rec["grant"], set(g["scope"]), family=rec["family"])
        return err("unsupported_grant_type", f"grant_type {gt!r} not supported")

    def _token_response(self, gid: str, scope: set, family: str | None = None):
        g = self.store.grant(gid)
        now = time.time()
        access = jwt_sign({"iss": self.issuer, "sub": gid, "aud": g["resource"],
                           "client_id": g["client_id"], "scope": " ".join(sorted(scope)),
                           "iat": int(now), "exp": int(now + self.access_ttl),
                           "jti": secrets.token_hex(12)}, self.store.jwt_key)
        out = {"access_token": access, "token_type": "Bearer",
               "expires_in": int(self.access_ttl), "scope": " ".join(sorted(scope))}
        if "offline_access" in scope or family:
            out["refresh_token"] = self.store.add_refresh(gid, self.refresh_ttl, family)
        return 200, {"Content-Type": "application/json", "Cache-Control": "no-store",
                     "Pragma": "no-cache"}, json.dumps(out).encode()

    # revocation (RFC 7009)
    def _revoke(self, form: dict, ip: str = ""):
        tok = form.get("token", "")
        payload = jwt_verify(tok, self.store.jwt_key)
        if payload and payload.get("iss") == self.issuer and payload.get("jti"):
            self.store.revoke_jti(payload["jti"], payload.get("exp", time.time() + self.access_ttl))
        else:
            rec = self.store.get_refresh(tok)
            if rec:
                self.store.revoke_refresh_family(rec["family"])
                self.store.revoke_grant(rec["grant"])
        self.log("revoke", ip=ip, client_id=form.get("client_id", ""),
                 kind="access" if payload else ("refresh" if self.store.get_refresh(tok) else "unknown"))
        return 200, {"Content-Type": "application/json"}, b"{}"

    # dynamic client registration (RFC 7591)
    def _register(self, headers: dict, body: bytes, ip: str = ""):
        def err(code, desc):
            return 400, {"Content-Type": "application/json"}, \
                json.dumps({"error": code, "error_description": desc}).encode()

        try:
            meta = json.loads(body or b"{}")
            if not isinstance(meta, dict):
                raise ValueError
        except ValueError:
            return err("invalid_client_metadata", "body must be a JSON object")
        uris = meta.get("redirect_uris")
        if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
            return err("invalid_redirect_uri", "redirect_uris must be a non-empty list of strings")
        if not all(redirect_allowed(u) for u in uris):
            return err("invalid_redirect_uri",
                       "redirect URIs must be loopback or one of the supported agent platforms")
        if meta.get("token_endpoint_auth_method") not in (None, "none"):
            return err("invalid_client_metadata", "only public clients (auth method 'none') are supported")
        if set(meta.get("grant_types", ["authorization_code", "refresh_token"])) - \
                {"authorization_code", "refresh_token"}:
            return err("invalid_client_metadata", "unsupported grant_types")
        clean = {"client_name": str(meta.get("client_name") or "agent")[:100],
                 "redirect_uris": uris, "token_endpoint_auth_method": "none",
                 "grant_types": ["authorization_code", "refresh_token"],
                 "response_types": ["code"]}
        cid = self.store.add_client(clean)
        self.log("register", ip=ip, client_id=cid, client_name=clean["client_name"])
        return 201, {"Content-Type": "application/json", "Cache-Control": "no-store"}, \
            json.dumps({"client_id": cid, "client_id_issued_at": int(time.time()),
                        "client_secret_expires_at": 0, **clean}).encode()

    # the resource proxy
    async def _proxy(self, hub: str, method: str, headers: dict, body: bytes, ip: str = ""):
        def js(obj, status=200, extra=None):
            return status, {"Content-Type": "application/json", "Cache-Control": "no-store", **(extra or {})}, \
                json.dumps(obj, ensure_ascii=False).encode()

        if self.store.hub_key(hub) is None:
            return js({"error": "unknown_hub", "hub": hub}, 404)
        if method == "DELETE":
            return 204, {}, b""
        if method != "POST":
            return js({"error": "method_not_allowed"}, 405, {"Allow": "POST"})
        resource = f"{self.base_url}/h/{hub}/mcp"
        chal = f'Bearer resource_metadata="{self.base_url}/.well-known/oauth-protected-resource/mcp"'
        auth = headers.get("authorization", "")
        scopes = None
        if auth.lower().startswith("bearer "):
            p = jwt_verify(auth[7:].strip(), self.store.jwt_key)
            if p and p.get("iss") == self.issuer:
                if p.get("aud") != resource:
                    return js({"error": "invalid_token",
                               "error_description": f"audience must be {resource}"}, 401,
                              {"WWW-Authenticate": chal + ', error="invalid_token"'})
                if p.get("exp", 0) < time.time() or self.store.jti_revoked(p.get("jti", "")):
                    return js({"error": "invalid_token"}, 401,
                              {"WWW-Authenticate": chal + ', error="invalid_token"'})
                g = self.store.grant(p.get("sub", ""))
                if not g or g.get("revoked"):
                    return js({"error": "invalid_token"}, 401,
                              {"WWW-Authenticate": chal + ', error="invalid_token"'})
                scopes = frozenset((p.get("scope") or "").split())
        if scopes is None:
            return js({"error": "unauthorized"}, 401, {"WWW-Authenticate": chal})
        if not scopes & set(PHONE_SCOPES):
            return js({"error": "insufficient_scope", "scope": "phone:read phone:act"}, 403,
                      {"WWW-Authenticate": chal + ', error="insufficient_scope"'})
        try:
            msg = json.loads(body or b"null")
        except json.JSONDecodeError:
            return js({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}, 400)
        batch = msg if isinstance(msg, list) else [msg]
        if not all(isinstance(m, dict) for m in batch):
            return js({"error": "invalid_request"}, 400)
        replies = []
        actor = p.get("client_id") if p else None
        t0 = time.time()
        try:
            for m in batch:
                r = await self.forward(hub, m, scopes, actor=actor)
                if r is not None:
                    replies.append(r)
            self.log("mcp", ip=ip, hub=hub, client=actor,
                     calls=[m.get("method") for m in batch if isinstance(m, dict)],
                     ms=round((time.time() - t0) * 1000, 1))
        except HubOffline:
            return js({"error": "hub_offline", "hub": hub,
                       "error_description": "the hub is not connected to the relay"}, 503, {"Retry-After": "5"})
        except HubTimeout:
            return js({"error": "hub_timeout", "hub": hub}, 504)
        except (ConnectionError, FrameError):  # hub died mid-request
            return js({"error": "hub_offline", "hub": hub,
                       "error_description": "the hub connection was lost"}, 503, {"Retry-After": "5"})
        if not replies:
            return 202, {}, b""
        extra = {"Mcp-Session-Id": secrets.token_hex(16)} \
            if any(m.get("method") == "initialize" for m in batch) else {}
        return js(replies if isinstance(msg, list) else replies[0], extra=extra)


def _form_of(headers: dict, body: bytes) -> dict:
    ct = headers.get("content-type", "")
    if ct.startswith("application/x-www-form-urlencoded") or (body and not ct):
        qs = urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
        return {k: v[0] for k, v in qs.items()}
    return {}


def _add_query(uri: str, params: dict) -> str:
    parts = urllib.parse.urlsplit(uri)
    q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    q += [(k, v) for k, v in params.items() if v != ""]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path,
                                    urllib.parse.urlencode(q), parts.fragment))


def _err_page(err: tuple) -> tuple:
    kind = err[0]
    if kind == "redirect":
        return 302, {"Location": err[1], "Cache-Control": "no-store"}, b""
    _, status, title, detail = err
    page = _PAGE.format(title=title, body=f"<h1>{html.escape(title)}</h1><p>{html.escape(detail)}</p>"
                                    "<p><a href='javascript:history.back()'>go back</a></p>")
    return status, {"Content-Type": "text/html; charset=utf-8"}, page.encode()


_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
:root{{color-scheme:light dark}}
body{{font:16px/1.5 system-ui,sans-serif;margin:0;display:grid;place-items:center;min-height:100vh;background:#f5f5f7;color:#1d1d1f}}
main{{background:#fff;border-radius:16px;box-shadow:0 4px 24px rgba(0,0,0,.08);padding:32px;max-width:460px;width:calc(100vw - 48px)}}
h1{{font-size:20px;margin:0 0 12px}}code{{background:#eee;padding:1px 5px;border-radius:4px;font-size:.9em}}
.scopes{{padding-left:18px}}.scopes li{{margin:6px 0}}
input[name=link_code]{{font:1.4em/1 ui-monospace,monospace;letter-spacing:.12em;padding:10px;width:100%;box-sizing:border-box;border:1px solid #ccc;border-radius:8px;margin-top:6px}}
.btns{{display:flex;gap:12px;margin-top:18px}}button{{flex:1;padding:12px;border:0;border-radius:10px;font-size:16px;cursor:pointer}}
.allow{{background:#0071e3;color:#fff}}.deny{{background:#e8e8ed;color:#1d1d1f}}
@media(prefers-color-scheme:dark){{body{{background:#000;color:#f5f5f7}}main{{background:#1c1c1e}}code{{background:#2c2c2e}}.deny{{background:#2c2c2e;color:#f5f5f7}}}}
</style></head><body><main>{body}</main></body></html>"""


# ── servers ─────────────────────────────────────────────────────────────────
async def _http_conn(relay: Relay, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer = writer.get_extra_info("peername")
    ip = peer[0] if isinstance(peer, tuple) and peer else ""
    try:
        while True:
            line = await reader.readline()
            if not line:
                return
            method, path, _ = line.decode("latin-1").split(" ", 2)
            headers = {}
            while True:
                h = await reader.readline()
                if h in (b"\r\n", b"\n", b""):
                    break
                k, _, v = h.decode("latin-1").partition(":")
                headers[k.strip().lower()] = v.strip()
            body = await reader.readexactly(int(headers.get("content-length", "0") or 0)) \
                if headers.get("content-length") else b""
            target = urllib.parse.urlsplit(path)
            query = {k: v[0] for k, v in urllib.parse.parse_qs(target.query, keep_blank_values=True).items()}
            status, rh, rb = await relay.route(method, target.path, query, headers, body, ip)
            rh = {"Content-Length": str(len(rb)), "Cache-Control": "no-store", **rh}
            writer.write(f"HTTP/1.1 {status}\r\n".encode()
                         + "".join(f"{k}: {v}\r\n" for k, v in rh.items()).encode() + b"\r\n" + rb)
            await writer.drain()
            if headers.get("connection", "").lower() == "close":
                return
    except (asyncio.IncompleteReadError, ConnectionError, ValueError, asyncio.LimitOverrunError):
        pass
    finally:
        writer.close()


async def serve_relay(host: str, http_port: int, tunnel_port: int | None, store: RelayStore, *,
                      base_url: str | None = None, issuer: str | None = None,
                      ssl_http=None, ssl_tunnel=None, audit=None, rate_auth_per_min: int = 30,
                      rate_mcp_per_min: int = 300, **kw) -> tuple[Relay, dict]:
    """Start both listeners. Returns (relay, {"http": srv, "tunnel": srv}).
    Set relay.base_url afterwards when http_port == 0 (ephemeral)."""
    relay = Relay(store, base_url or f"http://{host}:{http_port}", issuer=issuer,
                  audit=audit, rate_auth_per_min=rate_auth_per_min,
                  rate_mcp_per_min=rate_mcp_per_min, **kw)
    tunnel_port = tunnel_port if tunnel_port is not None else http_port + 1
    http = await asyncio.start_server(lambda r, w: _http_conn(relay, r, w), host, http_port, ssl=ssl_http)
    tunnel = await asyncio.start_server(relay.tunnel_connection, host, tunnel_port, ssl=ssl_tunnel)
    return relay, {"http": http, "tunnel": tunnel}


# ── CLI ─────────────────────────────────────────────────────────────────────
def main(argv=None):
    import argparse
    import ssl

    ap = argparse.ArgumentParser(prog="axdroid relay",
                                 description="cloud relay + authorization server (run this where the hubs can reach you)")
    ap.add_argument("--http", default="0.0.0.0:8080", metavar="HOST:PORT", help="public HTTP(S) listener")
    ap.add_argument("--tunnel", default=None, metavar="HOST:PORT", help="hub tunnel listener (default: http port+1)")
    ap.add_argument("--base-url", default=os.environ.get("AXDROID_RELAY_BASE"),
                    help="public https:// base of this relay (required behind a proxy)")
    ap.add_argument("--issuer", default=None, help="authorization server issuer URL (default: base-url)")
    ap.add_argument("--state", default=os.environ.get("AXDROID_RELAY_STATE",
                 os.path.join(os.environ.get("AXDROID_HOME", os.path.join(os.path.expanduser("~"), ".axdroid")), "relay-state.json")))
    ap.add_argument("--add-hub", action="append", default=[], metavar="ID[:SECRET]",
                    help="register a hub (generates and prints a secret when omitted); repeatable")
    ap.add_argument("--print-admin-token", action="store_true",
                    help="print the relay admin token (for /admin/* owner operations) and exit")
    ap.add_argument("--no-serve", action="store_true", help="register hubs and exit")
    ap.add_argument("--audit", default=None, metavar="FILE",
                    help="audit log (JSONL); default: next to the state file; 'off' disables")
    ap.add_argument("--rate-auth", type=int, default=30, metavar="N/MIN",
                    help="per-IP requests/min on /token+/authorize (0 = unlimited; default 30)")
    ap.add_argument("--rate-mcp", type=int, default=300, metavar="N/MIN",
                    help="per-IP requests/min on MCP calls (0 = unlimited; default 300)")
    ap.add_argument("--http-cert", default=None); ap.add_argument("--http-key", default=None)
    ap.add_argument("--tunnel-cert", default=None); ap.add_argument("--tunnel-key", default=None)
    a = ap.parse_args(argv)

    host, _, port = a.http.rpartition(":")
    tport = int(a.tunnel.rpartition(":")[2]) if a.tunnel else int(port) + 1
    store = RelayStore(a.state)
    audit = None
    if a.audit != "off":
        from .journal import Journal
        audit = Journal(a.audit or os.path.join(os.path.dirname(os.path.abspath(a.state)) or ".", "audit.jsonl"),
                        max_bytes=5_000_000)
    for spec in a.add_hub:
        hub_id, _, secret = spec.partition(":")
        secret = store.add_hub(hub_id, secret or None)
        print(f"hub {hub_id} secret: {secret}  (store it on the hub as AXDROID_HUB_SECRET)")
    if a.print_admin_token:
        print(f"relay admin token: {store.admin_token}")
        print("owner operations: GET /admin/grants · GET /admin/hubs · POST /admin/revoke {grant|client|hub}")
    if a.no_serve or a.print_admin_token:
        return 0

    def ctx(cert, key):
        if not (cert and key):
            return None
        c = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        c.load_cert_chain(cert, key)
        return c

    async def go():
        base = a.base_url or f"http://{host or '127.0.0.1'}:{port}"
        if not a.base_url and (int(port) == 443 or ctx(a.http_cert, a.http_key)):
            print("warning: public deployment — pass --base-url https://… so tokens carry the right audience", file=sys.stderr)
        relay, servers = await serve_relay(host or "0.0.0.0", int(port), tport, store, base_url=base,
                                           issuer=a.issuer, ssl_http=ctx(a.http_cert, a.http_key),
                                           ssl_tunnel=ctx(a.tunnel_cert, a.tunnel_key), audit=audit,
                                           rate_auth_per_min=a.rate_auth, rate_mcp_per_min=a.rate_mcp)
        hport = servers["http"].sockets[0].getsockname()[1]
        tports = servers["tunnel"].sockets[0].getsockname()[1]
        print(f"axdroid relay: http on {host or '0.0.0.0'}:{hport}, tunnel on {host or '0.0.0.0'}:{tports}, issuer {relay.issuer}", file=sys.stderr)
        async with servers["http"], servers["tunnel"]:
            await asyncio.gather(servers["http"].serve_forever(), servers["tunnel"].serve_forever())
    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
