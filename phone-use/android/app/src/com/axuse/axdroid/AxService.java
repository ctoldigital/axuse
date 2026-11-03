package com.axuse.axdroid;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Intent;
import android.graphics.Path;
import android.graphics.Rect;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

/**
 * axdroid â€” the on-device half of axuse phone-use for Android.
 *
 * An AccessibilityService that exposes the phone's own UI structure (never
 * pixels) over a local HTTP RPC API: the phone is the hub, no companion
 * hardware, no ADB requirement for consumers, no screenshots anywhere.
 *
 * Design rules carried over from the axphone product values (not its code):
 *   - observe via the native accessibility tree only; there is no screenshot
 *     code path at all (the engine reports opaque screens instead)
 *   - act semantically first (performAction), gesture fallback with an
 *     occlusion check before touching
 *   - consequential actions are classified here, on the device, so every
 *     transport (LAN, ADB, anything speaking /rpc) gets the same guard
 *   - the owner's stop latch outranks every agent
 */
public class AxService extends AccessibilityService {

    static final String TAG = "axdroid";
    static volatile AxService instance;

    PolicyStore policy;
    Journal journal;
    Custody custody;
    RpcServer server;
    Thread serverThread;
    Core core;
    Mcp mcp;
    RelayTunnel relay;

    // Semantic change counter: every accessibility event the OS pushes us
    // (window state/content/scroll/text/click). The host settles on this
    // instead of polling full trees â€” same role as axphone's changes().
    final AtomicLong changeCount = new AtomicLong();
    final AtomicLong lastEventAt = new AtomicLong();   // elapsedRealtime ms
    volatile String foregroundPkg = null;

    @Override
    protected void onServiceConnected() {
        super.onServiceConnected();
        instance = this;
        policy = new PolicyStore(this);
        journal = new Journal(this);
        custody = new Custody(policy);
        policy.migrate(custody);
        core = new Core(this);
        mcp = new Mcp(this, core);
        foregroundPkg = null;
        postStatusNotification();
        if (policy.serverEnabled()) startServer();
        startRelayIfNeeded();
        journal.append("service", "connected", true, null, "owner", 0, null);
    }

    void startRelayIfNeeded() {
        stopRelay();
        RelayTunnel t = RelayTunnel.fromPrefs(this);
        if (t != null) {
            relay = t;
            relay.start();
        }
    }

    void stopRelay() {
        if (relay != null) relay.stop();
        relay = null;
    }

    @Override
    public boolean onUnbind(Intent intent) {
        stopServer();
        stopRelay();
        instance = null;
        journal.append("service", "disconnected", true, null, "owner", 0, null);
        return super.onUnbind(intent);
    }

    @Override
    public void onInterrupt() {
        // nothing: we produce no feedback output
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        // Owner controls arrive as service intents (notification actions).
        String action = intent != null ? intent.getAction() : null;
        if ("stop".equals(action) && custody != null) {
            custody.stop("notification");
            journal.append("owner_stop", "notification", true, null, "owner", 0, null);
        } else if ("resume".equals(action) && custody != null) {
            custody.resume();
            journal.append("owner_resume", "notification", true, null, "owner", 0, null);
        }
        postStatusNotification();
        return START_STICKY;
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        changeCount.incrementAndGet();
        lastEventAt.set(SystemClock.elapsedRealtime());
        int t = event.getEventType();
        if (t == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) {
            CharSequence pkg = event.getPackageName();
            if (pkg != null && !getPackageName().equals(String.valueOf(pkg))) {
                foregroundPkg = String.valueOf(pkg);
            }
        }
    }

    // â”€â”€ lifecycle helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    synchronized void startServer() {
        if (serverThread != null && serverThread.isAlive()) return;
        server = new RpcServer(this);
        serverThread = new Thread(server, "axdroid-rpc-accept");
        serverThread.start();
    }

    synchronized void stopServer() {
        if (server != null) server.stop();
        server = null;
        serverThread = null;
    }

    void postStatusNotification() {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (nm == null) return;
        if (android.os.Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel("status", "Agent bridge status", NotificationManager.IMPORTANCE_LOW);
            nm.createNotificationChannel(ch);
        }
        boolean stopped = custody != null && custody.isStopped();
        Intent open = new Intent(this, MainActivity.class);
        int piFlags = PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE;
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, "status")
                : new Notification.Builder(this);
        b.setSmallIcon(android.R.drawable.ic_menu_manage)
                .setContentTitle(stopped ? "axdroid â€” stopped by owner" : "axdroid â€” agent bridge ready")
                .setContentText(stopped ? "Reading still works; acting is refused."
                        : (policy != null && policy.lan() ? "LAN server on (token required)"
                        : "Local server on (adb reverse / localhost)"))
                .setContentIntent(PendingIntent.getActivity(this, 0, open, piFlags))
                .setOngoing(true);
        Intent stop = new Intent(this, AxService.class).setAction("stop");
        Intent resume = new Intent(this, AxService.class).setAction("resume");
        if (stopped) {
            b.addAction(0, "Resume agent", PendingIntent.getService(this, 2, resume, piFlags));
        } else {
            b.addAction(0, "Stop agent", PendingIntent.getService(this, 1, stop, piFlags));
        }
        // Foreground service (like TalkBack): Samsung's freezer otherwise caches
        // and freezes the process mid-session (measured: FreecessController UFZ).
        try {
            if (Build.VERSION.SDK_INT >= 34) {
                startForeground(1, b.build(),
                        android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
            } else {
                startForeground(1, b.build());
            }
        } catch (Exception e) {
            nm.notify(1, b.build());
        }
    }

    // â”€â”€ device facts â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    JSONObject deviceInfo() throws org.json.JSONException {
        JSONObject d = new JSONObject();
        d.put("model", Build.MODEL);
        d.put("manufacturer", Build.MANUFACTURER);
        d.put("os", "Android " + Build.VERSION.RELEASE);
        d.put("sdk", Build.VERSION.SDK_INT);
        // display metrics first: never touches the window binder (which can block
        // when another app is frozen)
        android.util.DisplayMetrics dm = getResources().getDisplayMetrics();
        int rotation = 0;
        try {
            android.view.WindowManager wm = (android.view.WindowManager) getSystemService(WINDOW_SERVICE);
            if (wm != null) rotation = wm.getDefaultDisplay().getRotation();
        } catch (Exception ignored) {
        }
        JSONObject s = new JSONObject();
        s.put("w", dm.widthPixels);
        s.put("h", dm.heightPixels);
        s.put("density", dm.density);
        s.put("rotation", rotation * 90);
        d.put("display", s);
        return d;
    }

    Rect screenBounds() {
        Rect best = new Rect();
        try {
            List<AccessibilityWindowInfo> ws = getWindows();
            for (AccessibilityWindowInfo w : ws) {
                Rect r = new Rect();
                w.getBoundsInScreen(r);
                if (r.width() * r.height() > best.width() * best.height()) best = r;
            }
        } catch (Exception ignored) {
        }
        if (best.isEmpty()) {
            android.util.DisplayMetrics dm = getResources().getDisplayMetrics();
            best.set(0, 0, dm.widthPixels, dm.heightPixels);
        }
        return best;
    }

    /** The on-device MCP engine (lazily never: created with the service). */
    Mcp mcp() {
        return mcp != null ? mcp : (mcp = new Mcp(this, core != null ? core : (core = new Core(this))));
    }

    /** ms since the last accessibility event (settle signal; -1 = none yet). */
    long quietMs() {
        long last = lastEventAt.get();
        return last == 0 ? -1 : SystemClock.elapsedRealtime() - last;
    }

    String foregroundPackage() {        if (foregroundPkg != null) return foregroundPkg;
        try {
            AccessibilityNodeInfo root = getRootInActiveWindow();
            if (root != null && root.getPackageName() != null) return String.valueOf(root.getPackageName());
        } catch (Exception ignored) {
        }
        return foregroundPkg;
    }

    // â”€â”€ tree access â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    /** Snapshot every on-screen window's tree. Active window first. */
    JSONObject observe(int maxNodes) throws RpcError, org.json.JSONException {
        return TreeDump.snapshot(this, maxNodes);
    }

    /** Resolve "w<windowId>:<i>/<j>..." to a live node (null if gone). */
    AccessibilityNodeInfo resolve(String id) {
        if (id == null) return null;
        try {
            int colon = id.indexOf(':');
            if (!id.startsWith("w") || colon < 0) return null;
            int winId = Integer.parseInt(id.substring(1, colon));
            String[] steps = id.substring(colon + 1).split("/");
            AccessibilityNodeInfo root = rootForWindow(winId);
            if (root == null) return null;
            AccessibilityNodeInfo node = root;
            for (String step : steps) {
                AccessibilityNodeInfo child = node.getChild(Integer.parseInt(step));
                if (child == null) return null;
                node = child;
            }
            return node;
        } catch (Exception e) {
            return null;
        }
    }

    private AccessibilityNodeInfo rootForWindow(int winId) {
        AccessibilityNodeInfo active = getRootInActiveWindow();
        if (active != null && active.getWindowId() == winId) return active;
        List<AccessibilityWindowInfo> ws = getWindows();
        if (ws == null) return null;
        for (AccessibilityWindowInfo w : ws) {
            if (w.getId() == winId) return w.getRoot();
        }
        return null;
    }

    /**
     * Self-healing resolve: path first; if the path is gone (RecyclerView
     * re-bound the row — its windowId or indexes changed), find the node by
     * its content signature, preferring the original window. Same identity
     * idea as axphone's ref registry, implemented with the live tree.
     */
    AccessibilityNodeInfo resolveSmart(String id, String sig, String rid, String text) {
        AccessibilityNodeInfo byPath = resolve(id);
        if (byPath != null) return byPath;
        if (sig == null || sig.isEmpty()) return null;
        int winId = -1;
        try {
            winId = Integer.parseInt(id.substring(1, id.indexOf(':')));
        } catch (Exception ignored) {
        }
        // prefer the original window, then everything else
        AccessibilityNodeInfo root = rootForWindow(winId);
        if (root != null) {
            AccessibilityNodeInfo hit = findBySignature(root, sig, rid, text, new int[]{0}, 2600);
            if (hit != null) return hit;
        }
        List<AccessibilityWindowInfo> ws = getWindows();
        if (ws != null) {
            for (AccessibilityWindowInfo w : ws) {
                if (w.getId() == winId) continue;
                AccessibilityNodeInfo r = w.getRoot();
                if (r == null) continue;
                AccessibilityNodeInfo hit = findBySignature(r, sig, rid, text, new int[]{0}, 2600);
                if (hit != null) return hit;
            }
        }
        return null;
    }

    private AccessibilityNodeInfo findBySignature(AccessibilityNodeInfo node, String sig, String rid, String text,
                                                  int[] budget, int cap) {
        if (budget[0]++ > cap || node == null) return null;
        if (sig.equals(nodeSig(node))) return node;
        for (int i = 0; i < node.getChildCount() && i < TreeDump.MAX_CHILDREN; i++) {
            AccessibilityNodeInfo c = node.getChild(i);
            if (c == null) continue;
            AccessibilityNodeInfo hit = findBySignature(c, sig, rid, text, budget, cap);
            if (hit != null) return hit;
        }
        return null;
    }

    /** The same content identity TreeDump writes into dumps. */
    static String nodeSig(AccessibilityNodeInfo node) {
        String t = chars(node.getText());
        String d = chars(node.getContentDescription());
        String s = (node.getClassName() == null ? "" : String.valueOf(node.getClassName())) + "|"
                + (node.getViewIdResourceName() == null ? "" : node.getViewIdResourceName()) + "|"
                + (t == null ? "" : t) + "|" + (d == null ? "" : d) + "|"
                + ((node.isCheckable() ? 16 : 0) | (node.isChecked() ? 32 : 0));
        return Integer.toHexString(s.hashCode());
    }

    /** The nearest node (self or ancestor) that carries this action bit. */
    static AccessibilityNodeInfo actionableAncestor(AccessibilityNodeInfo node, int cap) {
        AccessibilityNodeInfo n = node;
        for (int i = 0; n != null && i < cap; i++) {
            if (n.isClickable()) return n;
            n = n.getParent();
        }
        return null;
    }

    static AccessibilityNodeInfo scrollableAncestor(AccessibilityNodeInfo node, int cap) {
        AccessibilityNodeInfo n = node;
        for (int i = 0; n != null && i < cap; i++) {
            if (n.isScrollable()) return n;
            n = n.getParent();
        }
        return null;
    }

    // â”€â”€ gestures â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    /** Run one stroke through the given screen points; blocks until done. */
    boolean dispatchStroke(List<float[]> pts, long durationMs, long timeoutMs) throws RpcError {
        if (pts == null || pts.size() < 2) throw new RpcError("BAD_REQUEST", "gesture needs >= 2 points");
        Path path = new Path();
        path.moveTo(pts.get(0)[0], pts.get(0)[1]);
        for (int i = 1; i < pts.size(); i++) path.lineTo(pts.get(i)[0], pts.get(i)[1]);
        GestureDescription.Builder gb = new GestureDescription.Builder();
        gb.addStroke(new GestureDescription.StrokeDescription(path, 0, Math.max(10, durationMs)));
        final CountDownLatch done = new CountDownLatch(1);
        final boolean[] ok = {false};
        Handler h = new Handler(Looper.getMainLooper());
        h.post(() -> {
            try {
                boolean dispatched = dispatchGesture(gb.build(), new GestureResultCallback() {
                    @Override
                    public void onCompleted(GestureDescription gestureDescription) {
                        ok[0] = true;
                        done.countDown();
                    }

                    @Override
                    public void onCancelled(GestureDescription gestureDescription) {
                        ok[0] = false;
                        done.countDown();
                    }
                }, h);
                if (!dispatched) done.countDown();
            } catch (Exception e) {
                done.countDown();
            }
        });
        try {
            if (!done.await(timeoutMs, TimeUnit.MILLISECONDS)) {
                throw new RpcError("GESTURE_TIMEOUT", "gesture did not complete in " + timeoutMs + " ms", "retry", "never");
            }
        } catch (InterruptedException ie) {
            Thread.currentThread().interrupt();
            throw new RpcError("INTERRUPTED", "gesture wait interrupted");
        }
        return ok[0];
    }

    // â”€â”€ occlusion check (verify before touch) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    /**
     * Before a fallback gesture tap at (x,y) intended for `target`: find the
     * deepest visible node containing the point. It must be the target, a
     * descendant of it, or one of its ancestors â€” else something else is on
     * top and we refuse instead of touching blind. Mirrors axphone's
     * hit-test verify, implemented with the tree itself.
     */
    JSONObject verifyPoint(float x, float y, AccessibilityNodeInfo target) throws RpcError {
        try {
            List<AccessibilityWindowInfo> ws = getWindows();
            if (ws == null || ws.isEmpty()) return null;
            // topmost first: the highest window that contains the point decides
            ws = new java.util.ArrayList<>(ws);
            java.util.Collections.sort(ws, (a, b) -> Integer.compare(b.getLayer(), a.getLayer()));
            for (AccessibilityWindowInfo w : ws) {
                AccessibilityNodeInfo root = w.getRoot();
                if (root == null) continue;
                Rect wr = new Rect();
                w.getBoundsInScreen(wr);
                if (!wr.contains((int) x, (int) y)) continue;
                AccessibilityNodeInfo hit = deepestAt(root, x, y, new int[]{0}, 2200);
                if (hit == null) continue;
                if (isRelated(hit, target)) return null;
                Rect r = new Rect();
                hit.getBoundsInScreen(r);
                // a row's own tap surface: list rows often draw an unnamed
                // clickable layer (swipe foreground, ripple card) as a SIBLING
                // over their labels. It encloses the label and is far smaller
                // than the window (a scrim or dialog backdrop is not) — the
                // tap a human makes on the label lands on it by design.
                Rect tb = new Rect();
                target.getBoundsInScreen(tb);
                if (w.getId() == target.getWindowId() && chars(hit.getText()) == null
                        && chars(hit.getContentDescription()) == null && r.contains(tb)
                        && (long) r.width() * r.height() * 10 < (long) wr.width() * wr.height() * 4) {
                    return null;
                }
                JSONObject o = new JSONObject();
                o.put("code", "OCCLUDED");
                o.put("message", "the tap point is covered by another element");
                JSONObject coverer = new JSONObject();
                coverer.put("cls", hit.getClassName() == null ? null : String.valueOf(hit.getClassName()));
                coverer.put("text", chars(hit.getText()));
                coverer.put("desc", chars(hit.getContentDescription()));
                coverer.put("bounds", new JSONArray(new int[]{r.left, r.top, r.right, r.bottom}));
                o.put("coverer", coverer);
                return o;
            }
            return null; // no window claims the point: proceed
        } catch (Exception e) {
            return null; // verification is best-effort; the action itself still runs
        }
    }

    private static boolean isRelated(AccessibilityNodeInfo a, AccessibilityNodeInfo b) {
        if (a == null || b == null) return true;
        AccessibilityNodeInfo n = a;
        for (int i = 0; n != null && i < 64; i++) {
            if (n.equals(b)) return true;
            n = n.getParent();
        }
        n = b;
        for (int i = 0; n != null && i < 64; i++) {
            if (n.equals(a)) return true;
            n = n.getParent();
        }
        return false;
    }

    /** DFS for the deepest visible node whose bounds contain the point. */
    AccessibilityNodeInfo deepestAt(AccessibilityNodeInfo node, float x, float y, int[] budget, int maxNodes) {
        if (budget[0]++ > maxNodes || node == null) return null;
        Rect r = new Rect();
        node.getBoundsInScreen(r);
        if (!r.contains((int) x, (int) y)) return null;
        AccessibilityNodeInfo best = node.isVisibleToUser() ? node : null;
        for (int i = 0; i < node.getChildCount() && i < TreeDump.MAX_CHILDREN; i++) {
            AccessibilityNodeInfo c = node.getChild(i);
            if (c == null) continue;
            AccessibilityNodeInfo deeper = deepestAt(c, x, y, budget, maxNodes);
            if (deeper != null && deeper.isVisibleToUser()) best = deeper;
        }
        return best;
    }

    /** a node's text as content: line breaks kept, long documents kept
     *  (chars() collapsed every newline and cut at 220 chars, so agents
     *  never saw a note's lines or more than its first sentences) */
    static String rawText(CharSequence cs) {
        if (cs == null) return null;
        String s = String.valueOf(cs).replace("\r\n", "\n").trim();
        if (s.isEmpty()) return null;
        return s.length() > 4000 ? s.substring(0, 4000) : s;
    }

    static String chars(CharSequence cs) {
        if (cs == null) return null;
        String s = String.valueOf(cs).replaceAll("\\s+", " ").trim();
        if (s.isEmpty()) return null;
        return s.length() > 220 ? s.substring(0, 220) : s;
    }

    // â”€â”€ global buttons â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    boolean globalAction(String name) throws RpcError {
        Integer a;
        if ("back".equals(name)) a = GLOBAL_ACTION_BACK;
        else if ("home".equals(name)) a = GLOBAL_ACTION_HOME;
        else if ("recents".equals(name)) a = GLOBAL_ACTION_RECENTS;
        else if ("notifications".equals(name)) a = GLOBAL_ACTION_NOTIFICATIONS;
        else if ("quick_settings".equals(name)) a = GLOBAL_ACTION_QUICK_SETTINGS;
        else if ("power_dialog".equals(name)) a = GLOBAL_ACTION_POWER_DIALOG;
        else if ("lock".equals(name)) a = Build.VERSION.SDK_INT >= 28 ? GLOBAL_ACTION_LOCK_SCREEN : null;
        else if ("take_screenshot".equals(name)) a = Build.VERSION.SDK_INT >= 28 ? GLOBAL_ACTION_TAKE_SCREENSHOT : null;
        else a = null;
        if (a == null) throw new RpcError("BAD_REQUEST", "unknown global action '" + name + "'");
        boolean ok = performGlobalAction(a);
        if (!ok) throw new RpcError("ACTION_FAILED", "global action '" + name + "' returned false");
        return true;
    }
}

