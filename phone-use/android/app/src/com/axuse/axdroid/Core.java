package com.axuse.axdroid;

import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.graphics.Rect;
import android.net.Uri;
import android.os.Bundle;
import android.os.SystemClock;
import android.view.accessibility.AccessibilityNodeInfo;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.locks.ReentrantLock;

/**
 * The /rpc method table. One phone, one actor at a time: every acting method
 * runs under a single fair lock, so an agent's actions are serialized in
 * order and never interleave with a plan's steps.
 *
 * The action contract mirrors axphone's, implemented with Android's own
 * semantics:
 *   1. resolve the target by its path id (exact; stale â†’ STALE_REF)
 *   2. guard: consequential labels need confirm:true â€” decided here, on the
 *      device, so every transport gets it
 *   3. act semantically first (performAction â€” the TalkBack contract),
 *      falling back to a gesture at the visible centre, but only after an
 *      occlusion check of the fresh tree
 *   4. the change counter tells the host when the screen has settled
 */
final class Core {

    /** launch-name aliases → package. Benchmark + common apps whose
     *  launcher label doesn't contain the name an agent will use. */
    static final java.util.Map<String, String> ALIASES = new java.util.HashMap<>();
    static {
        ALIASES.put("simple sms messenger", "com.simplemobiletools.smsmessenger");
        ALIASES.put("sms messenger", "com.simplemobiletools.smsmessenger");
        ALIASES.put("simple sms", "com.simplemobiletools.smsmessenger");
        ALIASES.put("simple calendar pro", "com.simplemobiletools.calendar.pro");
        ALIASES.put("simple calendar", "com.simplemobiletools.calendar.pro");
        ALIASES.put("simple gallery pro", "com.simplemobiletools.gallery.pro");
        ALIASES.put("simple gallery", "com.simplemobiletools.gallery.pro");
        ALIASES.put("simple draw pro", "com.simplemobiletools.draw.pro");
        ALIASES.put("simple draw", "com.simplemobiletools.draw.pro");
        ALIASES.put("markor", "net.gsantner.markor");
        ALIASES.put("osmand", "net.osmand.plus");
        ALIASES.put("tasks", "org.tasks");
        ALIASES.put("tasks app", "org.tasks");
        ALIASES.put("opentracks", "de.dennisguse.opentracks");
        ALIASES.put("open tracks", "de.dennisguse.opentracks");
        ALIASES.put("open tracks sports tracker", "de.dennisguse.opentracks");
        ALIASES.put("activity tracker", "de.dennisguse.opentracks");
        ALIASES.put("sports tracker", "de.dennisguse.opentracks");
        ALIASES.put("joplin", "net.cozic.joplin");
        ALIASES.put("joplin app", "net.cozic.joplin");
        ALIASES.put("vlc", "org.videolan.vlc");
        ALIASES.put("vlc player", "org.videolan.vlc");
        ALIASES.put("vlc app", "org.videolan.vlc");
        ALIASES.put("retro music", "code.name.monkey.retromusic");
        ALIASES.put("retro player", "code.name.monkey.retromusic");
        ALIASES.put("retro", "code.name.monkey.retromusic");
        ALIASES.put("pro expense", "com.arduia.expense");
        ALIASES.put("expense", "com.arduia.expense");
        ALIASES.put("broccoli", "com.flauschcode.broccoli");
        ALIASES.put("recipe app", "com.flauschcode.broccoli");
        ALIASES.put("broccoli app", "com.flauschcode.broccoli");
        ALIASES.put("audio recorder", "com.dimowner.audiorecorder");
        ALIASES.put("settings", "com.android.settings");
        ALIASES.put("contacts", "com.android.contacts");
        ALIASES.put("phone", "com.google.android.dialer");
        ALIASES.put("dialer", "com.google.android.dialer");
        ALIASES.put("clock", "com.google.android.deskclock");
        ALIASES.put("camera", "com.android.camera2");
        ALIASES.put("files", "com.android.documentsui");
        ALIASES.put("chrome", "com.android.chrome");
        ALIASES.put("browser", "com.android.chrome");
        ALIASES.put("calculator", "com.google.android.calculator");
        ALIASES.put("calendar", "com.google.android.calendar");
    }
    static final String VERSION = "0.1.0";
    private final AxService svc;
    private final ReentrantLock actLock = new ReentrantLock(true);
    private long waitLastDumpChange = -1;
    private JSONArray waitLastNodes = null;

    Core(AxService svc) {
        this.svc = svc;
    }

    JSONObject dispatch(String method, JSONObject p, String actor, boolean loopback) throws RpcError, org.json.JSONException {
        if (method == null) throw new RpcError("BAD_REQUEST", "missing method");
        // owner controls are never custody-checked: they are how the owner re-enables acting
        boolean isOwnerControl = method.equals("stop") || method.equals("resume") || method.equals("relay_set")
                || method.equals("relay_link");
        boolean act = !isOwnerControl
                && !(method.equals("status") || method.equals("observe") || method.equals("changes")
                || method.equals("apps") || method.equals("journal") || method.equals("policy_get")
                || method.equals("relay_get") || method.equals("healthz"));
        String custody = svc.custody.check(act);
        if (custody != null) {
            svc.journal.append("refused", method, false, custody, actor, 0, p.optString("id"));
            throw new RpcError(custody, custodyHint(custody));
        }
        if (method.equals("status")) return status();
        if (method.equals("observe")) return svc.observe(p.optInt("max_nodes", 1500));
        if (method.equals("changes")) return changes();
        if (method.equals("act")) return act(p, actor);
        if (method.equals("type")) return type(p, actor);
        if (method.equals("wait")) return wait(p);
        if (method.equals("apps")) return apps();
        if (method.equals("launch")) return launch(p, actor);
        if (method.equals("open_uri")) return openUri(p);
        if (method.equals("clipboard")) return clipboard(p);
        if (method.equals("stop")) {
            svc.custody.stop(p.optString("reason", "rpc"));
            svc.journal.append("owner_stop", "rpc", true, null, actor, 0, null);
            svc.postStatusNotification();
            return new JSONObject().put("ok", true).put("custody", svc.custody.facts());
        }
        if (method.equals("resume")) {
            svc.custody.resume();
            svc.journal.append("owner_resume", "rpc", true, null, actor, 0, null);
            svc.postStatusNotification();
            return new JSONObject().put("ok", true).put("custody", svc.custody.facts());
        }
        if (method.equals("journal")) {
            JSONArray a = new JSONArray();
            for (JSONObject j : svc.journal.tail(p.optInt("n", 50))) a.put(j);
            return new JSONObject().put("events", a);
        }
        if (method.equals("policy_get")) return policyGet();
        if (method.equals("policy_set")) {
            if (!loopback) throw new RpcError("OWNER_ONLY", "policy changes are accepted from the phone itself (loopback) only");
            return policySet(p);
        }
        if (method.equals("relay_get")) {
            JSONObject o = new JSONObject();
            o.put("enabled", sp().getBoolean("relay_enabled", false));
            o.put("host", sp().getString("relay_host", ""));
            o.put("id", sp().getString("relay_id", ""));
            o.put("tls", sp().getBoolean("relay_tls", false));
            o.put("connected", svc.relay != null && svc.relay.isRunning());
            return o;
        }
        if (method.equals("relay_link")) {
            // the owner asks the PHONE for the one-time agent link code — minted
            // through the phone's own tunnel, so possession is proven here
            if (svc.relay == null || !svc.relay.isRunning())
                throw new RpcError("RELAY_OFFLINE", "the relay tunnel is not connected");
            String code = svc.relay.requestLinkCode(5000);
            if (code == null) throw new RpcError("LINK_TIMEOUT", "the relay did not mint a code in time");
            svc.journal.append("link_code", "rpc", true, null, "owner", 0, null);
            JSONObject o = new JSONObject();
            o.put("code", code);
            o.put("expires_in", 300);
            o.put("note", "type this into the agent's approval page; valid once, 5 min");
            return o;
        }
        if (method.equals("relay_set")) {
            // the owner configures the phone's outbound relay from the phone
            // itself (loopback via adb) — never from the network
            if (!loopback) throw new RpcError("OWNER_ONLY", "relay config is accepted from the phone itself (loopback) only");
            if (p.has("host")) sp().edit().putString("relay_host", p.optString("host")).commit();
            if (p.has("id")) sp().edit().putString("relay_id", p.optString("id")).commit();
            if (p.has("secret")) sp().edit().putString("relay_secret", p.optString("secret")).commit();
            if (p.has("tls")) sp().edit().putBoolean("relay_tls", p.optBoolean("tls")).commit();
            if (p.has("enabled")) sp().edit().putBoolean("relay_enabled", p.optBoolean("enabled")).commit();
            svc.startRelayIfNeeded();
            svc.journal.append("owner_relay", "rpc", true, null, "owner", 0, p.optString("host"));
            try {
                Thread.sleep(700);   // let the tunnel try to connect before reporting
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
            }
            JSONObject o = new JSONObject();
            o.put("enabled", sp().getBoolean("relay_enabled", false));
            o.put("host", sp().getString("relay_host", ""));
            o.put("id", sp().getString("relay_id", ""));
            o.put("connected", svc.relay != null && svc.relay.isRunning());
            return o;
        }
        if (method.equals("reset_token")) {
            if (!loopback) throw new RpcError("OWNER_ONLY", "token reset is accepted from the phone itself (loopback) only");
            svc.policy.regenToken();
            svc.journal.append("owner_reset_token", "rpc", true, null, actor, 0, null);
            return new JSONObject().put("ok", true).put("token", svc.policy.token());
        }
        if (method.equals("screenshot")) {
            return screenshotGuarded(p.optBoolean("force", false), p.optInt("max_px", 1280), p.optBoolean("grid", true), null, regionOf(p.optJSONArray("region")));
        }
        throw new RpcError("UNKNOWN_METHOD", "unknown method '" + method + "'");
    }

    private android.content.SharedPreferences sp() {
        return svc.getSharedPreferences("axdroid", android.content.Context.MODE_PRIVATE);
    }

    private static String custodyHint(String code) {
        if ("STOPPED_BY_OWNER".equals(code)) return "the owner stopped the bridge (notification action, axdroid app, or RPC resume)";
        if ("SESSION_EXPIRED".equals(code)) return "the session box expired; the owner must resume";
        if ("IDLE_TIMEOUT".equals(code)) return "idle box exceeded; any successful call re-arms it after resume";
        return code;
    }

    // â”€â”€ reads â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    private JSONObject status() throws org.json.JSONException {
        JSONObject o = new JSONObject();
        o.put("app", "axdroid");
        o.put("version", VERSION);
        o.put("device", svc.deviceInfo());
        o.put("foreground", svc.foregroundPackage());
        o.put("policy", policyGet());
        o.put("custody", svc.custody.facts());
        JSONObject s = new JSONObject();
        s.put("connected", true);
        s.put("lan", svc.policy.lan());
        s.put("screenshots", svc.policy.screenshots());
        s.put("port", RpcServer.PORT);
        s.put("changes", svc.changeCount.get());
        s.put("quiet_ms", quietMs());
        o.put("service", s);
        return o;
    }

    private long quietMs() {
        long last = svc.lastEventAt.get();
        if (last == 0) return -1;
        return SystemClock.elapsedRealtime() - last;
    }

    private JSONObject changes() throws org.json.JSONException {
        JSONObject o = new JSONObject();
        o.put("count", svc.changeCount.get());
        o.put("quiet_ms", quietMs());
        return o;
    }

    private JSONObject apps() throws org.json.JSONException {
        PackageManager pm = svc.getPackageManager();
        Intent main = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
        List<ResolveInfo> ris = pm.queryIntentActivities(main, 0);
        JSONArray a = new JSONArray();
        for (ResolveInfo ri : ris) {
            try {
                JSONObject app = new JSONObject();
                app.put("pkg", ri.activityInfo.packageName);
                String label = String.valueOf(ri.loadLabel(pm));
                app.put("name", label);
                a.put(app);
            } catch (Exception ignored) {
            }
        }
        return new JSONObject().put("apps", a);
    }

    private JSONObject policyGet() throws org.json.JSONException {
        JSONObject o = new JSONObject();
        o.put("guard_mode", svc.policy.guardMode());
        Set<String> allow = svc.policy.allowApps();
        o.put("allow_apps", allow.isEmpty() ? null : new JSONArray(allow));
        o.put("deny_apps", new JSONArray(svc.policy.denyApps()));
        Set<String> kinds = svc.policy.allowKinds();
        o.put("allow_kinds", kinds == null ? null : new JSONArray(kinds));
        o.put("max_session_min", svc.policy.maxSessionMin());
        o.put("idle_min", svc.policy.idleMin());
        return o;
    }

    /** Screenshot fallback for screens whose accessibility tree cannot say what is there (games, maps,
     *  canvases, photos). The owner can switch it off; a screen with plenty of readable text is refused
     *  unless the caller insists with force:true. */
    JSONObject screenshotGuarded(boolean force, int maxPx, boolean grid, org.json.JSONArray marks, int[] region) throws RpcError, org.json.JSONException {
        if (!svc.policy.screenshots())
            throw new RpcError("SCREENSHOTS_OFF", "The phone's owner has turned screenshots off in the axdroid app.");
        if (!force) {
            JSONObject dump = svc.observe(1500);
            int readable = readableLabels(dump);
            if (readable >= 3 && !hasOpaqueRegion(dump))
                throw new RpcError("SCREENSHOT_NOT_NEEDED", "The accessibility tree explains this screen ("
                        + readable + " readable elements, nothing opaque): use phone_observe or phone_find."
                        + " If it really lacks what you need (a map, game, canvas or photo), call phone_screenshot"
                        + " again with force:true.");
        }
        stableWait(0.3, 2);          // do not capture mid-animation
        return svc.screenshot(maxPx, grid, marks, region);
    }

    private static int[] regionOf(org.json.JSONArray a) {
        if (a == null || a.length() != 4) return null;
        return new int[]{a.optInt(0), a.optInt(1), a.optInt(2), a.optInt(3)};
    }

    /** Nodes with a visible text or content description: how much the tree can tell. */
    private static int readableLabels(JSONObject dump) {
        int n = 0;
        org.json.JSONArray nodes = dump.optJSONArray("nodes");
        if (nodes == null) return 0;
        for (int i = 0; i < nodes.length(); i++) {
            JSONObject o = nodes.optJSONObject(i);
            if (o == null) continue;
            if (!o.optString("text", "").trim().isEmpty() || !o.optString("desc", "").trim().isEmpty()) n++;
        }
        return n;
    }

    /** A large visible leaf that says nothing the tree can use: a drawing canvas, a game or map surface,
     *  a photo. That is what a screenshot is for. */
    private boolean hasOpaqueRegion(JSONObject dump) throws org.json.JSONException {
        org.json.JSONArray nodes = dump.optJSONArray("nodes");
        JSONObject display = svc.deviceInfo().optJSONObject("display");
        if (nodes == null || display == null) return false;
        double screen = (double) display.optInt("w") * display.optInt("h");
        if (screen <= 0) return false;
        for (int i = 0; i < nodes.length(); i++) {
            JSONObject o = nodes.optJSONObject(i);
            if (o == null || (o.optInt("f") & 1024) == 0) continue;               // visible only
            boolean leaf = i + 1 >= nodes.length() || nodes.optJSONObject(i + 1).optInt("d") <= o.optInt("d");
            org.json.JSONArray b = o.optJSONArray("b");
            if (!leaf || b == null || b.length() < 4) continue;
            double area = Math.max(0, b.optInt(2) - b.optInt(0)) * (double) Math.max(0, b.optInt(3) - b.optInt(1));
            if (area < 0.25 * screen) continue;
            String cls = o.optString("cls", "");
            boolean unlabelled = o.optString("text", "").trim().isEmpty() && o.optString("desc", "").trim().isEmpty();
            if (unlabelled || cls.endsWith("ImageView") || cls.endsWith("SurfaceView") || cls.endsWith("TextureView")) return true;
        }
        return false;
    }

    private JSONObject policySet(JSONObject p) throws RpcError, org.json.JSONException {
        boolean wasLan = svc.policy.lan();
        boolean wasOn = svc.policy.serverEnabled();
        if (p.has("guard_mode")) {
            String m = p.optString("guard_mode");
            if (!m.equals("ask") && !m.equals("block") && !m.equals("allow"))
                throw new RpcError("BAD_REQUEST", "guard_mode must be ask|block|allow");
            svc.policy.setGuardMode(m);
        }
        if (p.has("allow_apps")) svc.policy.setAllowApps(toStringSet(p.optJSONArray("allow_apps")));
        if (p.has("deny_apps")) svc.policy.setDenyApps(toStringSet(p.optJSONArray("deny_apps")));
        if (p.has("allow_kinds")) svc.policy.setAllowKinds(toStringSet(p.optJSONArray("allow_kinds")));
        if (p.has("max_session_min") || p.has("idle_min"))
            svc.policy.setBoxes(p.optInt("max_session_min", svc.policy.maxSessionMin()),
                    p.optInt("idle_min", svc.policy.idleMin()));
        if (p.has("lan")) svc.policy.setLan(p.optBoolean("lan"));
        if (p.has("screenshots")) svc.policy.setScreenshots(p.optBoolean("screenshots"));
        if (p.has("server_enabled")) svc.policy.setServerEnabled(p.optBoolean("server_enabled"));
        svc.journal.append("owner_policy", "rpc", true, null, "owner", 0, null);
        if (svc.policy.serverEnabled() != wasOn || svc.policy.lan() != wasLan) {
            svc.stopServer();
            if (svc.policy.serverEnabled()) svc.startServer();
        }
        svc.postStatusNotification();
        return policyGet();
    }

    private static Set<String> toStringSet(JSONArray a) {
        Set<String> out = new HashSet<String>();
        if (a != null) for (int i = 0; i < a.length(); i++) out.add(a.optString(i));
        return out;
    }

    // â”€â”€ the action path â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    private JSONObject act(JSONObject p, String actor) throws RpcError, org.json.JSONException {
        actLock.lock();
        long t0 = SystemClock.elapsedRealtime();
        try {
            JSONObject out = actLocked(p, actor);
            out.put("ms", SystemClock.elapsedRealtime() - t0);
            out.put("changes", svc.changeCount.get());
            svc.custody.touch();
            svc.journal.append("tool", "act:" + p.optString("op"), !out.has("__refusal"), out.optString("code"), actor,
                    out.optLong("ms"), p.optString("id"));
            out.remove("__refusal");
            return out;
        } finally {
            actLock.unlock();
        }
    }

    private JSONObject actLocked(JSONObject p, String actor) throws RpcError, org.json.JSONException {
        String op = p.optString("op", "");
        String id = p.optString("id", null);
        boolean confirm = p.optBoolean("confirm", false);

        if (op.equals("click") || op.equals("long_click")) {
            AccessibilityNodeInfo node = null;
            String name = null;
            Rect bounds = null;
            if (id != null) {
                node = svc.resolveSmart(id, p.optString("sig", null), p.optString("rid", null), p.optString("text", null));
                if (node == null) throw new RpcError("STALE_REF", "element is gone; observe again and use the fresh id", "retry", "observe");
                name = firstNonEmpty(AxService.chars(node.getText()), AxService.chars(node.getContentDescription()));
                bounds = new Rect();
                node.getBoundsInScreen(bounds);
            }
            // blind coordinate tap: no id â€” name the point from the fresh tree first
            if (node == null) {
                float x = (float) p.optDouble("x", -1), y = (float) p.optDouble("y", -1);
                if (x < 0 || y < 0) throw new RpcError("BAD_REQUEST", "click needs id or x/y");
                AccessibilityNodeInfo root = svc.getRootInActiveWindow();
                if (root != null) {
                    AccessibilityNodeInfo deepest = svc.deepestAt(root, x, y, new int[]{0}, 2200);
                    // only a *named* element counts (text/desc/rid); an unnamed root
                    // or container is exactly the blind tap the policy asks about
                    if (deepest != null && (AxService.chars(deepest.getText()) != null
                            || AxService.chars(deepest.getContentDescription()) != null
                            || deepest.getViewIdResourceName() != null)) {
                        name = firstNonEmpty(AxService.chars(deepest.getText()), AxService.chars(deepest.getContentDescription()));
                        node = deepest;
                    }
                }
                if (node == null && !confirm) {
                    JSONObject refusal = new JSONObject();
                    refusal.put("code", "NEEDS_CONFIRMATION");
                    refusal.put("kind", "blind_tap");
                    refusal.put("message", "no element can be named at (" + (int) x + "," + (int) y + ") â€” a blind tap. Ask the user, then repeat with confirm:true.");
                    throw refusalOf(refusal, "act", actor);
                }
            }
            // app allow/deny lists
            String pkg = svc.foregroundPackage();
            String denied = appPolicyRefusal(pkg);
            if (denied != null) throw new RpcError("BLOCKED_BY_POLICY", denied);
            // guard: classify by label â€” decided on the device for every transport
            JSONObject refusal = Guard.evaluate(svc.policy, name, pkg, guardRole(node), confirm);
            if (refusal != null) throw refusalOf(refusal, "act", actor);

            // semantic first: ACTION_CLICK / ACTION_LONG_CLICK on the node or
            // its nearest clickable ancestor (exactly what a tap would hit)
            AccessibilityNodeInfo clickTarget = node == null ? null
                    : (node.isClickable() ? node : AxService.actionableAncestor(node, 12));
            if (clickTarget != null) {
                int action = op.equals("long_click")
                        ? AccessibilityNodeInfo.ACTION_LONG_CLICK : AccessibilityNodeInfo.ACTION_CLICK;
                boolean ok = clickTarget.performAction(action);
                if (!ok) {
                    // a view mid-animation (a collapsing mode list) rejects the
                    // click; one retry after it settles beats a gesture that the
                    // occlusion check refuses (CameraTakePhoto)
                    sleep(350);
                    try {
                        clickTarget.refresh();
                    } catch (Exception ignored) {
                    }
                    ok = clickTarget.performAction(action);
                }
                if (ok) {
                    return new JSONObject().put("ok", true)
                            .put("via", clickTarget == node ? "ax-click" : "ax-click-ancestor");
                }
            }
            // gesture fallback at the visible centre, verified against the fresh tree
            float cx, cy;
            if (bounds == null || bounds.isEmpty()) {
                if (node != null) {
                    bounds = new Rect();
                    node.getBoundsInScreen(bounds);
                }
            }
            if (bounds == null || bounds.isEmpty()) {
                cx = (float) p.optDouble("x", -1);
                cy = (float) p.optDouble("y", -1);
                if (cx < 0 || cy < 0) throw new RpcError("NO_BOUNDS", "element has no on-screen bounds to tap");
            } else {
                cx = bounds.centerX();
                cy = bounds.centerY();
                if (node != null) {
                    JSONObject occ = svc.verifyPoint(cx, cy, node);
                    if (occ != null) throw refusalOf(occ, "act", actor);
                }
            }
            long dur = op.equals("long_click") ? 650 : 60;
            List<float[]> pts = new ArrayList<float[]>();
            pts.add(new float[]{cx, cy});
            pts.add(new float[]{cx, cy});
            boolean done = svc.dispatchStroke(pts, dur, 3000);
            if (!done) throw new RpcError("ACTION_FAILED", "gesture was cancelled (another gesture in flight, or the display is off)");
            return new JSONObject().put("ok", true).put("via", "gesture");
        }

        if (op.equals("set_text")) {
            AccessibilityNodeInfo node = requireEditable(p);
            String text = p.optString("text", "");
            boolean ok = node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT,
                    bundle(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text));
            if (!ok) throw new RpcError("ACTION_FAILED", "ACTION_SET_TEXT returned false (field may be read-only)");
            return new JSONObject().put("ok", true).put("via", "ax-set-text");
        }

        if (op.equals("clear_text")) {
            AccessibilityNodeInfo node = requireEditable(p);
            boolean ok = node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT,
                    bundle(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, ""));
            if (!ok) throw new RpcError("ACTION_FAILED", "ACTION_SET_TEXT returned false");
            return new JSONObject().put("ok", true).put("via", "ax-set-text");
        }

        if (op.equals("ime_submit")) {
            AccessibilityNodeInfo node = svc.resolve(id);
            if (node == null) throw new RpcError("STALE_REF", "element is gone; observe again", "retry", "observe");
            return imeSubmit(node);
        }

        if (op.equals("scroll_fwd") || op.equals("scroll_back")) {
            AccessibilityNodeInfo node = svc.resolve(id);
            if (node == null) throw new RpcError("STALE_REF", "element is gone; observe again", "retry", "observe");
            AccessibilityNodeInfo scroller = node.isScrollable() ? node : AxService.scrollableAncestor(node, 12);
            if (scroller != null) {
                boolean ok = scroller.performAction(op.equals("scroll_fwd")
                        ? AccessibilityNodeInfo.ACTION_SCROLL_FORWARD : AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD);
                if (ok) return new JSONObject().put("ok", true).put("via", "ax-scroll");
            }
            // gesture fling inside the scroller's bounds
            Rect b = new Rect();
            scroller = scroller != null ? scroller : node;
            scroller.getBoundsInScreen(b);
            if (b.isEmpty()) throw new RpcError("NO_BOUNDS", "no bounds to fling");
            boolean forward = op.equals("scroll_fwd");
            float cx = b.centerX();
            float y1 = forward ? b.top + b.height() * 0.7f : b.top + b.height() * 0.3f;
            float y2 = forward ? b.top + b.height() * 0.3f : b.top + b.height() * 0.7f;
            List<float[]> pts = new ArrayList<float[]>();
            pts.add(new float[]{cx, y1});
            pts.add(new float[]{cx, y2});
            boolean done = svc.dispatchStroke(pts, 260, 4000);
            if (!done) throw new RpcError("ACTION_FAILED", "scroll gesture cancelled");
            return new JSONObject().put("ok", true).put("via", "gesture-fling");
        }

        if (op.equals("swipe") || op.equals("gesture")) {
            if (op.equals("swipe")) {
                float x = (float) p.optDouble("x"), y = (float) p.optDouble("y");
                float dx = (float) p.optDouble("dx"), dy = (float) p.optDouble("dy");
                long dur = (long) p.optDouble("duration", 300);
                List<float[]> pts = new ArrayList<float[]>();
                pts.add(new float[]{x, y});
                pts.add(new float[]{x + dx, y + dy});
                boolean done = svc.dispatchStroke(pts, dur, dur + 3000);
                if (!done) throw new RpcError("ACTION_FAILED", "swipe gesture cancelled");
                return new JSONObject().put("ok", true).put("via", "gesture");
            }
            JSONArray raw = p.optJSONArray("points");
            long dur = (long) p.optDouble("duration", 300);
            if (raw == null || raw.length() < 2) throw new RpcError("BAD_REQUEST", "gesture needs points[[x,y],â€¦]");
            List<float[]> pts = new ArrayList<float[]>();
            for (int i = 0; i < raw.length(); i++) {
                JSONArray pt = raw.optJSONArray(i);
                if (pt == null || pt.length() < 2) throw new RpcError("BAD_REQUEST", "points must be [x,y] pairs");
                pts.add(new float[]{(float) pt.optDouble(0), (float) pt.optDouble(1)});
            }
            boolean done = svc.dispatchStroke(pts, dur, dur + 4000);
            if (!done) throw new RpcError("ACTION_FAILED", "gesture cancelled");
            return new JSONObject().put("ok", true).put("via", "gesture");
        }

        if (op.equals("global")) {
            svc.globalAction(p.optString("name", ""));
            return new JSONObject().put("ok", true).put("via", "global");
        }

        throw new RpcError("BAD_REQUEST", "unknown act op '" + op + "'");
    }

    /** type: focus â†’ set (append if asked) â†’ verify â†’ optional submit. */
    private JSONObject type(JSONObject p, String actor) throws RpcError, org.json.JSONException {
        actLock.lock();
        long t0 = SystemClock.elapsedRealtime();
        try {
            String id = p.optString("id", null);
            String text = p.optString("text", "");
            boolean replace = p.optBoolean("replace", true);
            boolean submit = p.optBoolean("submit", false);
            AccessibilityNodeInfo node = svc.resolve(id);
            if (node == null) throw new RpcError("STALE_REF", "field is gone; observe again", "retry", "observe");
            if (!node.isEditable()) {
                AccessibilityNodeInfo editable = null;
                // a labelled cell wrapping the field: look one level down
                for (int i = 0; i < node.getChildCount() && i < TreeDump.MAX_CHILDREN; i++) {
                    AccessibilityNodeInfo c = node.getChild(i);
                    if (c != null && c.isEditable()) {
                        editable = c;
                        break;
                    }
                }
                if (editable == null) throw new RpcError("NOT_EDITABLE",
                        "element is not a text field (no editable node here)");
                node = editable;
            }
            // a read-only field that opens a picker when tapped (date/time):
            // ACTION_SET_TEXT changes what is displayed but never the value the
            // app uses, and the read-back "verified" it
            if (!node.isFocusable() && node.isClickable()
                    && String.valueOf(node.getClassName()).endsWith("EditText")) {
                throw new RpcError("NOT_TYPEABLE", "this field is read-only: its value is chosen in a picker "
                        + "that opens when you tap it (typing only changes the displayed text, not the value the app uses)");
            }
            node.performAction(AccessibilityNodeInfo.ACTION_FOCUS);
            String existing = node.getText() == null ? null : String.valueOf(node.getText());   // exact: prepend/append must not touch the content
            if (android.os.Build.VERSION.SDK_INT >= 26 && node.isShowingHintText()) existing = null;
            // at:"start" inserts before the existing value (prepend a line
            // without re-typing — and possibly corrupting — the content)
            boolean atStart = "start".equals(p.optString("at", ""));
            boolean keep = (!replace || atStart) && existing != null && !existing.isEmpty();
            String expected = keep ? (atStart ? text + existing : existing + text) : text;
            // A replace that re-types the shown value after a new beginning
            // ("HDR\n\n" + value) is a prepend. The displayed value is trimmed,
            // so re-typing it silently drops the whitespace the field really
            // holds (a note's trailing newline): keep the field's own text.
            if (replace && !atStart && existing != null && !existing.isEmpty()) {
                String shown = existing.trim();
                if (!shown.isEmpty() && !existing.equals(shown) && text.length() > shown.length()
                        && text.endsWith(shown)) {
                    expected = text.substring(0, text.length() - shown.length()) + existing;
                }
            }
            boolean ok = node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT,
                    bundle(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, expected));
            if (!ok) throw new RpcError("ACTION_FAILED", "ACTION_SET_TEXT returned false");
            sleep(180);   // let the field settle before the read-back (measured: immediate reads race)
            JSONObject out = new JSONObject();
            if (node.isPassword()) {
                out.put("ok", true).put("via", "ax-set-text").put("verified", false)
                        .put("note", "secure field: value not read back");
            } else {
                // web fields (Chrome) publish the new value asynchronously and
                // as "<value>, <placeholder>": poll a refreshed node briefly
                String want = expected.replaceAll("\\s+", " ").trim();
                String actual = null;
                for (int attempt = 0; attempt < 6; attempt++) {
                    if (attempt > 0) sleep(150);
                    AccessibilityNodeInfo fresh = svc.resolve(id);
                    if (fresh == null) break;
                    try {
                        fresh.refresh();
                    } catch (Exception ignored) {
                    }
                    actual = AxService.rawText(fresh.getText());
                    if (actual != null && android.os.Build.VERSION.SDK_INT >= 26 && fresh.isShowingHintText()) actual = "";
                    String got = actual == null ? "" : actual.replaceAll("\\s+", " ").trim();
                    if (got.equals(want) || got.startsWith(want + ", ")) {
                        actual = expected;
                        break;
                    }
                }
                if (actual != null && !actual.replaceAll("\\s+", " ").trim().equals(want)) {
                    // the focus/IME open may re-layout paths or change the active
                    // window; look for any node that now holds the text
                    AccessibilityNodeInfo holder = findNodeByTextAnywhere(expected);
                    if (holder == null) {
                        throw new RpcError("TEXT_VERIFY_FAILED", "field reads '" + actual + "' after typing");
                    }
                    out.put("ok", true).put("via", "ax-set-text").put("value", expected).put("verified", true)
                            .put("note", "field moved after the IME opened; verified by value");
                } else {
                    out.put("ok", true).put("via", "ax-set-text").put("value", actual).put("verified", true);
                }
            }
            if (submit) {
                JSONObject sub = imeSubmit(node);
                out.put("submit_via", sub.optString("via"));
            }
            out.put("ms", SystemClock.elapsedRealtime() - t0);
            svc.custody.touch();
            svc.journal.append("tool", "type", true, null, actor, out.optLong("ms"), id);
            return out;
        } finally {
            actLock.unlock();
        }
    }

    private JSONObject imeSubmit(AccessibilityNodeInfo node) throws RpcError, org.json.JSONException {
        // API 34+: the platform action. Older: click the IME window's action button.
        try {
            if (android.os.Build.VERSION.SDK_INT >= 34) {
                int imeEnter = AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.getId();
                if (node.performAction(imeEnter)) {
                    return new JSONObject().put("ok", true).put("via", "ax-ime-enter");
                }
            }
        } catch (Throwable ignored) {
        }
        AccessibilityNodeInfo root = svc.getRootInActiveWindow();
        String[] labels = {"search", "go", "send", "enter", "next", "done", "suchen", "æœç´¢", "æ¤œç´¢", "é€ä¿¡"};
        AccessibilityNodeInfo imeBtn = root == null ? null : findByLabels(root, labels, new int[]{0});
        if (imeBtn != null) {
            AccessibilityNodeInfo target = imeBtn.isClickable() ? imeBtn : AxService.actionableAncestor(imeBtn, 6);
            if (target != null && target.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                return new JSONObject().put("ok", true).put("via", "ime-button");
            }
        }
        throw new RpcError("SUBMIT_FAILED", "no IME enter action available (API < 34 and no action button visible)");
    }

    /** Any node whose text equals the needle, across every on-screen window. */
    private AccessibilityNodeInfo findNodeByTextAnywhere(String needle) {
        try {
            java.util.List<android.view.accessibility.AccessibilityWindowInfo> ws = svc.getWindows();
            if (ws != null) {
                for (android.view.accessibility.AccessibilityWindowInfo w : ws) {
                    AccessibilityNodeInfo root = w.getRoot();
                    if (root == null) continue;
                    AccessibilityNodeInfo hit = findByText(root, needle, new int[]{0});
                    if (hit != null) return hit;
                }
            }
        } catch (Exception ignored) {
        }
        return null;
    }

    /** Any node whose text equals the needle (used by the text read-back). */
    private AccessibilityNodeInfo findByText(AccessibilityNodeInfo node, String needle, int[] budget) {
        if (budget[0]++ > 400 || node == null) return null;
        String t = AxService.chars(node.getText());
        if (t != null && t.replaceAll("\\s+", " ").trim().equals(needle.replaceAll("\\s+", " ").trim())) return node;
        for (int i = 0; i < node.getChildCount() && i < TreeDump.MAX_CHILDREN; i++) {
            AccessibilityNodeInfo c = node.getChild(i);
            if (c == null) continue;
            AccessibilityNodeInfo hit = findByText(c, needle, budget);
            if (hit != null) return hit;
        }
        return null;
    }

    private AccessibilityNodeInfo findByLabels(AccessibilityNodeInfo node, String[] labels, int[] budget) {        if (budget[0]++ > 400 || node == null) return null;
        String t = AxService.chars(node.getText());
        String d = AxService.chars(node.getContentDescription());
        for (String label : labels) {
            if ((t != null && t.toLowerCase().contains(label)) || (d != null && d.toLowerCase().contains(label))) {
                if (node.isClickable() || AxService.actionableAncestor(node, 4) != null) return node;
            }
        }
        for (int i = 0; i < node.getChildCount() && i < TreeDump.MAX_CHILDREN; i++) {
            AccessibilityNodeInfo c = node.getChild(i);
            if (c == null) continue;
            AccessibilityNodeInfo hit = findByLabels(c, labels, budget);
            if (hit != null) return hit;
        }
        return null;
    }

    private AccessibilityNodeInfo requireEditable(JSONObject p) throws RpcError {
        String id = p.optString("id", null);
        AccessibilityNodeInfo node = svc.resolve(id);
        if (node == null) throw new RpcError("STALE_REF", "field is gone; observe again", "retry", "observe");
        if (!node.isEditable()) throw new RpcError("NOT_EDITABLE", "element is not an editable field");
        return node;
    }

    private String appPolicyRefusal(String pkg) {
        Set<String> deny = svc.policy.denyApps();
        if (pkg != null && deny.contains(pkg)) return "app " + pkg + " is on the operator's deny list";
        Set<String> allow = svc.policy.allowApps();
        if (allow != null && !allow.isEmpty() && pkg != null && !allow.contains(pkg)
                && !"com.android.systemui".equals(pkg)) {
            return "app " + pkg + " is not on the operator's allow list";
        }
        return null;
    }

    private RpcError refusalOf(JSONObject refusal, String tool, String actor) {
        svc.journal.append("refused", tool, false, refusal.optString("code"), actor, 0, refusal.optString("element"));
        RpcError err = new RpcError(refusal.optString("code"), refusal.optString("message"));
        try {
            JSONObject facts = new JSONObject();
            facts.put("kind", refusal.optString("kind"));
            facts.put("element", refusal.optString("element"));
            if (refusal.has("coverer")) facts.put("coverer", refusal.getJSONObject("coverer"));
            err.facts.put("facts", facts);
        } catch (Exception ignored) {
        }
        return err;
    }

    // â”€â”€ waits (device-side, event-driven) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    private JSONObject wait(JSONObject p) throws RpcError, org.json.JSONException {
        String text = p.optString("text", null);
        boolean gone = p.optBoolean("gone", false);
        double timeout = p.optDouble("timeout", 8.0);
        double stable = p.optDouble("stable", 0.0);
        long deadline = SystemClock.elapsedRealtime() + (long) (Math.min(25.0, timeout) * 1000);
        long t0 = SystemClock.elapsedRealtime();

        if (text == null && stable <= 0) throw new RpcError("BAD_REQUEST", "wait needs text or stable");

        while (SystemClock.elapsedRealtime() < deadline) {
            if (text != null) {
                if (containsText(text) == !gone) {
                    return new JSONObject().put("ok", true)
                            .put("matched", true)
                            .put("elapsed_ms", SystemClock.elapsedRealtime() - t0)
                            .put("changes", svc.changeCount.get());
                }
            }
            if (stable > 0) {
                // quiet for stable seconds AND the text condition holds now
                if (quietMs() >= stable * 1000 && (text == null || containsText(text) == !gone)) {
                    return new JSONObject().put("ok", true)
                            .put("stable", true)
                            .put("elapsed_ms", SystemClock.elapsedRealtime() - t0)
                            .put("changes", svc.changeCount.get());
                }
            }
            sleep(120);
        }
        JSONObject o = new JSONObject();
        o.put("ok", false);
        o.put("timeout", true);
        o.put("elapsed_ms", SystemClock.elapsedRealtime() - t0);
        o.put("changes", svc.changeCount.get());
        return o;
    }

    /** True if any visible node's text/desc contains the needle (cached per change count). */
    private boolean containsText(String needle) {
        JSONObject dump;
        try {
            if (waitLastNodes == null || waitLastDumpChange != svc.changeCount.get()) {
                try {
                    dump = svc.observe(1200);
                    waitLastNodes = dump.optJSONArray("nodes");
                    waitLastDumpChange = svc.changeCount.get();
                } catch (org.json.JSONException je) {
                    return false;
                }
            }
        } catch (RpcError e) {
            return false;
        }
        JSONArray nodes = waitLastNodes;
        if (nodes == null) return false;
        String low = fold(needle.toLowerCase());
        for (int i = 0; i < nodes.length(); i++) {
            JSONObject n = nodes.optJSONObject(i);
            if (n == null || (n.optInt("f", 0) & 1024) == 0) continue;
            String t = fold(n.optString("text", "").toLowerCase());
            String d = fold(n.optString("desc", "").toLowerCase());
            if (t.contains(low) || d.contains(low)) return true;
        }
        return false;
    }

    /** Unicode hyphens/quotes fold to ASCII: 'Wi‑Fi' matches 'Wi-Fi'. */
    private static String fold(String s) {
        return s == null ? "" : s.replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-")
                .replace("\u2019", "'").replace("\u2018", "'");
    }

    // â”€â”€ launches, URIs, clipboard â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    private JSONObject launch(JSONObject p, String actor) throws RpcError, org.json.JSONException {
        String pkg = p.optString("pkg", null);
        if (pkg == null || pkg.isEmpty()) throw new RpcError("BAD_REQUEST", "launch needs pkg");
        if (pkg.startsWith("http") || pkg.contains("://")) throw new RpcError("BAD_REQUEST", "launch takes a package name; use open_uri for URLs");
        boolean fresh = p.optBoolean("fresh", false);
        if (fresh) {
            try {
                android.app.ActivityManager am = (android.app.ActivityManager) svc.getSystemService(Context.ACTIVITY_SERVICE);
                if (am != null) am.killBackgroundProcesses(pkg);
            } catch (Exception ignored) {
            }
            sleep(350);
        }
        Intent intent = svc.getPackageManager().getLaunchIntentForPackage(pkg);
        if (intent == null) throw new RpcError("APP_NOT_FOUND", "no launchable app '" + pkg + "'");
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        // a foreground app survives killBackgroundProcesses and NEW_TASK alone
        // resurfaces its task on whatever screen it was left on: fresh must
        // restart the task at the app's main screen
        if (fresh) intent.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TASK);
        long t0 = SystemClock.elapsedRealtime();
        try {
            svc.startActivity(intent);
        } catch (Exception e) {
            throw new RpcError("LAUNCH_FAILED", String.valueOf(e.getMessage()));
        }
        svc.custody.touch();
        svc.journal.append("tool", "launch", true, null, actor, SystemClock.elapsedRealtime() - t0, pkg);
        return new JSONObject().put("ok", true).put("pkg", pkg)
                .put("ms", SystemClock.elapsedRealtime() - t0)
                .put("note", fresh ? "fresh: task restarted at the main screen (process not force-stopped)" : null);
    }

    private JSONObject openUri(JSONObject p) throws RpcError, org.json.JSONException {
        String uri = p.optString("uri", null);
        if (uri == null || uri.isEmpty()) throw new RpcError("BAD_REQUEST", "open_uri needs uri");
        String scheme = Uri.parse(uri).getScheme();
        if (scheme == null || !(scheme.equals("http") || scheme.equals("https"))) {
            throw new RpcError("UNSAFE_URI", "only http/https URIs are opened (deep links and app schemes are refused)");
        }
        Intent intent = new Intent(Intent.ACTION_VIEW, Uri.parse(uri)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        try {
            svc.startActivity(intent);
        } catch (Exception e) {
            throw new RpcError("NO_HANDLER", String.valueOf(e.getMessage()));
        }
        svc.custody.touch();
        return new JSONObject().put("ok", true);
    }

    private JSONObject clipboard(JSONObject p) throws RpcError, org.json.JSONException {
        ClipboardManager cm = (ClipboardManager) svc.getSystemService(Context.CLIPBOARD_SERVICE);
        if (cm == null) throw new RpcError("CLIPBOARD_DENIED", "no clipboard service");
        if (p.optString("op", "get").equals("set")) {
            cm.setPrimaryClip(ClipData.newPlainText("axdroid", p.optString("text", "")));
            return new JSONObject().put("ok", true);
        }
        ClipData cd = cm.getPrimaryClip();
        if (cd == null || cd.getItemCount() == 0) return new JSONObject().put("ok", true).put("text", null);
        CharSequence t = cd.getItemAt(0).coerceToText(svc);
        return new JSONObject().put("ok", true).put("text", t == null ? null : String.valueOf(t));
    }

    // ── adapters for the on-device MCP engine (Mcp.java) ──────────────

    JSONObject changesInfo() throws org.json.JSONException {
        return new JSONObject().put("count", svc.changeCount.get()).put("quiet_ms", svc.quietMs());
    }

    JSONObject callAct(String op, String id, boolean confirm, String sig, String rid, String text)
            throws RpcError, org.json.JSONException {
        return dispatch("act", new JSONObject().put("op", op).put("id", id).put("confirm", confirm)
                        .put("sig", sig).put("rid", rid).put("text", text),
                "on-device", true);
    }

    JSONObject callActAt(String op, float x, float y, boolean confirm)
            throws RpcError, org.json.JSONException {
        return dispatch("act", new JSONObject().put("op", op).put("x", (double) x).put("y", (double) y)
                .put("confirm", confirm), "on-device", true);
    }

    /** one semantic scroll inside the given scroller (or gesture fling) */
    void dispatchScroll(boolean forward, int swipes, String scrollerId) throws RpcError, org.json.JSONException {
        for (int i = 0; i < swipes; i++) {
            JSONObject p = new JSONObject().put("op", forward ? "scroll_fwd" : "scroll_back");
            if (scrollerId != null) p.put("id", scrollerId);
            dispatch("act", p, "on-device", true);
        }
    }

    /** a real gesture fling over the largest visible list */
    void flingList(boolean down) throws RpcError, org.json.JSONException {
        JSONObject dump = svc.observe(600);
        JSONArray nodes = dump.optJSONArray("nodes");
        JSONObject best = null;
        long bestArea = 0;
        for (int i = 0; nodes != null && i < nodes.length(); i++) {
            JSONObject n = nodes.optJSONObject(i);
            if (!"scroll".equals(Mcp.role(n)) && !"list".equals(Mcp.role(n))) continue;
            if ((n.optInt("f", 0) & 1024) == 0 || n.optJSONArray("b") == null) continue;
            JSONArray b = n.getJSONArray("b");
            long area = (long) (b.optInt(2) - b.optInt(0)) * (b.optInt(3) - b.optInt(1));
            if (area > bestArea) {
                bestArea = area;
                best = n;
            }
        }
        float x = 540, y1, y2;
        JSONObject display = svc.deviceInfo().optJSONObject("display");
        int h = display != null ? display.optInt("h") : 2340;
        if (best != null) {
            JSONArray b = best.getJSONArray("b");
            x = (b.optInt(0) + b.optInt(2)) / 2f;
            int top = b.optInt(1), bot = b.optInt(3);
            y1 = down ? top + (bot - top) * 0.72f : top + (bot - top) * 0.28f;
            y2 = down ? top + (bot - top) * 0.28f : top + (bot - top) * 0.72f;
        } else {
            y1 = down ? h * 0.72f : h * 0.28f;
            y2 = down ? h * 0.28f : h * 0.72f;
        }
        java.util.List<float[]> pts = new java.util.ArrayList<>();
        pts.add(new float[]{x, y1});
        pts.add(new float[]{x, y2});
        svc.dispatchStroke(pts, 280, 4000);
    }

    JSONObject callType(String id, String text, boolean replace, boolean submit, String at)
            throws RpcError, org.json.JSONException {
        JSONObject p = new JSONObject().put("text", text).put("replace", replace).put("submit", submit);
        if (at != null && !at.isEmpty()) p.put("at", at);
        if (id != null) p.put("id", id);
        return dispatch("type", p, "on-device", true);
    }

    String resolveApp(String app) throws RpcError, org.json.JSONException {
        String low = app.toLowerCase(java.util.Locale.US);
        // well-known aliases first: common and benchmark names that don't
        // match the launcher label (e.g. 'simple sms messenger' whose label
        // is just 'SMS Messenger')
        String aliased = ALIASES.get(low);
        if (aliased != null && svc.getPackageManager().getLaunchIntentForPackage(aliased) != null)
            return aliased;
        JSONArray apps = appsList();
        // an alias names the AOSP package; vendors ship the same app under
        // their own prefix (com.google.android.documentsui on Pixel builds)
        if (aliased != null) {
            // last two segments ("android.documentsui"): one segment
            // (".plus") would match unrelated apps
            int dot = aliased.lastIndexOf('.', aliased.lastIndexOf('.') - 1);
            String tail = dot < 0 ? aliased : aliased.substring(dot);
            for (int i = 0; i < apps.length(); i++) {
                String pkg = apps.optJSONObject(i).optString("pkg");
                if (pkg.endsWith(tail)) return pkg;
            }
        }
        // exact/substring match on name or package, direct package accepted
        for (int i = 0; i < apps.length(); i++) {
            JSONObject a = apps.optJSONObject(i);
            String name = a.optString("name").toLowerCase(java.util.Locale.US);
            String pkg = a.optString("pkg").toLowerCase(java.util.Locale.US);
            if (name.equals(low) || pkg.equals(low)) return a.optString("pkg");
        }
        for (int i = 0; i < apps.length(); i++) {
            JSONObject a = apps.optJSONObject(i);
            String name = a.optString("name").toLowerCase(java.util.Locale.US);
            String pkg = a.optString("pkg").toLowerCase(java.util.Locale.US);
            if (name.contains(low) || pkg.contains(low)) return a.optString("pkg");
        }
        if (low.contains(".")) return low;   // caller passed a package we cannot enumerate
        throw new RpcError("APP_NOT_FOUND", "no installed app matches '" + app + "'");
    }

    JSONObject callLaunch(String pkg, boolean fresh) throws RpcError, org.json.JSONException {
        return dispatch("launch", new JSONObject().put("pkg", pkg).put("fresh", fresh), "on-device", true);
    }

    JSONArray appsList() throws RpcError, org.json.JSONException {
        return dispatch("apps", new JSONObject(), "on-device", true).optJSONArray("apps");
    }

    JSONObject callWait(String text, boolean gone, double timeout, double stable)
            throws RpcError, org.json.JSONException {
        return dispatch("wait", new JSONObject().put("text", text).put("gone", gone)
                .put("timeout", timeout).put("stable", stable), "on-device", true);
    }

    boolean stableWait(double stableSec, double timeoutSec) {
        try {
            JSONObject r = dispatch("wait", new JSONObject().put("stable", stableSec)
                    .put("timeout", timeoutSec), "on-device", true);
            return r.optBoolean("ok");
        } catch (Exception e) {
            return false;
        }
    }

    JSONObject statusJson() throws RpcError, org.json.JSONException {
        return dispatch("status", new JSONObject(), "on-device", true);
    }

    private static Bundle bundle(String key, CharSequence value) {
        Bundle b = new Bundle();
        b.putCharSequence(key, value);
        return b;
    }

    private static String firstNonEmpty(String a, String b) {
        return a != null && !a.isEmpty() ? a : (b != null && !b.isEmpty() ? b : null);
    }

    /** Coarse role for the guard only: the host derives proper roles from the dump. */
    private static String guardRole(AccessibilityNodeInfo node) {
        if (node == null) return "button";
        String cls = node.getClassName() == null ? "" : String.valueOf(node.getClassName());
        if (cls.contains("Switch") || cls.contains("CheckBox") || cls.contains("Toggle")) return "switch";
        if (cls.contains("MenuItem") || cls.contains("ListItem")) return "cell";
        if (cls.contains("EditText")) return "textfield";
        return "button";
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}

