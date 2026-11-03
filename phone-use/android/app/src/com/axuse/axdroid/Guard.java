package com.axuse.axdroid;

import org.json.JSONObject;

import java.util.regex.Pattern;

/**
 * Consequential-action guard â€” the device, not the agent, decides.
 *
 * Port of the axphone policy (same rules, adapted labels for Android):
 * a click/long-click whose target looks like money movement, sending,
 * deleting, or an irreversible confirmation needs confirm:true from the
 * caller. App content is untrusted data: a label can only trigger the
 * policy, never relax it. Declines ("No thanks", "Cancel", "Not now") are
 * safe â€” except cancelling something that costs or loses something.
 */
final class Guard {
    private static final Object[][] RISK = {
            {"payment", "(?i)\\b(pay|buy|purchase|checkout|place order|order now|subscribe|confirm (payment|purchase|order)|send money|transfer|donate|tip|book now|rent|renew)\\b|(?i)google pay|samsung pay|apple pay"},
            {"send", "(?i)\\b(send|post|publish|tweet|reply all|share|submit|call|dial)\\b"},
            {"delete", "(?i)\\b(delete|remove|erase|discard|clear all|reset|unsubscribe|cancel subscription|deactivate|close account|sign out|log out|log out|uninstall|block|factory reset|erase all)\\b"},
            {"permission", "(?i)\\b(allow|always allow|trust|grant|install|enable installation)\\b"},
    };
    private static final Pattern[] RISK_RE = new Pattern[RISK.length];
    private static final String[] RISK_KIND = new String[RISK.length];

    static {
        for (int i = 0; i < RISK.length; i++) {
            RISK_KIND[i] = (String) RISK[i][0];
            RISK_RE[i] = Pattern.compile((String) RISK[i][1]);
        }
    }

    // Apps where *every* write-class action is consequential regardless of label.
    private static final String[] SENSITIVE_APPS = {"com.android.dialer", "com.samsung.android.dialer", "com.android.contacts"};
    private static final String SENSITIVE_KIND = "send";

    private static final Pattern SAFE_RE = Pattern.compile(
            "^\\s*(don'?t|do not|not now|no thanks|no|deny|decline|never|later|skip|close|dismiss|ask (?:(?:app|me) )?not to|"
                    // Android's SearchView labels its search button "Submit query":
                    // it runs a search, it sends nothing
                    + "submit query|"
                    + "cancel(?! (?:subscription|membership|order|plan|account|trip|booking|reservation)))\\b", Pattern.CASE_INSENSITIVE);

    static String classify(String name, String pkg, String role) {
        if (name == null) return null;
        String text = name.replaceAll("\\s+", " ").trim();
        if (text.isEmpty()) return null;
        text = text.replace('\u2019', '\'').replace('\u2018', '\'');   // Don’t → Don't
        if ("textfield".equals(role) || "searchfield".equals(role) || "textarea".equals(role)
                || "text".equals(role) || "heading".equals(role) || "switch".equals(role)) return null;
        if (SAFE_RE.matcher(text).find()) return null;
        for (int i = 0; i < RISK_RE.length; i++) {
            if (RISK_RE[i].matcher(text).find()) return RISK_KIND[i];
        }
        if ("button".equals(role) || "cell".equals(role) || "link".equals(role) || "image-button".equals(role)) {
            for (String p : SENSITIVE_APPS) {
                if (p.equals(pkg)) return SENSITIVE_KIND;
            }
        }
        return null;
    }

    /** null = proceed. Otherwise a refusal object the host must surface. */
    static JSONObject evaluate(PolicyStore policy, String name, String pkg, String role, boolean confirm) throws org.json.JSONException {
        String kind = classify(name, pkg, role);
        if (kind == null) return null;
        if ("allow".equals(policy.guardMode())) return null;
        if (policy.allowKinds() != null && policy.allowKinds().contains(kind)) return null;
        if ("block".equals(policy.guardMode())) {
            JSONObject o = new JSONObject();
            o.put("code", "BLOCKED_BY_POLICY");
            o.put("kind", kind);
            o.put("element", name);
            o.put("message", "'" + name + "' is a " + kind + " action and the operator's policy blocks it.");
            return o;
        }
        if (!confirm) {
            JSONObject o = new JSONObject();
            o.put("code", "NEEDS_CONFIRMATION");
            o.put("kind", kind);
            o.put("element", name);
            o.put("message", "'" + name + "' looks like a " + kind + " action. Ask the user, then repeat the call with confirm:true.");
            return o;
        }
        return null;
    }
}

