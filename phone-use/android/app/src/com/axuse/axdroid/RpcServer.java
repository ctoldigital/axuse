package com.axuse.axdroid;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.nio.charset.Charset;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;

/**
 * The whole agent surface, served from the phone: a small HTTP/1.1 server on
 * the accessibility service's own process. Plain sockets, no dependencies.
 *
 *   GET  /healthz       no auth — proves the bridge is alive
 *   POST /rpc           JSON {method, params} → {ok, data} | {ok:false, error}
 *                       token auth; loopback (adb reverse / on-device) is exempt
 *   GET  /events        SSE change ticks {c: changeCount} — lets a host settle
 *                       without polling (it may still poll `changes`)
 *
 * Binds 127.0.0.1 by default; the owner's "Allow LAN" switch binds 0.0.0.0 and
 * the token is then required for every non-loopback connection.
 */
final class RpcServer implements Runnable {
    static final int PORT = 8360;
    private static final Charset UTF8 = Charset.forName("UTF-8");
    private static final int MAX_BODY = 8 * 1024 * 1024;

    private final AxService svc;
    private final Core core;
    private final AtomicBoolean running = new AtomicBoolean(false);
    private ServerSocket serverSocket;
    // unbounded on purpose: binder calls into a frozen app can block forever,
    // and a fixed pool would starve (measured on a Samsung freezer)
    private final ExecutorService pool = Executors.newCachedThreadPool(r -> {
        Thread t = new Thread(r, "axdroid-rpc-worker");
        t.setDaemon(true);
        return t;
    });

    RpcServer(AxService svc) {
        this.svc = svc;
        this.core = svc.core;
    }

    @Override
    public void run() {
        running.set(true);
        try {
            InetAddress bind = svc.policy.lan() ? InetAddress.getByName("0.0.0.0") : InetAddress.getByName("127.0.0.1");
            serverSocket = new ServerSocket();
            serverSocket.setReuseAddress(true);
            serverSocket.bind(new InetSocketAddress(bind, PORT), 8);
            svc.journal.append("server", "listening", true, null, "owner", 0,
                    (svc.policy.lan() ? "0.0.0.0" : "127.0.0.1") + ":" + PORT);
            while (running.get()) {
                Socket sock = serverSocket.accept();
                pool.execute(() -> serve(sock));
            }
        } catch (Exception e) {
            if (running.get()) {
                try {
                    svc.journal.append("server", "died", false, String.valueOf(e.getMessage()), "owner", 0, null);
                } catch (Exception ignored) {
                }
            }
        }
    }

    void stop() {
        running.set(false);
        try {
            if (serverSocket != null) serverSocket.close();
        } catch (IOException ignored) {
        }
        pool.shutdownNow();
    }

    /** One MCP JSON-RPC message (or batch) on the phone, watchdogged like /rpc. */
    private static String dispatchMcp(Mcp mcp, String body, String actor) {
        try {
            Object msg = new org.json.JSONTokener(body == null || body.isEmpty() ? "null" : body).nextValue();
            if (msg instanceof org.json.JSONArray) {
                org.json.JSONArray replies = new org.json.JSONArray();
                for (int i = 0; i < ((org.json.JSONArray) msg).length(); i++) {
                    Object m = ((org.json.JSONArray) msg).opt(i);
                    if (m instanceof JSONObject) replies.put(mcpReply(mcp, (JSONObject) m, actor));
                }
                return replies.toString();
            }
            if (msg instanceof JSONObject) {
                return mcpReply(mcp, (JSONObject) msg, actor).toString();
            }
            return "{\"jsonrpc\":\"2.0\",\"id\":null,\"error\":{\"code\":-32700,\"message\":\"parse error\"}}";
        } catch (Exception e) {
            return "{\"jsonrpc\":\"2.0\",\"id\":null,\"error\":{\"code\":-32603,\"message\":"
                    + JSONObject.quote(String.valueOf(e)) + "}}";
        }
    }

    private static JSONObject mcpReply(Mcp mcp, JSONObject msg, String actor) {
        try {
            return mcp.handle(msg, actor);
        } catch (Exception e) {
            try {
                return new JSONObject()
                        .put("jsonrpc", "2.0")
                        .put("id", msg.opt("id"))
                        .put("error", new JSONObject().put("code", -32603).put("message", String.valueOf(e)));
            } catch (Exception ignored) {
                return new JSONObject();
            }
        }
    }

    // ── per-connection loop (keep-alive) ──────────────────────────────

    /** One JSON-RPC dispatch, catching everything (runs inside a worker lambda). */
    private static String dispatchJson(Core core, String bodyStr, String actor, boolean loopback) {
        try {
            JSONObject req2 = new JSONObject(bodyStr == null || bodyStr.isEmpty() ? "{}" : bodyStr);
            String m = req2.optString("method");
            JSONObject params = req2.optJSONObject("params");
            if (params == null) params = new JSONObject();
            JSONObject data = core.dispatch(m, params, actor, loopback);
            JSONObject out = new JSONObject();
            out.put("ok", true);
            out.put("data", data);
            return out.toString();
        } catch (RpcError e) {
            try {
                JSONObject out = new JSONObject();
                out.put("ok", false);
                out.put("error", e.toError());
                return out.toString();
            } catch (Exception je) {
                return "{\"ok\":false,\"error\":{\"code\":\"" + e.code + "\"}}";
            }
        } catch (org.json.JSONException e) {
            return "{\"ok\":false,\"error\":{\"code\":\"BAD_JSON\",\"message\":"
                    + JSONObject.quote(String.valueOf(e.getMessage())) + "}}";
        } catch (Exception e) {
            try {
                JSONObject out = new JSONObject();
                out.put("ok", false);
                JSONObject err = new JSONObject();
                err.put("code", "INTERNAL");
                err.put("message", String.valueOf(e));
                out.put("error", err);
                return out.toString();
            } catch (Exception ignored) {
                return "{\"ok\":false,\"error\":{\"code\":\"INTERNAL\"}}";
            }
        }
    }

    private void serve(Socket sock) {
        try {
            sock.setTcpNoDelay(true);
            sock.setSoTimeout(20000);
            BufferedReader in = new BufferedReader(new InputStreamReader(sock.getInputStream(), "ISO-8859-1"));
            while (running.get()) {
                String line = in.readLine();
                if (line == null || line.isEmpty()) return;
                String[] req = line.split(" ");
                if (req.length < 3) return;
                String method = req[0], path = req[1];
                int contentLength = 0;
                boolean keepAlive = true;
                String auth = "";
                while (true) {
                    String h = in.readLine();
                    if (h == null || h.isEmpty()) break;
                    int c = h.indexOf(':');
                    if (c < 0) continue;
                    String k = h.substring(0, c).trim().toLowerCase(Locale.US);
                    String v = h.substring(c + 1).trim();
                    if (k.equals("content-length")) contentLength = Integer.parseInt(v);
                    else if (k.equals("connection")) keepAlive = !v.equalsIgnoreCase("close");
                    else if (k.equals("authorization")) auth = v;
                    else if (k.equals("x-token")) auth = "Bearer " + v;
                }
                if (contentLength < 0 || contentLength > MAX_BODY) {
                    writeJson(sock, 413, "{\"error\":\"body too large\"}", false);
                    return;
                }
                char[] body = new char[contentLength];
                int read = 0;
                while (read < contentLength) {
                    int n = in.read(body, read, contentLength - read);
                    if (n < 0) return;
                    read += n;
                }
                // read as ISO-8859-1 so one char = one byte of Content-Length;
                // the body itself is UTF-8 — undecoded, a raw "…" arrived as
                // "â¦" and taps on such names failed NOT_FOUND
                String bodyStr = new String(new String(body).getBytes(java.nio.charset.StandardCharsets.ISO_8859_1),
                        java.nio.charset.StandardCharsets.UTF_8);

                boolean loopback = sock.getInetAddress() != null && sock.getInetAddress().isLoopbackAddress();
                if (path.equals("/healthz") && method.equals("GET")) {
                    writeJson(sock, 200, "{\"ok\":true,\"app\":\"axdroid\",\"version\":\"" + Core.VERSION + "\"}", keepAlive);
                    continue;
                }
                String token = auth.toLowerCase(Locale.US).startsWith("bearer ") ? auth.substring(7).trim() : "";
                boolean authorized = loopback || (!token.isEmpty() && token.equals(svc.policy.token()));
                if (!authorized) {
                    writeJson(sock, 401, "{\"ok\":false,\"error\":{\"code\":\"UNAUTHORIZED\",\"message\":\"pairing token required (shown in the axdroid app; loopback is exempt)\"}}", false);
                    return;
                }
                if (path.equals("/events") && method.equals("GET")) {
                    serveEvents(sock, in);
                    return;
                }
                if (path.equals("/mcp") && method.equals("POST")) {
                    // on-device MCP: the phone answers the agent itself
                    String actor = loopback ? "loopback" : "lan";
                    String response = dispatchMcp(svc.mcp(), bodyStr, actor);
                    writeJson(sock, 200, response, keepAlive);
                    continue;
                }
                if (!path.equals("/rpc") || !method.equals("POST")) {
                    writeJson(sock, 404, "{\"error\":\"not found\"}", keepAlive);
                    continue;
                }
                String actor = loopback ? "loopback" : "lan";
                // watchdog: a binder call into a frozen app can block forever; the
                // server must answer even then (the stuck worker is abandoned)
                final String b = bodyStr;
                final boolean lo = loopback;
                java.util.concurrent.Future<String> task = java.util.concurrent.CompletableFuture.supplyAsync(
                        () -> dispatchJson(core, b, actor, lo), pool);
                String response;
                try {
                    response = task.get(30, java.util.concurrent.TimeUnit.SECONDS);
                } catch (java.util.concurrent.TimeoutException te) {
                    task.cancel(true);
                    response = "{\"ok\":false,\"error\":{\"code\":\"BRIDGE_TIMEOUT\","
                            + "\"message\":\"the phone did not answer in 30 s (an app may be frozen by the OS); "
                            + "wake the screen / reopen the app and retry\",\"retry\":\"later\"}}";
                } catch (Exception ex) {
                    response = "{\"ok\":false,\"error\":{\"code\":\"INTERNAL\",\"message\":" +
                            JSONObject.quote(String.valueOf(ex)) + "}}";
                }
                writeJson(sock, 200, response, keepAlive);
            }
        } catch (SocketTimeoutException ignored) {
        } catch (Exception ignored) {
        } finally {
            try {
                sock.close();
            } catch (IOException ignored) {
            }
        }
    }

    /** SSE stream of change ticks; the connection closes when the host does. */
    private void serveEvents(Socket sock, BufferedReader in) {
        try {
            OutputStream os = sock.getOutputStream();
            sock.setSoTimeout(0);
            byte[] head = ("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n"
                    + "Connection: keep-alive\r\n\r\n").getBytes(UTF8);
            os.write(head);
            os.flush();
            AtomicLong last = new AtomicLong(svc.changeCount.get());
            long lastBeat = System.currentTimeMillis();
            while (running.get()) {
                long c = svc.changeCount.get();
                if (c != last.get()) {
                    last.set(c);
                    os.write(("data: {\"c\":" + c + "}\n\n").getBytes(UTF8));
                    os.flush();
                    lastBeat = System.currentTimeMillis();
                } else if (System.currentTimeMillis() - lastBeat > 5000) {
                    os.write(": beat\n\n".getBytes(UTF8));
                    os.flush();
                    lastBeat = System.currentTimeMillis();
                }
                Thread.sleep(80);
            }
        } catch (Exception ignored) {
            // host went away — done
        }
    }

    private void writeJson(Socket sock, int status, String body, boolean keepAlive) {
        try {
            OutputStream os = sock.getOutputStream();
            byte[] b = body.getBytes(UTF8);
            String head = "HTTP/1.1 " + status + " " + (status == 200 ? "OK" : status == 401 ? "Unauthorized"
                    : status == 404 ? "Not Found" : status == 413 ? "Payload Too Large" : "Bad Request")
                    + "\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: " + b.length
                    + "\r\nConnection: " + (keepAlive ? "keep-alive" : "close") + "\r\n\r\n";
            os.write(head.getBytes(UTF8));
            os.write(b);
            os.flush();
        } catch (Exception ignored) {
        }
    }
}
