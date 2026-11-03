package com.axuse.axdroid;

import android.graphics.Rect;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/**
 * Snapshot of every on-screen window's accessibility tree.
 *
 * Node identity is "w<windowId>:<i>/<j>/<k>" â€” the child-index path from the
 * window root. Ids are opaque to the host; they resolve back to live nodes
 * exactly (one binder call per path step) and go stale safely: acting on a
 * stale id fails with STALE_REF and the host re-observes once.
 *
 * Windows come from getWindows() (topmost-first, overlays included â€” alerts,
 * keyboards); the active window's root is read separately and marked, so the
 * host renders what is on top first, like axphone's overlay-first order.
 */
final class TreeDump {
    static final int MAX_CHILDREN = 140;
    static final int MAX_DEPTH = 44;

    static JSONObject snapshot(AxService svc, int maxNodes) throws RpcError, org.json.JSONException {
        long t0 = android.os.SystemClock.elapsedRealtime();
        JSONObject out = new JSONObject();
        JSONArray windows = new JSONArray();
        JSONArray nodes = new JSONArray();
        int[] budget = new int[]{maxNodes};
        int state = ++stateSeq;

        List<Object[]> wins = new ArrayList<Object[]>(); // {AccessibilityWindowInfo, id}
        AccessibilityNodeInfo activeRoot = null;
        try {
            activeRoot = svc.getRootInActiveWindow();
        } catch (Exception ignored) {
        }

        try {
            List<AccessibilityWindowInfo> list = svc.getWindows();
            int activeWinId = activeRoot != null ? activeRoot.getWindowId() : -1;
            // active window first if getWindows() doesn't already include it
            boolean sawActive = false;
            if (list != null) {
                for (AccessibilityWindowInfo w : list) {
                    if (w.getId() == activeWinId) sawActive = true;
                }
            }
            if (activeRoot != null && !sawActive && activeWinId != -1) {
                wins.add(new Object[]{null, activeWinId, activeRoot, true});
            }
            if (list != null) {
                for (AccessibilityWindowInfo w : list) {
                    wins.add(new Object[]{w, w.getId(), null, w.getId() == activeWinId});
                }
            }
        } catch (Exception e) {
            // fall through: windows list may fail while content still works
        }
        if (wins.isEmpty() && activeRoot == null) {
            throw new RpcError("EMPTY_TREE", "no accessibility content on screen (service not enabled, or an empty/lock screen)");
        }

        for (Object[] wmeta : wins) {
            AccessibilityWindowInfo w = (AccessibilityWindowInfo) wmeta[0];
            int winId = (Integer) wmeta[1];
            AccessibilityNodeInfo root = (AccessibilityNodeInfo) wmeta[2];
            boolean isActive = (Boolean) wmeta[3];
            String pkg = null;
            if (root == null && w != null) root = w.getRoot();
            if (root != null && root.getPackageName() != null) pkg = String.valueOf(root.getPackageName());
            JSONObject wm = new JSONObject();
            wm.put("id", winId);
            wm.put("pkg", pkg);
            wm.put("title", w != null && w.getTitle() != null ? String.valueOf(w.getTitle()) : null);
            wm.put("active", isActive);
            if (w != null) {
                wm.put("ime", w.getType() == AccessibilityWindowInfo.TYPE_INPUT_METHOD);
                wm.put("overlay", w.getType() == AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY
                        || w.getType() == AccessibilityWindowInfo.TYPE_SYSTEM);
                Rect b = new Rect();
                w.getBoundsInScreen(b);
                wm.put("bounds", new JSONArray(new int[]{b.left, b.top, b.right, b.bottom}));
            }
            windows.put(wm);
            if (root == null) continue;
            walk(svc, root, winId, "", 0, nodes, budget);
            root.recycle();
        }

        out.put("state", state);
        out.put("windows", windows);
        out.put("nodes", nodes);
        out.put("count", nodes.length());
        out.put("truncated", budget[0] <= 0);
        out.put("changes", svc.changeCount.get());
        out.put("ms", android.os.SystemClock.elapsedRealtime() - t0);
        return out;
    }

    private static void walk(AxService svc, AccessibilityNodeInfo node, int winId, String path,
                             int depth, JSONArray out, int[] budget) throws org.json.JSONException {
        if (budget[0] <= 0 || depth > MAX_DEPTH || node == null) return;
        budget[0]--;

        JSONObject o = new JSONObject();
        String id = "w" + winId + ":" + path;
        o.put("id", id);
        o.put("d", depth);
        CharSequence cls = node.getClassName();
        if (cls != null) o.put("cls", String.valueOf(cls));
        String text = AxService.rawText(node.getText());
        // an EMPTY field reports its hint as text (API 26+); agents then
        // believe a default value is there (Markor's ".md" extension field)
        if (android.os.Build.VERSION.SDK_INT >= 26 && node.isShowingHintText()) text = null;
        if (text != null) o.put("text", text);
        String desc = AxService.rawText(node.getContentDescription());
        if (desc != null) o.put("desc", desc);
        String rid = node.getViewIdResourceName();
        if (rid != null) o.put("rid", rid);
        Rect r = new Rect();
        node.getBoundsInScreen(r);
        if (!r.isEmpty()) o.put("b", new JSONArray(new int[]{r.left, r.top, r.right, r.bottom}));

        int f = 0;
        if (node.isClickable()) f |= 1;
        if (node.isLongClickable()) f |= 2;
        if (node.isScrollable()) f |= 4;
        if (node.isEditable()) f |= 8;
        if (node.isCheckable()) f |= 16;
        if (node.isChecked()) f |= 32;
        if (node.isEnabled()) f |= 64;
        if (node.isSelected()) f |= 128;
        if (node.isFocusable()) f |= 256;
        if (node.isFocused()) f |= 512;
        if (node.isVisibleToUser()) f |= 1024;
        if (node.isPassword()) f |= 2048;
        // a list shows only the rows on screen: say whether it can still be
        // scrolled, or an agent reads the visible rows as the whole list
        if (node.isScrollable()) {
            for (AccessibilityNodeInfo.AccessibilityAction a : node.getActionList()) {
                int aid = a.getId();
                if (aid == AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
                        || aid == AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_DOWN.getId()) f |= 4096;
                if (aid == AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
                        || aid == AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_UP.getId()) f |= 8192;
            }
        }
        o.put("f", f);
        String value = AxService.chars(node.getText()) == null && node.isCheckable() ? (node.isChecked() ? "1" : "0") : null;
        if (value != null) o.put("value", value);
        // field hints (placeholders): Android exposes them separately from
        // text since API 26 — form validators key on them
        if (android.os.Build.VERSION.SDK_INT >= 26) {
            CharSequence hint = null;
            try { hint = node.getHintText(); } catch (Throwable ignored) { }
            if (hint != null) o.put("hint", String.valueOf(hint));
        }
        // sliders / seek bars / progress: without the range an agent cannot
        // read a brightness or playback position, nor verify that it moved
        AccessibilityNodeInfo.RangeInfo ri = node.getRangeInfo();
        if (ri != null) {
            o.put("range", new JSONArray(new double[]{ri.getCurrent(), ri.getMin(), ri.getMax()}));
        }
        o.put("sig", sig(o));
        out.put(o);

        int n = Math.min(node.getChildCount(), MAX_CHILDREN);
        for (int i = 0; i < n; i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child == null) continue;
            walk(svc, child, winId, path.isEmpty() ? String.valueOf(i) : path + "/" + i, depth + 1, out, budget);
            child.recycle();
        }
    }

    /** Content identity: role-class + resource id + text/desc + checked.
     *  Hashed over chars()-normalized text, exactly like AxService.nodeSig
     *  (the dump keeps raw multi-line text; the live-tree lookup must match). */
    private static String sig(JSONObject o) throws org.json.JSONException {
        String t = AxService.chars(o.optString("text", ""));
        String d = AxService.chars(o.optString("desc", ""));
        String s = o.optString("cls", "") + "|" + o.optString("rid", "") + "|"
                + (t == null ? "" : t) + "|" + (d == null ? "" : d) + "|" + (o.optInt("f", 0) & (16 | 32));
        return Integer.toHexString(s.hashCode());
    }

    private static int stateSeq = 0;
}

