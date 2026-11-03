package com.axuse.axdroid;

import android.content.SharedPreferences;

import org.json.JSONObject;

import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * The phone side of the relay connection — a port of axdroid/tunnel.py (wire
 * protocol identical to the axphone relay, so one relay serves both fleets):
 *
 *   phone → relay  {"t":"hello","hub":id,"proto":1,"nonce":n,"serves":true}
 *   relay → phone  {"t":"challenge","salt":s}
 *   phone → relay  {"t":"auth","mac":hmac_sha256(sha256(secret), hub|nonce|salt)}
 *   relay → phone  {"t":"welcome",…}
 *   relay → phone  {"t":"req","id":n,"scopes":[…],"msg":{jsonrpc}} → phone answers {"t":"res"}
 *   relay → phone  {"t":"ping"} → {"t":"pong"}   ·  link_create → link_ok (one-time codes)
 *
 * The phone dials OUT (nothing is opened on the home network); requests are
 * answered by the on-device MCP engine, so the guard, custody latch and
 * journal hold for relay traffic exactly like local traffic. Reconnects
 * back off 1 s → 30 s.
 */
final class RelayTunnel implements Runnable {
    private final AxService svc;
    private final String host;
    private final int port;
    private final String hubId;
    private final String secret;
    private final boolean tls;
    private volatile boolean running = true;
    private Thread thread;
    private volatile DataOutputStream out;   // the live connection's writer
    private final java.util.concurrent.ConcurrentHashMap<String, java.util.concurrent.SynchronousQueue<Object>> pendingLinks =
            new java.util.concurrent.ConcurrentHashMap<>();
    private java.util.concurrent.ConcurrentHashMap<String, Long> pendingPings = new java.util.concurrent.ConcurrentHashMap<>();

    RelayTunnel(AxService svc, String hostPort, String hubId, String secret, boolean tls) {
        this.svc = svc;
        String h = hostPort;
        int p = 7000;
        int c = hostPort.lastIndexOf(':');
        if (c > 0) {
            h = hostPort.substring(0, c);
            try {
                p = Integer.parseInt(hostPort.substring(c + 1));
            } catch (NumberFormatException ignored) {
            }
        }
        this.host = h.startsWith("[") ? h.substring(1, h.indexOf(']')) : h;
        this.port = p;
        this.hubId = hubId;
        this.secret = secret;
        this.tls = tls;
    }

    static RelayTunnel fromPrefs(AxService svc) {
        SharedPreferences sp = svc.getSharedPreferences("axdroid", android.content.Context.MODE_PRIVATE);
        String hostPort = sp.getString("relay_host", "");
        String id = sp.getString("relay_id", "");
        String secret = sp.getString("relay_secret", "");
        boolean enabled = sp.getBoolean("relay_enabled", false);
        boolean tls = sp.getBoolean("relay_tls", false);
        if (!enabled || hostPort.isEmpty() || id.isEmpty() || secret.isEmpty()) return null;
        return new RelayTunnel(svc, hostPort, id, secret, tls);
    }

    synchronized void start() {
        if (thread != null && thread.isAlive()) return;
        running = true;
        thread = new Thread(this, "axdroid-relay-tunnel");
        thread.setDaemon(true);
        thread.start();
    }

    synchronized void stop() {
        running = false;
        if (thread != null) thread.interrupt();
        thread = null;
    }

    boolean isRunning() {
        return thread != null && thread.isAlive();
    }

    // ── wire framing ──────────────────────────────────────────────────

    private static void sendFrame(DataOutputStream out, JSONObject obj) throws IOException {
        byte[] data = obj.toString().getBytes(StandardCharsets.UTF_8);
        out.writeInt(data.length);
        out.write(data);
        out.flush();
    }

    private static JSONObject readFrame(DataInputStream in) throws IOException, org.json.JSONException {
        int n = in.readInt();
        if (n <= 0 || n > (4 << 20)) throw new IOException("bad frame length " + n);
        byte[] buf = new byte[n];
        in.readFully(buf);
        return new JSONObject(new String(buf, StandardCharsets.UTF_8));
    }

    private static String hmacSha256(byte[] key, String data) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(key, "HmacSHA256"));
            byte[] out = mac.doFinal(data.getBytes(StandardCharsets.UTF_8));
            StringBuilder s = new StringBuilder();
            for (byte b : out) s.append(String.format("%02x", b));
            return s.toString();
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    private static byte[] sha256(String s) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(s.getBytes(StandardCharsets.UTF_8));
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    // ── connection loop ───────────────────────────────────────────────

    @Override
    public void run() {
        int attempt = 0;
        while (running && !Thread.currentThread().isInterrupted()) {
            long started = System.currentTimeMillis();
            try {
                connectOnce();
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                break;
            } catch (Exception e) {
                if (!running) break;
                try {
                    svc.journal.append("relay", "disconnected", false, String.valueOf(e.getMessage()),
                            "owner", 0, host + ":" + port);
                } catch (Exception ignored) {
                }
            }
            if (!running) break;
            attempt = (System.currentTimeMillis() - started >= 30_000) ? 0 : attempt + 1;
            long backoff = Math.min(30_000L, 1000L * (1L << Math.max(0, attempt - 1)));
            try {
                Thread.sleep(backoff);
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                break;
            }
        }
    }

    private void connectOnce() throws Exception {
        Socket sock;
        if (tls) {
            javax.net.ssl.SSLSocketFactory f = (javax.net.ssl.SSLSocketFactory) javax.net.ssl.SSLSocketFactory.getDefault();
            sock = f.createSocket(host, port);
        } else {
            sock = new Socket(host, port);
        }
        sock.setTcpNoDelay(true);
        DataOutputStream out = new DataOutputStream(sock.getOutputStream());
        DataInputStream in = new DataInputStream(sock.getInputStream());
        this.out = out;
        try {
            // ── handshake ──
            String nonce = String.format("%032x", new java.security.SecureRandom().nextLong())
                    + String.format("%032x", new java.security.SecureRandom().nextLong());
            sendFrame(out, new JSONObject()
                    .put("t", "hello").put("hub", hubId).put("proto", 1)
                    .put("nonce", nonce).put("serves", true));
            JSONObject f = readFrame(in);
            if (!"challenge".equals(f.optString("t"))) {
                throw new IOException("expected challenge, got " + f.optString("t"));
            }
            String mac = hmacSha256(sha256(secret), hubId + "|" + nonce + "|" + f.optString("salt"));
            sendFrame(out, new JSONObject().put("t", "auth").put("mac", mac));
            f = readFrame(in);
            if (!"welcome".equals(f.optString("t"))) {
                throw new IOException("relay refused: " + f.optString("code", f.optString("t")));
            }
            svc.journal.append("relay", "connected", true, null, "owner", 0, host + ":" + port);
            // ── serve ──
            while (running && !Thread.currentThread().isInterrupted()) {
                JSONObject frame;
                try {
                    frame = readFrame(in);
                } catch (EOFException | java.net.SocketException e) {
                    break;          // relay went away: reconnect with backoff
                }
                String t = frame.optString("t");
                if ("req".equals(t)) {
                    JSONObject reply = svc.mcp().handle(frame.optJSONObject("msg"), "relay");
                    sendFrame(out, new JSONObject().put("t", "res")
                            .put("id", frame.opt("id")).put("reply", reply));
                } else if ("ping".equals(t)) {
                    sendFrame(out, new JSONObject().put("t", "pong").put("id", frame.opt("id")));
                } else if ("status_req".equals(t)) {
                    sendFrame(out, new JSONObject().put("t", "status").put("status",
                            new JSONObject().put("app", "axdroid")));
                } else if ("link_ok".equals(t)) {
                    java.util.concurrent.SynchronousQueue<Object> q = pendingLinks.remove(frame.optString("id"));
                    if (q != null) q.offer(frame.optString("code"));
                } else if ("error".equals(t) && frame.has("id")) {
                    java.util.concurrent.SynchronousQueue<Object> q = pendingLinks.remove(frame.optString("id"));
                    if (q != null) q.offer(new IOException(frame.optString("code", "LINK_DENIED")));
                }
            }
        } finally {
            this.out = null;
            try {
                sock.close();
            } catch (IOException ignored) {
            }
        }
    }

    /** Mint a one-time agent link code through the live tunnel (possession of
     *  the phone); blocks up to timeoutMs. null = tunnel not connected. */
    String requestLinkCode(long timeoutMs) {
        DataOutputStream o = out;
        if (o == null || thread == null || !thread.isAlive()) return null;
        String qid = "q-" + Long.toHexString(System.nanoTime());
        java.util.concurrent.SynchronousQueue<Object> q = new java.util.concurrent.SynchronousQueue<>();
        pendingLinks.put(qid, q);
        try {
            synchronized (o) {
                sendFrame(o, new JSONObject().put("t", "link_create").put("id", qid));
            }
            Object r = q.poll(timeoutMs, java.util.concurrent.TimeUnit.MILLISECONDS);
            if (r instanceof String) return (String) r;
            if (r instanceof Exception) return null;
        } catch (Exception ignored) {
        } finally {
            pendingLinks.remove(qid);
        }
        return null;
    }
}
