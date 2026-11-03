package com.axuse.axdroid;

import android.graphics.Rect;
import android.view.accessibility.AccessibilityNodeInfo;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Iterator;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/**
 * The on-device MCP engine: the phone answers model-driven tool calls itself,
 * with the same agent-facing format as the host engine (render/diff/refs) —
 * zero host software over LAN, and the surface the relay tunnel speaks.
 *
 * Ports of the proven host logic (single behaviour, two implementations that
 * must not diverge; parity is checked on-device by tools/check-ondevice-mcp.py):
 *   - roles by class, then flags (model.infer_role)
 *   - compact render: chrome hidden, package-stripped ids, ×N compaction
 *   - refs that survive re-binding (registry with identity inheritance)
 *   - id-keyed diff with signature fallback
 *   - ranked text resolve (exact > prefix > substring, foreground window wins)
 *   - movement-aware scroll (semantic first, fling when a list lies, momentum
 *     re-verify) and adaptive settle on the event counter
 */
final class Mcp {
    private final AxService svc;
    private final Core core;
    private final Object lock = new Object();

    // refs registry: node id → ref, inherited across re-bound identities
    private final Map<String, String> refs = new HashMap<>();
    private final Map<String, String> refIdentity = new HashMap<>();   // ref → identity
    private int nextRef = 1;
    private JSONArray lastNodes = null;
    private List<Object[]> lastWins = null;   // {winId, pkg, active}
    private JSONArray lastWinsJson = null;    // windows of the last snapshot
    private String lastApp = null;

    private static final String[] CHROME_PKGS = {"com.android.systemui"};
    private static final String[] CHROME_ALL = {"com.android.systemui", "com.sec.android.app.launcher",
            "com.google.android.googlequicksearchbox"};

    // flag bits — must match TreeDump.java
    private static final int F_CLICK = 1, F_LONG = 2, F_SCROLL = 4, F_EDIT = 8,
            F_CHECKABLE = 16, F_CHECKED = 32, F_ENABLED = 64, F_SELECTED = 128,
            F_FOCUSABLE = 256, F_FOCUSED = 512, F_VISIBLE = 1024, F_PASSWORD = 2048;

    Mcp(AxService svc, Core core) {
        this.svc = svc;
        this.core = core;
    }

    // ── JSON-RPC dispatch ─────────────────────────────────────────────

    JSONObject handle(JSONObject msg, String actor) {
        long id = msg.optLong("id", -1);
        String method = msg.optString("method", "");
        JSONObject params = msg.optJSONObject("params");
        if (params == null) params = new JSONObject();
        final JSONObject[] result = {new JSONObject()};
        try {
            JSONObject out = new JSONObject();
            out.put("jsonrpc", "2.0");
            if (id >= 0) out.put("id", id);
            if (method.equals("initialize")) {
                out.put("result", serverInfo(params.optString("protocolVersion")));
            } else if (method.equals("ping")) {
                out.put("result", new JSONObject());
            } else if (method.equals("tools/list")) {
                JSONArray visible = McpTools.tools();
                if (!svc.policy.screenshots()) {      // switched off by the owner: the tool is not even listed (saves tokens)
                    JSONArray keep = new JSONArray();
                    for (int i = 0; i < visible.length(); i++)
                        if (!"phone_screenshot".equals(visible.getJSONObject(i).optString("name"))) keep.put(visible.getJSONObject(i));
                    visible = keep;
                }
                out.put("result", new JSONObject().put("tools", visible));
            } else if (method.equals("tools/call")) {
                out.put("result", callResult(params.optString("name"), params.optJSONObject("arguments"), actor));
            } else {
                out.put("error", new JSONObject().put("code", -32601)
                        .put("message", "method not found: " + method));
            }
            result[0] = out;
        } catch (RpcError err) {
            // tool-level errors are results, not protocol errors (MCP contract)
            try {
                JSONObject out = new JSONObject();
                out.put("jsonrpc", "2.0");
                if (id >= 0) out.put("id", id);
                if (method.equals("tools/call")) {
                    // proper MCP CallToolResult error shape (content + isError)
                    JSONObject data = errResult(err);
                    JSONObject content = new JSONObject().put("type", "text")
                            .put("text", data.optString("text", String.valueOf(err)));
                    JSONObject sc = new JSONObject().put("code", data.optString("code", err.code));
                    out.put("result", new JSONObject()
                            .put("content", new JSONArray().put(content))
                            .put("isError", true)
                            .put("structuredContent", sc));
                } else {
                    out.put("error", new JSONObject().put("code", -32603).put("message", err.getMessage()));
                }
                result[0] = out;
            } catch (Exception ignored) {
            }
        } catch (Exception e) {
            try {
                result[0] = new JSONObject().put("jsonrpc", "2.0")
                        .put("id", id >= 0 ? (Object) id : JSONObject.NULL)
                        .put("error", new JSONObject().put("code", -32603).put("message", String.valueOf(e)));
            } catch (Exception ignored) {
            }
        }
        return result[0];
    }

    private static JSONObject serverInfo(String wantProto) throws org.json.JSONException {
        String proto = McpTools.PROTOCOLS[0];
        for (String p : McpTools.PROTOCOLS) if (p.equals(wantProto)) proto = p;
        JSONObject r = new JSONObject();
        r.put("protocolVersion", proto);
        r.put("capabilities", new JSONObject().put("tools", new JSONObject().put("listChanged", false)));
        JSONObject si = new JSONObject();
        si.put("name", "axdroid");
        si.put("title", "axuse phone-use (Android, on-device)");
        si.put("version", McpTools.VERSION);
        r.put("serverInfo", si);
        r.put("instructions", McpTools.INSTRUCTIONS);
        return r;
    }

    // ── tool table dispatch ───────────────────────────────────────────

    JSONObject call(String name, JSONObject a, String actor) throws RpcError, org.json.JSONException {
        if (a == null) a = new JSONObject();
        boolean act = !McpTools.isReadOnly(name);
        String custody = svc.custody.check(act);
        if (custody != null) throw new RpcError(custody, custody);
        if (name.equals("phone_observe")) return toolObserve(a);
        if (name.equals("phone_find")) return toolFind(a);
        if (name.equals("phone_tap")) return toolTap(a);
        if (name.equals("phone_type")) return toolType(a);
        if (name.equals("phone_scroll")) return toolScroll(a);
        if (name.equals("phone_swipe")) return toolSwipe(a);
        if (name.equals("phone_back")) return globalAs("back", "back");
        if (name.equals("phone_button")) return globalAs(a.optString("name"), "button " + a.optString("name"));
        if (name.equals("phone_launch")) return toolLaunch(a);
        if (name.equals("phone_apps")) return toolApps();
        if (name.equals("phone_wait")) return toolWait(a);
        if (name.equals("phone_run")) return toolRun(a, actor);
        if (name.equals("phone_status")) return toolStatus();
        if (name.equals("phone_screenshot")) return toolScreenshot(a);
        throw new RpcError("UNKNOWN_TOOL", "unknown tool " + name);
    }

    /** MCP CallToolResult shape: content + isError (errors are results, not
     *  protocol errors — the same contract as the host server). Every call is
     *  metered to the phone journal (ms + output chars → token estimates). */
    private JSONObject callResult(String name, JSONObject a, String actor)
            throws RpcError, org.json.JSONException {
        long t0 = android.os.SystemClock.elapsedRealtime();
        JSONObject data;
        String code = null;
        try {
            data = call(name, a, actor);
        } catch (RpcError e) {
            long ms = android.os.SystemClock.elapsedRealtime() - t0;
            try {
                svc.journal.append("mcp_tool", name, false, e.code, actor, ms, null);
            } catch (Exception ignored) {
            }
            throw e;
        }
        long ms = android.os.SystemClock.elapsedRealtime() - t0;
        boolean isError = data.optBoolean("isError", false);
        String text = data.optString("text", data.toString());
        try {
            svc.journal.append("mcp_tool", name, !isError, data.optString("code"),
                    actor, ms, "chars=" + text.length());
        } catch (Exception ignored) {
        }
        JSONObject contentItem = new JSONObject().put("type", "text").put("text", text);
        JSONArray content = new JSONArray().put(contentItem);
        if (data.has("image_b64")) {      // a screenshot: MCP image content, kept out of structuredContent
            content.put(new JSONObject().put("type", "image").put("data", data.optString("image_b64"))
                    .put("mimeType", data.optString("mime", "image/jpeg")));
            data.remove("image_b64");
            data.remove("mime");
        }
        JSONObject r = new JSONObject()
                .put("content", content)
                .put("isError", isError);
        data.remove("text");
        data.remove("isError");
        if (data.length() > 0) r.put("structuredContent", data);
        return r;
    }

    // ── observe / render / refs ───────────────────────────────────────

    private JSONObject observeLocked(boolean fresh) throws RpcError, org.json.JSONException {
        JSONObject dump = svc.observe(1500);
        JSONArray nodes = dump.optJSONArray("nodes");
        JSONArray wins = dump.optJSONArray("windows");
        // active app
        String app = null;
        for (int i = 0; wins != null && i < wins.length(); i++) {
            JSONObject w = wins.getJSONObject(i);
            if (w.optBoolean("active")) {
                app = w.optString("pkg", null);
                break;
            }
        }
        if (app == null && wins != null && wins.length() > 0) app = wins.getJSONObject(0).optString("pkg", null);
        assignRefs(nodes, app);
        // every snapshot (not only phone_observe) records its windows: chrome
        // hiding and foreground preference read them on all render paths
        lastWinsJson = wins;
        lastWins = new ArrayList<>();
        for (int i = 0; wins != null && i < wins.length(); i++) {
            JSONObject w = wins.getJSONObject(i);
            lastWins.add(new Object[]{w.optInt("id"), w.optString("pkg", ""), w.optBoolean("active")});
        }
        return dump;
    }

    private void assignRefs(JSONArray nodes, String app) throws org.json.JSONException {
        Set<String> live = new HashSet<>();
        Map<String, String> oldIdentityToId = new HashMap<>();
        if (lastNodes != null) {
            for (int i = 0; i < lastNodes.length(); i++) {
                JSONObject n = lastNodes.getJSONObject(i);
                String nid = n.getString("id");
                if (live0(nodes, nid)) {
                    live.add(nid);
                } else {
                    oldIdentityToId.putIfAbsent(identity(n), nid);
                }
            }
        }
        refs.keySet().retainAll(live);
        for (int i = 0; i < nodes.length(); i++) {
            JSONObject n = nodes.getJSONObject(i);
            String nid = n.getString("id");
            String ref = refs.get(nid);
            if (ref == null) {
                String prev = oldIdentityToId.get(identity(n));
                if (prev != null && refs.containsKey(prev)) {
                    ref = refs.remove(prev);
                    refs.put(nid, ref);
                } else {
                    ref = "e" + (nextRef++);
                    refs.put(nid, ref);
                }
            }
            n.put("ref", ref);
            refIdentity.put(ref, identity(n));
        }
        lastNodes = nodes;
        lastApp = app;
    }

    private static boolean live0(JSONArray nodes, String nid) {
        for (int i = 0; i < nodes.length(); i++)
            if (nodes.optJSONObject(i).optString("id").equals(nid)) return true;
        return false;
    }

    private static String identity(JSONObject n) {
        return role(n) + "|" + n.optString("rid", "") + "|" + n.optString("text", "") + "|"
                + n.optString("desc", "") + "|" + ((n.optInt("f", 0) & (F_CHECKABLE | F_CHECKED)));
    }

    private String nodeSig(JSONObject n) {
        return n.optString("sig", identity(n));
    }

    // roles (port of model.infer_role — class first, flags as tiebreak)
    private static final Object[][] CLASS_ROLES = {
            {"ImageButton", "image-button"}, {"MaterialButton", "button"}, {"AppCompatButton", "button"},
            {"Button", "button"}, {"CheckBox", "checkbox"}, {"SwitchCompat", "switch"}, {"Switch", "switch"},
            {"ToggleButton", "switch"}, {"RadioButton", "radio"}, {"CheckedTextView", "cell"},
            {"EditText", "textfield"}, {"TextInputEditText", "textfield"}, {"SeekBar", "slider"},
            {"ProgressBar", "progress"}, {"Spinner", "picker"}, {"WebView", "webview"},
            {"RecyclerView", "list"}, {"ListView", "list"}, {"NestedScrollView", "scroll"},
            {"ScrollView", "scroll"}, {"ViewPager2", "pager"}, {"ViewPager", "pager"},
            {"Toolbar", "navbar"}, {"ActionBarContainer", "navbar"}, {"TabView", "tab"},
            {"TabItem", "tab"}, {"ImageView", "image"}, {"TextView", "text"},
    };

    static String role(JSONObject n) {
        String cls = n.optString("cls", "");
        String base = cls.contains(".") ? cls.substring(cls.lastIndexOf('.') + 1) : cls;
        int f = n.optInt("f", 0);
        if (base.endsWith("EditText")) return "textfield";
        if (base.endsWith("Button") || base.contains("Button"))
            return base.contains("Image") ? "image-button" : "button";
        for (Object[] cr : CLASS_ROLES) {
            String needle = (String) cr[0];
            if (cls.contains(needle)) return (String) cr[1];
        }
        if ((f & F_EDIT) != 0) return "textfield";
        if ((f & F_SCROLL) != 0) return "scroll";
        // a clickable row is a cell even when it is also long-clickable (list
        // rows usually are): as "group" it was never rendered and its labels
        // floated free of the row's own buttons
        if ((f & F_CLICK) != 0) return (f & F_VISIBLE) != 0 ? "cell" : "group";
        return "group";
    }

    private static boolean actionable(JSONObject n) {
        String role = role(n);
        if (role.equals("button") || role.equals("image-button") || role.equals("checkbox")
                || role.equals("switch") || role.equals("radio") || role.equals("textfield")
                || role.equals("cell") || role.equals("tab") || role.equals("picker")
                || role.equals("webview") || role.equals("link")) return true;
        String[] containers = {"group", "list", "scroll", "navbar", "tabbar", "alert", "keyboard", "app", "window", "pager"};
        if ((n.optInt("f", 0) & F_CLICK) != 0) {
            for (String c : containers) if (role.equals(c)) return false;
            return true;
        }
        return false;
    }

    static String fold(String s) {
        return (s == null ? "" : s).replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-")
                .replace("\u2019", "'").replace("\u2018", "'").replace("\u00a0", " ").trim().toLowerCase(Locale.ROOT);
    }

    /** node text as the agent must see it: never cut below 2000 chars (a
     *  60-char clip made documents, notes and descriptions unreadable),
     *  line breaks kept visible as ⏎ */
    static String full(String s) {
        if (s == null) return "";
        s = s.replace("\r\n", "\n").trim();
        if (s.length() > 2000) s = s.substring(0, 1999) + "…";
        return s.replace("\n", "⏎").replaceAll("[ \\t]+", " ");
    }

    private static String clip(String s, int n) {
        s = s == null ? "" : s.replaceAll("\\s+", " ").trim();
        return s.length() <= n ? s : s.substring(0, n - 1) + "…";
    }

    private static String shortRid(String rid) {
        if (rid == null || rid.isEmpty()) return "";
        String s = rid;
        int c = s.lastIndexOf('/');
        if (c >= 0) s = s.substring(c + 1);
        c = s.lastIndexOf(':');
        if (c >= 0) s = s.substring(c + 1);
        return s;
    }

    /** The compact agent-facing render: chrome hidden, textless wrappers
     *  dropped, a row's own texts folded into the row's line, runs of leaf
     *  siblings joined on one line — every named/actionable node stays. */
    String render(JSONArray nodes, JSONArray wins, boolean lite, int maxLines, boolean chrome,
                  String appLine) throws org.json.JSONException {
        List<String> lines = renderLines(nodes, wins, lite, chrome);
        StringBuilder sb = new StringBuilder();
        if (appLine != null) sb.append(appLine).append('\n');
        for (int i = 0; i < lines.size(); i++) {
            if (i >= maxLines) {
                sb.append("… (").append(lines.size() - i).append(" more lines; phone_observe max_lines to see all)\n");
                break;
            }
            sb.append(lines.get(i)).append('\n');
        }
        return sb.toString();
    }

    private static final int FOLD_MAX = 8;

    List<String> renderLines(JSONArray nodes, JSONArray wins, boolean lite, boolean chrome)
            throws org.json.JSONException {
        List<String> out = new ArrayList<>();
        if (nodes == null) return out;
        if (wins == null) wins = lastWinsJson;
        Map<Integer, String> winPkg = new HashMap<>();
        Set<Integer> imeWins = new HashSet<>();
        // systemui windows the user is actually in — the active one (dialogs:
        // brightness, volume; the shade) or a big one (keyguard, quick
        // settings) — are content, not chrome. Hiding every systemui node made
        // the brightness slider and the lock screen render as an empty screen.
        Set<Integer> shownChrome = new HashSet<>();
        for (int i = 0; wins != null && i < wins.length(); i++) {
            JSONObject w = wins.getJSONObject(i);
            winPkg.put(w.optInt("id"), w.optString("pkg", ""));
            if (w.optBoolean("ime")) imeWins.add(w.optInt("id"));
            JSONArray b = w.optJSONArray("bounds");
            boolean big = b != null && (b.optInt(3) - b.optInt(1)) > 1000;
            if (w.optBoolean("active") || big) shownChrome.add(w.optInt("id"));
        }
        int count = nodes.length();
        boolean[] keep = new boolean[count];
        boolean keyboard = false;
        for (int i = 0; i < count; i++) {
            JSONObject n = nodes.getJSONObject(i);
            if ((n.optInt("f", 0) & F_VISIBLE) == 0) continue;
            if (!chrome && isChrome(winPkg.getOrDefault(winId(n), ""))
                    && !shownChrome.contains(winId(n))) continue;
            // the soft keyboard's keys: ~40 lines per result and never needed
            // (phone_type sets text directly) — one line says it is up
            if (imeWins.contains(winId(n))) {
                keyboard = true;
                continue;
            }
            String role = role(n);
            boolean named = !name(n).isEmpty();
            if (lite) {
                keep[i] = role.equals("navbar") || (actionable(n) && (named || role.equals("textfield")
                        || role.equals("switch") || role.equals("checkbox")));
            } else {
                // a resource id alone no longer earns a line: textless layout
                // wrappers (@content, @action_bar_root…) carried no information
                keep[i] = named || actionable(n) || role.equals("textfield") || role.equals("alert")
                        || role.equals("keyboard") || role.equals("webview")
                        || ((role.equals("list") || role.equals("scroll") || role.equals("pager"))
                        && !n.optString("rid", "").isEmpty());
            }
        }
        // fold: an actionable row whose kept descendants are plain labels
        // becomes one line carrying all of their texts
        String[] extra = new String[count];
        boolean[] folded = new boolean[count];
        for (int i = 0; i < count && !lite; i++) {
            if (!keep[i] || folded[i]) continue;
            JSONObject n = nodes.getJSONObject(i);
            String role = role(n);
            if (!actionable(n) || role.equals("textfield") || role.equals("webview")) continue;
            String id = n.getString("id");
            List<Integer> desc = new ArrayList<>();
            boolean ok = true;
            int j = i + 1;
            String skip = null;   // subtree of a nested control: its labels stay with it
            for (; j < count && isDescendant(id, nodes.getJSONObject(j).getString("id")); j++) {
                JSONObject m = nodes.getJSONObject(j);
                if (skip != null && isDescendant(skip, m.getString("id"))) continue;
                skip = null;
                if (!keep[j]) continue;
                if (foldable(m)) {
                    desc.add(j);
                    continue;
                }
                String mr = role(m);
                if (actionable(m) && !mr.equals("list") && !mr.equals("scroll")) {
                    // the row's own button (⋮, checkbox…): stays a separate,
                    // indented line under the row; the row keeps its labels
                    skip = m.getString("id");
                    continue;
                }
                ok = false;   // a container (list/scroller) is not one row
                break;
            }
            if (!ok || desc.isEmpty() || desc.size() > FOLD_MAX) continue;
            StringBuilder ex = new StringBuilder();
            Set<String> seen = new HashSet<>();
            seen.add(fold(name(n)));
            for (int k : desc) {
                String nm = name(nodes.getJSONObject(k));
                if (seen.add(fold(nm))) ex.append(" · \"").append(full(nm)).append('"');
                folded[k] = true;
            }
            extra[i] = ex.toString();
        }
        // emit with depth = number of emitted ancestors
        List<Integer> emit = new ArrayList<>();
        for (int i = 0; i < count; i++) if (keep[i] && !folded[i]) emit.add(i);
        int[] depth = new int[emit.size()];
        List<String> stack = new ArrayList<>();
        for (int e = 0; e < emit.size(); e++) {
            String id = nodes.getJSONObject(emit.get(e)).getString("id");
            while (!stack.isEmpty() && !isDescendant(stack.get(stack.size() - 1), id))
                stack.remove(stack.size() - 1);
            depth[e] = stack.size();
            stack.add(id);
        }
        int e = 0;
        while (e < emit.size()) {
            JSONObject n = nodes.getJSONObject(emit.get(e));
            String role = role(n);
            // run of ≥3 leaf siblings with the same role → one line, all kept
            int r = e;
            while (r < emit.size()) {
                JSONObject m = nodes.getJSONObject(emit.get(r));
                boolean leaf = r + 1 >= emit.size() || depth[r + 1] <= depth[r];
                if (depth[r] != depth[e] || !role(m).equals(role) || !parentId(m).equals(parentId(n))
                        || !leaf || extra[emit.get(r)] != null) break;
                r++;
            }
            if (r - e >= 3) {
                StringBuilder sb = new StringBuilder(indent(depth[e])).append("- ").append(role)
                        .append('×').append(r - e).append(':');
                for (int k = e; k < r; k++) sb.append(' ').append(body(nodes.getJSONObject(emit.get(k)), null));
                out.add(sb.toString());
                e = r;
                continue;
            }
            out.add(indent(depth[e]) + "- " + role + " " + body(n, extra[emit.get(e)]));
            e++;
        }
        if (keyboard) out.add("- keyboard (on screen; phone_back hides it)");
        return out;
    }

    /** a label that can ride on its row's line: no own action, no state */
    private static boolean foldable(JSONObject n) {
        if (actionable(n) || name(n).isEmpty()) return false;
        if ((n.optInt("f", 0) & (F_CHECKABLE | F_EDIT | F_SCROLL | F_SELECTED)) != 0) return false;
        String role = role(n);
        return !(role.equals("list") || role.equals("scroll") || role.equals("pager") || role.equals("webview"));
    }

    private static boolean isDescendant(String ancestorId, String id) {
        if (ancestorId.endsWith(":")) return id.startsWith(ancestorId) && !id.equals(ancestorId);
        return id.startsWith(ancestorId + "/");
    }

    private static boolean isChrome(String pkg) {
        for (String c : CHROME_PKGS) if (c.equals(pkg)) return true;
        return false;
    }

    private static int winId(JSONObject n) {
        try {
            return Integer.parseInt(n.getString("id").substring(1, n.getString("id").indexOf(':')));
        } catch (Exception e) {
            return -1;
        }
    }

    private static String parentId(JSONObject n) throws org.json.JSONException {
        String id = n.getString("id");
        int colon = id.indexOf(':');
        String w = id.substring(0, colon + 1);
        String path = id.substring(colon + 1);
        int slash = path.lastIndexOf('/');
        return slash < 0 ? "" : w + path.substring(0, slash);
    }

    private static String indent(int depth) {
        StringBuilder s = new StringBuilder();
        for (int i = 0; i < Math.min(depth, 24); i++) s.append("  ");
        return s.toString();
    }

    /** one node without its role: "name" · "folded" states @rid [eN]. The
     *  resource id is shown only where it is the sole hint (nameless nodes,
     *  fields) — on named rows it was noise. */
    private static String num(double d) {
        return d == Math.rint(d) ? String.valueOf((long) d) : String.format(Locale.US, "%.2f", d);
    }

    private static String body(JSONObject n, String extra) {
        String name = name(n);
        StringBuilder sb = new StringBuilder();
        int f0 = n.optInt("f", 0);
        if ((f0 & F_EDIT) != 0 && (f0 & F_PASSWORD) == 0 && !n.optString("text", "").isEmpty()) {
            // a field's value is shown WHOLE, line breaks as ⏎: agents edit
            // from what they read, and a clipped/flattened value made them
            // rewrite documents wrong
            String v = n.optString("text", "");
            if (v.length() > 4000) v = v.substring(0, 3999) + "…";
            sb.append('"').append(v.replace("\r\n", "\n").replace("\n", "⏎")).append('"');
        } else if (!name.isEmpty()) sb.append('"').append(full(name)).append('"');
        if (extra != null && !extra.isEmpty())
            sb.append(name.isEmpty() ? extra.substring(3) : extra);
        int f = n.optInt("f", 0);
        JSONArray range = n.optJSONArray("range");
        if (range != null && range.length() == 3) {
            sb.append(" value=").append(num(range.optDouble(0))).append(" (")
                    .append(num(range.optDouble(1))).append("–").append(num(range.optDouble(2))).append(")");
        }
        if ((f & F_CHECKABLE) != 0) sb.append((f & F_CHECKED) != 0 ? " on" : " off");
        if ((f & F_SELECTED) != 0 && (f & F_CHECKABLE) == 0) sb.append(" selected");
        if ((f & F_ENABLED) == 0) sb.append(" disabled");
        if ((f & F_SCROLL) != 0 && (f & 4096) != 0) sb.append(" more-below");
        if ((f & F_EDIT) != 0 && n.optString("text", "").isEmpty() && !n.optString("hint", "").isEmpty())
            sb.append(" empty hint=\"").append(clip(n.optString("hint", ""), 32)).append('"');
        String rid = shortRid(n.optString("rid", ""));
        boolean showRid = sb.length() == 0 || role(n).equals("textfield")
                || ((extra == null || extra.isEmpty()) && name.isEmpty());
        if (!rid.isEmpty() && showRid) sb.append(sb.length() == 0 ? "" : " ").append('@').append(rid);
        String ref = n.optString("ref", "");
        if (!ref.isEmpty()) sb.append(sb.length() == 0 ? "" : " ").append('[').append(ref).append(']');
        return sb.toString();
    }

    // ── diff ──────────────────────────────────────────────────────────

    /** What changed, in the same compact lines the agent reads from
     *  observe. A screen that mostly changed (navigation, a scrolled page)
     *  comes back whole — the agent needs no follow-up observe; otherwise
     *  only added/changed lines, plus the removed ones when few. */
    String diff(JSONArray before, JSONArray beforeWins, JSONArray after, int maxLines)
            throws org.json.JSONException {
        List<String> now = renderLines(after, lastWinsJson, false, false);
        List<String> was = before == null ? new ArrayList<String>() : renderLines(before, beforeWins, false, false);
        Set<String> wasSet = new HashSet<>();
        for (String l : was) wasSet.add(l.trim());
        Set<String> nowSet = new HashSet<>();
        for (String l : now) nowSet.add(l.trim());
        List<String> added = new ArrayList<>();
        for (String l : now) if (!wasSet.contains(l.trim())) added.add(l);
        List<String> removed = new ArrayList<>();
        for (String l : was) if (!nowSet.contains(l.trim())) removed.add(l);
        StringBuilder sb = new StringBuilder();
        if (before == null || (now.size() > 6 && added.size() * 10 > now.size() * 6)) {
            sb.append("screen now (app: ").append(lastApp == null ? "?" : lastApp)
                    .append(" · device time ").append(deviceNow()).append("):\n");
            for (int i = 0; i < now.size(); i++) {
                if (i >= maxLines) {
                    sb.append("… (").append(now.size() - i).append(" more lines; phone_observe max_lines to see all)\n");
                    break;
                }
                sb.append(now.get(i)).append('\n');
            }
            return sb.toString();
        }
        for (int i = 0; i < added.size(); i++) {
            if (i >= maxLines) {
                sb.append("… (").append(added.size() - i).append(" more changed lines)\n");
                break;
            }
            sb.append("+ ").append(added.get(i).trim()).append('\n');
        }
        if (removed.size() <= 6) {
            for (String l : removed) sb.append("- ").append(l.trim()).append('\n');
        } else {
            sb.append("(").append(removed.size()).append(" lines gone)\n");
        }
        if (sb.length() == 0) sb.append("(no visible change)");
        return sb.toString();
    }

    // ── resolve: refs and ranked text (exact > prefix > substring) ────

    private JSONObject resolveRef(String ref) throws org.json.JSONException {
        if (lastNodes == null) return null;
        for (int i = 0; i < lastNodes.length(); i++) {
            JSONObject n = lastNodes.getJSONObject(i);
            if (n.optString("ref", "").equals(ref)) return n;
        }
        // identity self-heal
        String ident = refIdentity.get(ref);
        if (ident != null) {
            for (int i = 0; i < lastNodes.length(); i++) {
                JSONObject n = lastNodes.getJSONObject(i);
                if (identity(n).equals(ident) && (n.optInt("f", 0) & F_VISIBLE) != 0) return n;
            }
        }
        return null;
    }

    private List<JSONObject> findByText(String text, boolean visibleOnly) throws org.json.JSONException {
        List<JSONObject> out = new ArrayList<>();
        if (lastNodes == null || text == null) return out;
        String low = fold(text);
        for (int i = 0; i < lastNodes.length(); i++) {
            JSONObject n = lastNodes.getJSONObject(i);
            if (visibleOnly && (n.optInt("f", 0) & F_VISIBLE) == 0) continue;
            String name = fold(n.optString("text", "")) + " " + fold(n.optString("desc", ""));
            if (!low.isEmpty() && name.contains(low)) out.add(n);
        }
        return out;
    }

    /** Ranked: exact name (3) > prefix (2) > substring (1); foreground window wins. */
    private JSONObject resolveTarget(Object target) throws RpcError, org.json.JSONException {
        if (target == null) return null;
        if (target instanceof String) {
            String s = (String) target;
            if (s.startsWith("e") && s.length() > 1 && Character.isDigit(s.charAt(1))) {
                JSONObject n = resolveRef(s);
                if (n == null) throw new RpcError("STALE_REF", "ref " + s + " is not in the current tree — observe again");
                return n;
            }
            return resolveText(s);
        }
        JSONObject spec = (JSONObject) target;
        if (spec.has("ref")) {
            JSONObject n = resolveRef(spec.getString("ref"));
            if (n != null) return n;
        }
        return resolveText(spec.optString("text", ""));
    }

    private JSONObject resolveText(String text) throws RpcError, org.json.JSONException {
        List<JSONObject> hits = findByText(text, true);
        if (hits.isEmpty()) throw new RpcError("NOT_FOUND", "no element matching '" + text + "' on screen");
        String low = fold(text);
        int best = 0;
        for (JSONObject n : hits) best = Math.max(best, tier(n, low));
        List<JSONObject> tiered = new ArrayList<>();
        for (JSONObject n : hits) if (tier(n, low) == best && best > 0) tiered.add(n);
        if (tiered.isEmpty()) tiered = hits;
        // foreground window preference
        if (tiered.size() > 1 && lastApp != null) {
            List<JSONObject> fg = new ArrayList<>();
            for (JSONObject n : tiered) if (winPkgOf(n).equals(lastApp)) fg.add(n);
            if (!fg.isEmpty()) tiered = fg;
        }
        if (tiered.size() > 1) {
            List<JSONObject> actionable1 = new ArrayList<>();
            for (JSONObject n : tiered) if (actionable(n)) actionable1.add(n);
            if (actionable1.size() == 1) return actionable1.get(0);
            StringBuilder cands = new StringBuilder();
            for (int i = 0; i < Math.min(6, tiered.size()); i++)
                cands.append(i > 0 ? ", " : "").append(tiered.get(i).optString("ref", "-")).append(" ")
                        .append(role(tiered.get(i))).append(" \"").append(clip(name(tiered.get(i)), 40)).append("\"");
            throw new RpcError("AMBIGUOUS", "'" + text + "' matches " + tiered.size() + " elements: " + cands);
        }
        return tiered.get(0);
    }

    /** the single in-tree, not-visible node whose name is exactly text */
    private JSONObject hiddenExact(String text) throws org.json.JSONException {
        String low = fold(text);
        JSONObject hit = null;
        for (JSONObject n : findByText(text, false)) {
            if ((n.optInt("f", 0) & F_VISIBLE) != 0 || !fold(name(n)).equals(low)) continue;
            if (hit != null) return null;   // ambiguous: let the agent pick
            hit = n;
        }
        return hit;
    }

    private static int tier(JSONObject n, String low) {
        String name = fold(name(n));
        if (name.equals(low)) return 3;
        if (name.startsWith(low)) return 2;
        return 1;
    }

    private static String name(JSONObject n) {
        String s = n.optString("text", "");
        if (s.isEmpty()) s = n.optString("desc", "");
        return s;
    }

    private String winPkgOf(JSONObject n) {
        if (lastWins == null) return "";
        for (Object[] w : lastWins) {
            if ((Integer) w[0] == winId(n)) return (String) w[1];
        }
        return "";
    }

    // ── settle ────────────────────────────────────────────────────────

    private void settleAction(long minWaitMs) {
        try {
            Thread.sleep(Math.max(0, minWaitMs));
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        long t0 = android.os.SystemClock.elapsedRealtime();
        Long last = null;
        while (android.os.SystemClock.elapsedRealtime() - t0 < 1000) {
            try {
                JSONObject ch = core.changesInfo();
                long count = ch.optLong("count", -1);
                if (last != null && count == last && ch.optLong("quiet_ms", 0) >= 120) return;
                last = count;
                Thread.sleep(40);
            } catch (Exception e) {
                return;
            }
        }
    }

    // ── tools ─────────────────────────────────────────────────────────

    private JSONObject toolObserve(JSONObject a) throws RpcError, org.json.JSONException {
        synchronized (lock) {
            JSONObject dump = observeLocked(false);
            JSONArray nodes = dump.getJSONArray("nodes");
            JSONArray wins = dump.optJSONArray("windows");
            boolean lite = a.optString("mode", "full").equals("lite");
            String text = render(nodes, wins, lite, a.optInt("max_lines", 220), false,
                    "app: " + (lastApp == null ? "?" : lastApp) + " · device time " + deviceNow());
            JSONObject sc = new JSONObject().put("text", text).put("app", lastApp)
                    .put("nodes", nodes.length());
            return sc;
        }
    }

    private JSONObject toolFind(JSONObject a) throws RpcError, org.json.JSONException {
        // always a fresh snapshot: find is how agents check what appeared
        // since their last action (timers, async loads)
        synchronized (lock) {
            observeLocked(false);
        }
        String text = a.has("text") ? a.optString("text") : null;
        String role = a.has("role") ? a.optString("role") : null;
        String rid = a.has("rid") ? a.optString("rid") : null;
        List<JSONObject> hits = new ArrayList<>();
        if (lastNodes != null) {
            String low = text == null ? null : fold(text);
            for (int i = 0; i < lastNodes.length(); i++) {
                JSONObject n = lastNodes.getJSONObject(i);
                if (role != null && !role(n).equals(role)) continue;
                // observe prints the short id (@mode_list): accept it as well
                // as the full "pkg:id/mode_list"
                if (rid != null && !n.optString("rid", "").equals(rid)
                        && !shortRid(n.optString("rid", "")).equals(shortRid(rid))) continue;
                if (low != null) {
                    String hay = fold(n.optString("text", "")) + " " + fold(n.optString("desc", ""));
                    if (!low.isEmpty() && !hay.contains(low)) continue;
                }
                hits.add(n);
            }
        }
        // on-screen first; in-tree but not-visible nodes (collapsed lists,
        // ScrollView content below the fold) follow, marked hidden — they
        // are tappable by ref or exact text
        List<JSONObject> sorted = new ArrayList<>();
        for (JSONObject n : hits) if ((n.optInt("f", 0) & F_VISIBLE) != 0) sorted.add(n);
        for (JSONObject n : hits) if ((n.optInt("f", 0) & F_VISIBLE) == 0) sorted.add(n);
        hits = sorted;
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < Math.min(25, hits.size()); i++) {
            JSONObject n = hits.get(i);
            sb.append(n.optString("ref", "-")).append(" ").append(role(n))
                    .append(" \"").append(full(name(n))).append("\"");
            if ((n.optInt("f", 0) & F_VISIBLE) == 0) sb.append(" hidden");
            if (!n.optString("value", "").isEmpty()) sb.append(" value='").append(clip(n.optString("value"), 40)).append("'");
            if (!n.optString("rid", "").isEmpty()) sb.append(" rid=").append(n.optString("rid"));
            JSONArray b = n.optJSONArray("b");
            if (b != null) sb.append(" bounds=").append(b);
            sb.append('\n');
        }
        if (sb.length() == 0) sb.append("no match");
        return new JSONObject().put("text", sb.toString()).put("count", hits.size());
    }

    private interface ThrowingAct {
        void run() throws RpcError, org.json.JSONException;
    }

    private JSONObject actAndDiff(String opLabel, ThrowingAct act0) throws RpcError, org.json.JSONException {
        synchronized (lock) {
            JSONArray before = lastNodes, beforeWins = lastWinsJson;
            act0.run();
            settleAction(180);
            observeLocked(false);
            String d = diff(before, beforeWins, lastNodes, 150);
            return new JSONObject().put("text", opLabel + "\n" + d);
        }
    }

    private JSONObject toolTap(JSONObject a) throws RpcError, org.json.JSONException {
        convertSpace(a);
        // a confirmed blind tap (coordinates, no named target): say whether the pixels changed, so an agent on a
        // screen the tree cannot describe knows if the point hit anything. Named/ref taps are not touched.
        boolean blind = !a.has("target") && a.has("x") && a.has("y") && a.optBoolean("confirm", false) && svc.policy.screenshots();
        int[] before = blind ? svc.thumbnail() : null;
        JSONObject r = toolTapCore(a);
        if (before != null) {
            core.stableWait(0.4, 2);
            int[] after = svc.thumbnail();
            if (after != null && after.length == before.length) {
                double ch = AxService.lumaDiff(before, after);
                r.put("text", r.optString("text", "") + "\npixels: " + (ch < 0.005
                        ? "unchanged after the tap (the point probably missed: choose a different point, do not repeat this one)"
                        : Math.round(ch * 100) + "% of the screen changed after the tap"));
            }
        }
        return r;
    }

    private JSONObject toolTapCore(JSONObject a) throws RpcError, org.json.JSONException {
        Object target = a.has("target") ? a.get("target") : null;
        boolean confirm = a.optBoolean("confirm", false);
        String kind = a.optString("kind", "tap");
        synchronized (lock) {
            if (lastNodes == null) observeLocked(false);
        }
        JSONObject node = null;
        boolean hidden = false;
        if (target != null) {
            try {
                node = resolveTarget(target);
            } catch (RpcError e) {
                if (e.code.equals("NOT_FOUND") && target instanceof String
                        && (!((String) target).startsWith("e") || !Character.isDigit(((String) target).charAt(1)))) {
                    // a control the app keeps in the tree but flags not-visible
                    // (collapsed mode lists, drawers mid-animation): an
                    // accessibility click reaches it without any gesture
                    JSONObject h = hiddenExact((String) target);
                    if (h != null) {
                        node = h;
                        hidden = true;
                    } else if (mainScrollerId() != null
                            && (scrollUntil((String) target, 6, "down") || scrollUntil((String) target, 6, "up"))) {
                        // off-screen rows are not in an Android tree: scroll
                        // looking for the text — down first, then up if a fling
                        // overshot (only when the screen has a scroller at all)
                        node = resolveTarget(target);
                    } else {
                        // never fall through to a blind coordinate tap: the
                        // agent asked for a *named* element that isn't there
                        throw new RpcError("NOT_FOUND",
                                "no element matching '" + target + "' on screen"
                                        + (mainScrollerId() != null ? " (scrolled down and up)" : ""));
                    }
                } else {
                    throw e;
                }
            }
            if (node != null && !hidden && node.optJSONArray("b") != null) {
                revealIntoView(node);
                // the reveal may have re-observed and renumbered the row; a
                // failed re-resolve keeps the node (the act re-checks it)
                // instead of failing a ref the agent just read
                try {
                    JSONObject again = resolveTarget(target);
                    if (again != null) node = again;
                } catch (RpcError ignored) {
                }
            }
        }
        final JSONObject finalNode = node;
        final String op = kind.equals("long_press") ? "long_click" : "click";
        final JSONObject args = a;
        final JSONObject[] actInfo = {new JSONObject()};
        RpcError actErr = null;
        for (int attempt = 0; attempt < 3; attempt++) {
            JSONObject cur = finalNode;
            if (attempt > 0 && cur != null) {
                // STALE_REF retry: re-resolve the target on a fresh tree (a list
                // re-bound the row between resolve and act — same policy as the
                // host engine)
                synchronized (lock) {
                    try {
                        observeLocked(false);
                    } catch (Exception ignored) {
                    }
                }
                try {
                    JSONObject again = resolveTarget(target);
                    if (again != null) cur = again;
                } catch (RpcError ignored) {
                }
            }
            final JSONObject nodeNow = cur;
            try {
                return actAndDiffOnce(op, args, confirm, target, nodeNow, actInfo);
            } catch (RpcError e) {
                if (nodeNow != null && (e.code.equals("OCCLUDED") || (e.code.equals("STALE_REF") && attempt == 2))) {
                    throw goneOrOccluded(e, target, nodeNow);
                }
                if (!e.code.equals("STALE_REF") || attempt == 2 || nodeNow == null) throw e;
                actErr = e;
                try {
                    Thread.sleep(300);
                } catch (InterruptedException ie) {
                    Thread.currentThread().interrupt();
                    throw e;
                }
            }
        }
        throw actErr != null ? actErr : new RpcError("INTERNAL", "tap retries exhausted");
    }

    /** a tap refused as covered: was the target a panel that animated away
     *  (a launch-time peek of a mode list / drawer)? Then say so and show the
     *  screen — "covered by FrameLayout" sent the agent to tap something else
     *  (CameraTakePhoto shot a video: the camera stayed in video mode). */
    private RpcError goneOrOccluded(RpcError e, Object target, JSONObject was) throws org.json.JSONException {
        try {
            Thread.sleep(500);
        } catch (InterruptedException ie) {
            Thread.currentThread().interrupt();
            return e;
        }
        synchronized (lock) {
            try {
                observeLocked(false);
            } catch (Exception ignored) {
                return e;
            }
        }
        String nm = name(was);
        boolean present = false;
        for (int i = 0; lastNodes != null && i < lastNodes.length(); i++) {
            JSONObject n = lastNodes.getJSONObject(i);
            if ((n.optInt("f", 0) & F_VISIBLE) != 0 && !nm.isEmpty() && nm.equals(name(n))) {
                present = true;
                break;
            }
        }
        if (present) return e;
        // which edge the panel hid behind: the side the control sat on
        String reopen = "";
        JSONArray b = was.optJSONArray("b");
        if (b != null && b.length() == 4) {
            int cx = (b.optInt(0) + b.optInt(2)) / 2, cy = (b.optInt(1) + b.optInt(3)) / 2;
            int w = svc.getResources().getDisplayMetrics().widthPixels;
            if (cx < w / 3) {
                reopen = " Reopen it: phone_swipe {x1: 120, y1: " + cy + ", x2: " + (w * 3 / 4) + ", y2: " + cy
                        + ", duration: 400} (from the left edge; a swipe AT the edge is system Back), then"
                        + " tap '" + clip(nm, 40) + "' again.";
            } else if (cx > w * 2 / 3) {
                reopen = " Reopen it: phone_swipe {x1: " + (w - 120) + ", y1: " + cy + ", x2: " + (w / 4)
                        + ", y2: " + cy + ", duration: 400} (from the right edge), then tap '"
                        + clip(nm, 40) + "' again.";
            }
        }
        return new RpcError("TARGET_GONE", "NOT TAPPED: '" + clip(nm, 60) + "' disappeared before the tap"
                + " landed (it was on a panel that animated closed) — do not continue as if it had been"
                + " tapped." + (reopen.isEmpty() ? " Reopen the panel (edge swipe ~120 px inside the edge, or"
                + " its menu button) and tap it again." : reopen) + "\n" + diff(null, null, lastNodes, 150));
    }

    private JSONObject actAndDiffOnce(String op, JSONObject args, boolean confirm, Object target,
                                      JSONObject nodeNow, JSONObject[] actInfo)
            throws RpcError, org.json.JSONException {
        JSONObject out = actAndDiff(
                "tap " + (nodeNow != null ? clip(name(nodeNow), 40)
                        : "(" + args.opt("x") + "," + args.opt("y") + ")"),
                () -> {
                    if (nodeNow != null) {
                        actInfo[0] = core.callAct(op, nodeNow.getString("id"), confirm,
                                nodeNow.optString("sig", ""), nodeNow.optString("rid", ""),
                                clip(name(nodeNow), 80));
                    } else {
                        actInfo[0] = core.callActAt(op, (float) args.optDouble("x", -1),
                                (float) args.optDouble("y", -1), confirm);
                    }
                });
        // host-format parity: '· N ms · via ax-click' head on the diff
        String via = actInfo[0].optString("via", "");
        long ms = actInfo[0].optLong("ms", 0);
        String head = out.optString("text", "").split("\n", 2)[0]
                + (ms > 0 ? " · " + ms + " ms" : "") + (via.isEmpty() ? "" : " · via " + via);
        out.put("text", head + "\n"
                + (out.optString("text", "").contains("\n") ? out.optString("text", "").split("\n", 2)[1] : ""));
        return out;
    }

    private void revealIntoView(JSONObject node) {
        try {
            JSONArray b = node.optJSONArray("b");
            if (b == null) return;
            JSONObject st = svc.deviceInfo().optJSONObject("display");
            int h = st == null ? 2340 : st.optInt("h", 2340);
            String ident = identity(node);
            boolean down = b.optInt(1) >= h * 0.5;
            for (int i = 0; i < 4; i++) {
                if (b.optInt(1) < h * 0.94 && b.optInt(3) > h * 0.06) return;
                try {
                    core.dispatchScroll(down, 1, mainScrollerId());
                } catch (RpcError e) {
                    return;
                }
                settleAction(200);
                synchronized (lock) {
                    try {
                        observeLocked(false);
                    } catch (Exception ignored) {
                    }
                }
                JSONObject found = findByIdentity(ident);
                if (found != null && found.optJSONArray("b") != null) {
                    b = found.optJSONArray("b");
                    if (b.optInt(1) < h * 0.94 && b.optInt(3) > h * 0.06) return;
                }
            }
        } catch (Exception ignored) {
        }
    }

    private JSONObject findByIdentity(String ident) throws org.json.JSONException {
        if (lastNodes == null) return null;
        for (int i = 0; i < lastNodes.length(); i++) {
            JSONObject n = lastNodes.getJSONObject(i);
            if (identity(n).equals(ident) && (n.optInt("f", 0) & F_VISIBLE) != 0) return n;
        }
        return null;
    }

    private JSONObject toolType(JSONObject a) throws RpcError, org.json.JSONException {
        String text = a.optString("text", "");
        Object into = a.has("into") ? a.get("into") : null;
        boolean submit = a.optBoolean("submit", false);
        boolean replace = a.optBoolean("replace", true);
        JSONObject result = null;
        RpcError lastErr = null;
        for (int attempt = 0; attempt < 3; attempt++) {
            synchronized (lock) {
                if (lastNodes == null) observeLocked(false);
            }
            String id = null;
            String label = "focused";
            if (into != null) {
                try {
                    JSONObject f = resolveTarget(into);
                    id = f.getString("id");
                    label = clip(name(f), 40);
                    if (f.optJSONArray("range") != null && (f.optInt("f", 0) & F_EDIT) == 0) {
                        return setProgress(f, text);
                    }
                    if ((f.optInt("f", 0) & F_EDIT) == 0) {
                        throw new RpcError("NOT_EDITABLE", "element is not a text field");
                    }
                } catch (RpcError e) {
                    if (e.code.equals("STALE_REF") && attempt < 2) {
                        lastErr = e;
                        try {
                            Thread.sleep(300);
                        } catch (InterruptedException ie) {
                            Thread.currentThread().interrupt();
                            throw e;
                        }
                        synchronized (lock) {
                            try {
                                observeLocked(false);
                            } catch (Exception ignored) {
                            }
                        }
                        continue;
                    }
                    throw e;
                }
            }
            try {
                result = core.callType(id, text, replace, submit, a.optString("at", ""));
                break;
            } catch (RpcError e) {
                if (e.code.equals("STALE_REF") && attempt < 2) {
                    lastErr = e;
                    try {
                        Thread.sleep(300);
                    } catch (InterruptedException ie) {
                        Thread.currentThread().interrupt();
                        throw e;
                    }
                    continue;
                }
                throw e;
            }
        }
        if (result == null) throw lastErr != null ? lastErr : new RpcError("INTERNAL", "type retries exhausted");
        settleAction(200);
        JSONArray before = lastNodes, beforeWins = lastWinsJson;
        synchronized (lock) {
            try {
                observeLocked(false);
            } catch (Exception ignored) {
            }
        }
        String d = diff(before, beforeWins, lastNodes, 150);
        String head = "type · via " + result.optString("via", "ax-set-text")
                + (result.optBoolean("verified") ? " · verified" : "");
        return new JSONObject().put("text", head + "\n" + d).put("value", result.opt("value"))
                .put("verified", result.optBoolean("verified"));
    }

    /** phone_type on a slider / seek bar: set its value exactly with the
     *  platform's ACTION_SET_PROGRESS (swipes on a slider landed anywhere and
     *  could not be verified — the range was not even dumped). */
    private JSONObject setProgress(JSONObject f, String text) throws RpcError, org.json.JSONException {
        float v;
        try {
            v = Float.parseFloat(text.trim().replace("%", ""));
        } catch (NumberFormatException e) {
            throw new RpcError("BAD_REQUEST", "a slider takes a number (its range is shown as value=… (min–max))");
        }
        android.view.accessibility.AccessibilityNodeInfo node = svc.resolve(f.getString("id"));
        if (node == null) throw new RpcError("STALE_REF", "slider is gone; observe again");
        android.os.Bundle b = new android.os.Bundle();
        b.putFloat(android.view.accessibility.AccessibilityNodeInfo.ACTION_ARGUMENT_PROGRESS_VALUE, v);
        boolean ok = node.performAction(android.R.id.accessibilityActionSetProgress, b);
        if (!ok) throw new RpcError("ACTION_FAILED", "the slider rejected ACTION_SET_PROGRESS");
        JSONArray before = lastNodes, beforeWins = lastWinsJson;
        settleAction(250);
        synchronized (lock) {
            try {
                observeLocked(false);
            } catch (Exception ignored) {
            }
        }
        return new JSONObject().put("text", "set " + clip(name(f), 40) + " to " + num(v) + " · via ax-set-progress\n"
                + diff(before, beforeWins, lastNodes, 150));
    }

    /** the id of the largest visible scroller in the last observe (or null). */
    private String mainScrollerId() throws org.json.JSONException {
        if (lastNodes == null) return null;
        JSONObject best = null;
        long bestArea = 0;
        for (int i = 0; i < lastNodes.length(); i++) {
            JSONObject n = lastNodes.getJSONObject(i);
            String role = role(n);
            if (!role.equals("scroll") && !role.equals("list")) continue;
            if ((n.optInt("f", 0) & F_VISIBLE) == 0 || n.optJSONArray("b") == null) continue;
            JSONArray b = n.getJSONArray("b");
            long area = (long) (b.optInt(2) - b.optInt(0)) * (b.optInt(3) - b.optInt(1));
            if (area > bestArea) {
                bestArea = area;
                best = n;
            }
        }
        return best != null ? best.getString("id") : null;
    }

    /** The movement-aware scroll loop. Returns {found, swipes, note}. Shared by
     *  phone_scroll and phone_tap's auto-scroll (one behaviour, one place). */
    private JSONObject scrollLoop(String direction, String until, int maxSwipes)
            throws RpcError, org.json.JSONException {
        synchronized (lock) {
            if (lastNodes == null) observeLocked(false);
        }
        String method = "ax";
        String note = null;
        boolean found = false;
        int swipes = 0;
        for (int i = 0; i < maxSwipes; i++) {
            Set<String> sigsBefore = visibleSigs();
            boolean forward = direction.equals("down") || direction.equals("right");
            boolean axFailed = false;
            if (method.equals("ax")) {
                try {
                    core.dispatchScroll(forward, 1, mainScrollerId());
                    settleAction(200);
                    // the list animates after the action: reading the tree
                    // mid-scroll showed one new row of a whole page
                    core.stableWait(0.35, 3);
                } catch (RpcError e) {
                    // a list that re-bound its rows (STALE_REF) or a lying list:
                    // fall through to a real fling for this swipe
                    axFailed = true;
                }
            }
            if (!method.equals("ax") || axFailed) {
                core.flingList(forward);
                settleAction(250);
            }
            synchronized (lock) {
                try {
                    observeLocked(false);
                } catch (Exception ignored) {
                }
            }
            swipes = i + 1;
            boolean moved = !visibleSigs().equals(sigsBefore);
            if (!moved) method = method.equals("ax") ? "fling" : "ax";
            if (!until.isEmpty()) {
                if (untilVisible(until)) {
                    core.stableWait(0.6, 4);   // momentum re-verify
                    synchronized (lock) {
                        try {
                            observeLocked(false);
                        } catch (Exception ignored) {
                        }
                    }
                    if (untilVisible(until)) {
                        found = true;
                        note = "scrolled " + (i + 1) + "×" + direction + " → '" + until + "' visible";
                        break;
                    }
                }
            } else if (!moved) {
                note = "end of list after " + i + " swipes";
                break;
            }
        }
        return new JSONObject().put("found", found).put("swipes", swipes).put("note", note == null ? "" : note);
    }

    private boolean scrollUntil(String text, int maxSwipes, String direction)
            throws RpcError, org.json.JSONException {
        return scrollLoop(direction, text, maxSwipes).optBoolean("found");
    }

    private JSONObject toolScroll(JSONObject a) throws RpcError, org.json.JSONException {
        long t0 = android.os.SystemClock.elapsedRealtime();
        String direction = a.optString("direction", "down");
        String until = a.optString("until", "");
        synchronized (lock) {
            if (lastNodes == null) observeLocked(false);
        }
        // the viewport before the scroll: the result shows the rows it revealed
        // (or the whole new page) instead of the first raw nodes of the tree
        JSONArray before = lastNodes, beforeWins = lastWinsJson;
        JSONObject r = scrollLoop(direction, until, a.optInt("max_swipes", 8));
        String d = diff(before, beforeWins, lastNodes, 150);
        String note = r.optString("note", "");
        String text = (note.isEmpty() ? (until.isEmpty() ? "scrolled" : "'" + until + "' not found")
                : note);
        if (until.isEmpty() && note.isEmpty())
            text += anyMoreBelow() ? " (more rows below: keep scrolling)" : " (reached the end of the list)";
        text += "\n" + d;
        return new JSONObject().put("text", text)
                .put("found", r.optBoolean("found"))
                .put("ms", android.os.SystemClock.elapsedRealtime() - t0);
    }

    /** does a scrollable on screen still accept scroll-forward? */
    private boolean anyMoreBelow() throws org.json.JSONException {
        if (lastNodes == null) return false;
        for (int i = 0; i < lastNodes.length(); i++) {
            int f = lastNodes.getJSONObject(i).optInt("f", 0);
            if ((f & F_SCROLL) != 0 && (f & 4096) != 0 && (f & F_VISIBLE) != 0) return true;
        }
        return false;
    }

    private Set<String> visibleSigs() throws org.json.JSONException {
        Set<String> out = new HashSet<>();
        if (lastNodes == null) return out;
        for (int i = 0; i < lastNodes.length(); i++) {
            JSONObject n = lastNodes.getJSONObject(i);
            if ((n.optInt("f", 0) & F_VISIBLE) != 0 && (!name(n).isEmpty()))
                out.add(nodeSig(n));
        }
        return out;
    }

    /** until is satisfied only by the exact row (substring hits don't count). */
    private boolean untilVisible(String text) throws org.json.JSONException {
        if (lastNodes == null) return false;
        String low = fold(text);
        boolean exactVisible = false, exactAnywhere = false;
        boolean substringVisible = false;
        for (int i = 0; i < lastNodes.length(); i++) {
            JSONObject n = lastNodes.getJSONObject(i);
            boolean vis = (n.optInt("f", 0) & F_VISIBLE) != 0;
            String nm = fold(name(n));
            if (nm.equals(low)) {
                exactAnywhere = true;
                if (vis) exactVisible = true;
            } else if (vis && nm.contains(low)) {
                substringVisible = true;
            }
        }
        if (exactVisible) return true;
        if (exactAnywhere) return false;      // the real row exists but is off-screen
        return substringVisible;
    }

    private JSONObject toolSwipe(JSONObject a) throws RpcError, org.json.JSONException {
        float x1 = (float) a.optDouble("x1"), y1 = (float) a.optDouble("y1");
        float x2 = (float) a.optDouble("x2"), y2 = (float) a.optDouble("y2");
        float dur = (float) a.optDouble("duration", 0.3);
        return actAndDiff("swipe", () ->
                svc.dispatchStroke(points(x1, y1, x2, y2), (long) (dur * 1000), (long) (dur * 1000 + 3000)));
    }

    private static List<float[]> points(float... xy) {
        List<float[]> pts = new ArrayList<>();
        for (int i = 0; i + 1 < xy.length; i += 2) pts.add(new float[]{xy[i], xy[i + 1]});
        if (pts.isEmpty()) pts.add(new float[]{0, 0});
        pts.add(pts.get(pts.size() - 1));
        if (pts.size() < 2) pts.add(pts.get(0));
        return pts;
    }

    private JSONObject globalAs(String name, String label) throws RpcError, org.json.JSONException {
        return actAndDiff(label, () -> svc.globalAction(name));
    }

    private JSONObject toolLaunch(JSONObject a) throws RpcError, org.json.JSONException {
        String app = a.optString("app", "");
        String pkg = core.resolveApp(app);
        String fgBefore = activePkg();
        // an open notification shade / quick settings stays ON TOP of a
        // launched app (the agent kept seeing systemui after 8 s): close it
        if (fgBefore != null && isChrome(fgBefore) && android.os.Build.VERSION.SDK_INT >= 31) {
            svc.performGlobalAction(android.accessibilityservice.AccessibilityService
                    .GLOBAL_ACTION_DISMISS_NOTIFICATION_SHADE);
            try {
                Thread.sleep(250);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            fgBefore = activePkg();
        }
        JSONObject r = core.callLaunch(pkg, a.optBoolean("fresh", false));
        // wait for the launched app to own the active window: the launcher
        // is quiet while the app starts, so a stability wait alone returned
        // the launcher's (or the previous app's) tree
        long t0 = android.os.SystemClock.elapsedRealtime();
        while (android.os.SystemClock.elapsedRealtime() - t0 < 8000) {
            String fg = activePkg();
            if (pkg.equals(fg) || (fg != null && !fg.equals(fgBefore) && !isChrome(fg)
                    && !fg.contains("launcher"))) break;
            // a systemui window (keyguard, heads-up, shade) can hold focus
            // while the app is already on screen: every such launch waited
            // the full 8 s
            if (hasWindowOf(pkg)) break;
            try {
                Thread.sleep(100);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                break;
            }
        }
        core.stableWait(0.5, 3);
        synchronized (lock) {
            try {
                observeLocked(false);
            } catch (Exception ignored) {
            }
        }
        String warn = (pkg.equals(lastApp) ? "" : " · foreground is " + lastApp)
                + " · device time " + deviceNow();
        return new JSONObject().put("text", "launch " + app + " (" + pkg + ") · "
                + (android.os.SystemClock.elapsedRealtime() - t0) + " ms" + warn + "\n"
                + render(lastNodes, null, false, 150, false, null));
    }

    /** the PHONE's clock: relative dates ("in two weeks", "today") are the
     *  device's — agents otherwise fall back to their host's date */
    static String deviceNow() {
        return new java.text.SimpleDateFormat("EEE yyyy-MM-dd HH:mm", Locale.US)
                .format(new java.util.Date());
    }

    private boolean hasWindowOf(String pkg) {
        try {
            for (android.view.accessibility.AccessibilityWindowInfo w : svc.getWindows()) {
                android.view.accessibility.AccessibilityNodeInfo r = w.getRoot();
                if (r != null && pkg.equals(String.valueOf(r.getPackageName()))) return true;
            }
        } catch (Exception ignored) {
        }
        return false;
    }

    private String activePkg() {
        try {
            android.view.accessibility.AccessibilityNodeInfo root = svc.getRootInActiveWindow();
            if (root == null) return null;
            CharSequence p = root.getPackageName();
            return p == null ? null : String.valueOf(p);
        } catch (Exception e) {
            return null;
        }
    }

    private JSONObject toolApps() throws RpcError, org.json.JSONException {
        JSONArray apps = core.appsList();
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < apps.length(); i++) {
            JSONObject app = apps.getJSONObject(i);
            sb.append(app.optString("name")).append(" — ").append(app.optString("pkg")).append('\n');
        }
        return new JSONObject().put("text", sb.toString());
    }

    private JSONObject toolWait(JSONObject a) throws RpcError, org.json.JSONException {
        String text = a.optString("text", "");
        boolean gone = a.optBoolean("gone", false);
        double timeout = a.optDouble("timeout", 8.0), stable = a.optDouble("stable", 0.0);
        JSONObject r = core.callWait(text, gone, timeout, stable);
        String msg = r.optBoolean("ok")
                ? "'" + text + "' " + (gone ? "gone" : "visible") + " · " + r.optLong("elapsed_ms") + " ms"
                : "'" + text + "' " + (gone ? "still present" : "never appeared") + " after " + (int) timeout + "s";
        JSONObject out = new JSONObject().put("text", msg).put("matched", r.optBoolean("ok"));
        if (!r.optBoolean("ok")) out.put("_error", "WAIT_TIMEOUT");
        return out;
    }

    private JSONObject toolRun(JSONObject a, String actor) throws RpcError, org.json.JSONException {
        JSONArray steps = a.optJSONArray("steps");
        if (steps == null) throw new RpcError("BAD_REQUEST", "phone_run needs steps");
        List<String> log = new ArrayList<>();
        long t0 = android.os.SystemClock.elapsedRealtime();
        JSONObject failed = null;
        for (int i = 0; i < steps.length(); i++) {
            JSONObject s = steps.getJSONObject(i);
            int n = i + 1;
            try {
                if (s.has("tap")) {
                    call("phone_tap", new JSONObject().put("target", s.get("tap"))
                            .put("kind", s.optString("kind", "tap")).put("confirm", s.optBoolean("confirm", false)), actor);
                    log.add(n + ". tap " + s.get("tap"));
                } else if (s.has("type")) {
                    JSONObject args = new JSONObject().put("text", s.get("type")).put("replace", s.optBoolean("replace", true))
                            .put("submit", s.optBoolean("submit", false));
                    if (s.has("into")) args.put("into", s.get("into"));
                    if (s.has("at")) args.put("at", s.get("at"));
                    call("phone_type", args, actor);
                    log.add(n + ". type → " + (s.has("into") ? s.get("into") : "focused"));
                } else if (s.has("scroll")) {
                    call("phone_scroll", new JSONObject().put("direction", s.optString("direction", "down"))
                            .put("until", s.optString("until", "")).put("max_swipes", s.optInt("max_swipes", 6)), actor);
                    log.add(n + ". scroll " + s.optString("direction", "down"));
                } else if (s.has("launch")) {
                    call("phone_launch", new JSONObject().put("app", s.get("launch")).put("fresh", s.optBoolean("fresh", false)), actor);
                    log.add(n + ". launch " + s.get("launch"));
                } else if (s.has("button")) {
                    call("phone_button", new JSONObject().put("name", s.get("button")), actor);
                    log.add(n + ". button " + s.get("button"));
                } else if (s.optBoolean("back")) {
                    call("phone_back", new JSONObject(), actor);
                    log.add(n + ". back");
                } else if (s.has("wait")) {
                    Object w = s.get("wait");
                    JSONObject args = new JSONObject();
                    if (w instanceof String) args.put("text", w);
                    else args = (JSONObject) w;
                    call("phone_wait", args, actor);
                    log.add(n + ". wait " + w);
                } else if (s.has("expect")) {
                    synchronized (lock) {
                        if (lastNodes == null) observeLocked(false);
                    }
                    if (findByText(s.getString("expect"), true).isEmpty())
                        throw new RpcError("EXPECT_FAILED", "expected '" + s.get("expect") + "' on screen");
                    log.add(n + ". expect '" + s.get("expect") + "' ✓");
                } else {
                    throw new RpcError("BAD_STEP", "unknown step " + s);
                }
            } catch (RpcError e) {
                failed = new JSONObject().put("step", n).put("error", e.toError());
                log.add(n + ". FAILED (" + e.code + "): " + e.getMessage());
                break;
            }
        }
        StringBuilder sb = new StringBuilder();
        for (String l : log) sb.append(l).append('\n');
        sb.append("---\n");
        synchronized (lock) {
            try {
                observeLocked(false);
            } catch (Exception ignored) {
            }
            sb.append(render(lastNodes, null, false, 150, false, "app: " + lastApp));
        }
        JSONObject out = new JSONObject().put("text", sb.toString())
                .put("ms", android.os.SystemClock.elapsedRealtime() - t0);
        if (failed != null) {
            out.put("isError", true).put("failed_step", failed);
        }
        return out;
    }

    /** phone_screenshot: the guarded fallback image (see Core.screenshotGuarded). */
    private JSONObject toolScreenshot(JSONObject a) throws RpcError, org.json.JSONException {
        boolean grid = a.optBoolean("grid", true);
        JSONArray marks = null;
        if (a.optBoolean("marks", false)) {
            synchronized (lock) {
                observeLocked(true);          // fresh refs: the marks are the refs phone_observe would print now
                marks = marksFromRefs();
            }
        }
        int[] region = null;
        JSONArray ra = a.optJSONArray("region");
        if (ra != null) {
            if (ra.length() != 4) throw new RpcError("BAD_REQUEST", "region is [left, top, right, bottom]");
            String space = a.optString("space", "screen");
            int[] p1 = screenPoint(space, ra.optDouble(0), ra.optDouble(1)), p2 = screenPoint(space, ra.optDouble(2), ra.optDouble(3));
            region = new int[]{Math.min(p1[0], p2[0]), Math.min(p1[1], p2[1]), Math.max(p1[0], p2[0]), Math.max(p1[1], p2[1])};
        }
        JSONObject shot = core.screenshotGuarded(a.optBoolean("force", false), a.optInt("max_px", 1280), grid, marks, region);
        shotW = shot.optInt("w");
        shotH = shot.optInt("h");
        shotOx = shot.optInt("ox");
        shotOy = shot.optInt("oy");
        shotCw = shot.optInt("cw");
        shotCh = shot.optInt("ch");
        boolean zoom = region != null;
        shot.put("text", "screenshot " + shotW + "x" + shotH + " px"
                + (zoom ? " (a zoom of the screen region " + shotOx + "," + shotOy + " to " + (shotOx + shotCw) + "," + (shotOy + shotCh)
                        + ", native detail)" : "")
                + " (the phone screen is " + shot.optInt("sw") + "x" + shot.optInt("sh") + ")"
                + (grid ? " with a labelled coordinate grid in image pixels" : "")
                + (marks != null ? "; boxes are labelled with phone_observe refs, tap them by ref" : "")
                + ". Prefer refs. For a blind tap use phone_tap {x, y, space:\"image\" (these image pixels) or"
                + " \"normalized\" (0-1000 of the whole screen), confirm:true}; its result says whether the pixels changed."
                + (zoom ? "" : " To read small details, call again with region:[left,top,right,bottom] for a zoom."));
        return shot;
    }

    private int shotW = 0, shotH = 0, shotOx = 0, shotOy = 0, shotCw = 0, shotCh = 0;

    /** A point in `space` (screen px | image = the last screenshot's px | normalized 0-1000) as screen pixels,
     *  checked against the screen. */
    private int[] screenPoint(String space, double x, double y) throws RpcError, org.json.JSONException {
        JSONObject display = svc.deviceInfo().optJSONObject("display");
        int sw = display != null ? display.optInt("w") : 0, sh = display != null ? display.optInt("h") : 0;
        if (sw <= 0 || sh <= 0) throw new RpcError("BAD_REQUEST", "screen size unknown");
        double px, py;
        if (space.equals("screen")) {
            px = x;
            py = y;
        } else if (space.equals("normalized")) {
            px = x * sw / 1000.0;
            py = y * sh / 1000.0;
        } else if (space.equals("image")) {
            if (shotW <= 0 || shotH <= 0)
                throw new RpcError("BAD_REQUEST", "space:\"image\" needs a phone_screenshot first (it defines the image pixels)");
            int cw = shotCw > 0 ? shotCw : sw, ch = shotCh > 0 ? shotCh : sh;
            px = shotOx + x * cw / (double) shotW;
            py = shotOy + y * ch / (double) shotH;
        } else {
            throw new RpcError("BAD_REQUEST", "space must be screen, image or normalized");
        }
        if (px < 0 || py < 0 || px > sw || py > sh)
            throw new RpcError("BAD_REQUEST", "the point (" + Math.round(px) + "," + Math.round(py) + ") is outside the screen (" + sw + "x" + sh
                    + "): check the coordinate space");
        return new int[]{(int) Math.round(px), (int) Math.round(py)};
    }

    /** Interactive, visible elements with their bounds and refs, for the Set-of-Marks overlay. */
    private JSONArray marksFromRefs() throws org.json.JSONException {
        JSONArray out = new JSONArray();
        if (lastNodes == null) return out;
        JSONObject display = svc.deviceInfo().optJSONObject("display");
        double screen = display != null ? (double) display.optInt("w") * display.optInt("h") : 0;
        for (int i = 0; i < lastNodes.length(); i++) {
            JSONObject n = lastNodes.getJSONObject(i);
            int f = n.optInt("f");
            JSONArray b = n.optJSONArray("b");
            if ((f & 1024) == 0 || (f & (1 | 8 | 16)) == 0 || b == null || b.length() < 4) continue;
            double area = Math.max(0, b.optInt(2) - b.optInt(0)) * (double) Math.max(0, b.optInt(3) - b.optInt(1));
            if (area <= 0 || (screen > 0 && area > 0.6 * screen)) continue;
            out.put(new JSONObject().put("ref", n.optString("ref")).put("l", b.optInt(0)).put("t", b.optInt(1))
                    .put("r", b.optInt(2)).put("b", b.optInt(3)));
        }
        return out;
    }

    /** x,y given in the last screenshot's pixels or in 0-1000 are converted to screen pixels BEFORE the normal tap
     *  path runs; without a space argument nothing changes. */
    private void convertSpace(JSONObject a) throws RpcError, org.json.JSONException {
        String space = a.optString("space", "screen");
        if (space.equals("screen") || !a.has("x") || !a.has("y")) return;
        int[] p = screenPoint(space, a.optDouble("x"), a.optDouble("y"));
        a.put("x", p[0]);
        a.put("y", p[1]);
    }

    private JSONObject toolStatus() throws RpcError, org.json.JSONException {
        JSONObject st = core.statusJson();
        st.put("on_device", true);
        return new JSONObject().put("text", st.toString(1));
    }

    private static JSONObject textResult(String text) throws org.json.JSONException {
        return new JSONObject().put("text", text);
    }

    private static JSONObject errResult(RpcError e) throws org.json.JSONException {
        JSONObject payload = e.toError();
        return new JSONObject().put("isError", true).put("text", payload.toString())
                .put("code", payload.optString("code"));
    }
}
