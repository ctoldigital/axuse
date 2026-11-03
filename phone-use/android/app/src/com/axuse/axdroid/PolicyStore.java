package com.axuse.axdroid;

import android.content.Context;
import android.content.SharedPreferences;

import java.security.SecureRandom;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashSet;
import java.util.Set;

/**
 * Owner-held configuration: pairing token, server toggles, guard policy,
 * custody boxes. Stored in SharedPreferences; the token never leaves the
 * device except by the owner reading the screen (or copying it).
 */
final class PolicyStore {
    private static final String TOKEN_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
    private final SharedPreferences sp;

    PolicyStore(Context ctx) {
        sp = ctx.getSharedPreferences("axdroid", Context.MODE_PRIVATE);
        if (!sp.contains("token")) regenToken();
        if (!sp.contains("guard_mode")) sp.edit().putString("guard_mode", "ask").commit();
        if (!sp.contains("server_enabled")) sp.edit().putBoolean("server_enabled", true).commit();
        if (!sp.contains("lan")) sp.edit().putBoolean("lan", false).commit();
        if (!sp.contains("screenshots")) sp.edit().putBoolean("screenshots", true).commit();
    }

    void regenToken() {
        SecureRandom rnd = new SecureRandom();
        StringBuilder s = new StringBuilder(4 + 1 + 4 + 1 + 4 + 1 + 4);
        for (int g = 0; g < 4; g++) {
            if (g > 0) s.append('-');
            for (int i = 0; i < 4; i++) s.append(TOKEN_CHARS.charAt(rnd.nextInt(TOKEN_CHARS.length())));
        }
        sp.edit().putString("token", s.toString()).commit();
    }

    String token() {
        return sp.getString("token", "");
    }

    boolean serverEnabled() {
        return sp.getBoolean("server_enabled", true);
    }

    void setServerEnabled(boolean b) {
        sp.edit().putBoolean("server_enabled", b).commit();
    }

    /** Owner switch: may an agent ask for a screenshot of a screen with no readable text? */
    boolean screenshots() {
        return sp.getBoolean("screenshots", true);
    }

    void setScreenshots(boolean b) {
        sp.edit().putBoolean("screenshots", b).commit();
    }

    boolean lan() {
        return sp.getBoolean("lan", false);
    }

    void setLan(boolean b) {
        sp.edit().putBoolean("lan", b).commit();
    }

    String guardMode() {
        return sp.getString("guard_mode", "ask");
    }

    void setGuardMode(String m) {
        sp.edit().putString("guard_mode", m).commit();
    }

    Set<String> allowApps() {
        return csv("allow_apps");
    }

    Set<String> denyApps() {
        return csv("deny_apps");
    }

    void setAllowApps(Set<String> s) {
        setCsv("allow_apps", s);
    }

    void setDenyApps(Set<String> s) {
        setCsv("deny_apps", s);
    }

    Set<String> allowKinds() {
        String s = sp.getString("allow_kinds", "");
        if (s.isEmpty()) return null;
        return new HashSet<String>(Arrays.asList(s.split(",")));
    }

    void setAllowKinds(Set<String> kinds) {
        sp.edit().putString("allow_kinds", kinds == null ? "" : join(kinds)).commit();
    }

    int maxSessionMin() {
        return sp.getInt("max_session_min", 0);
    }

    int idleMin() {
        return sp.getInt("idle_min", 0);
    }

    void setBoxes(int maxSessionMin, int idleMin) {
        sp.edit().putInt("max_session_min", Math.max(0, maxSessionMin))
                .putInt("idle_min", Math.max(0, idleMin)).commit();
    }

    boolean stopped() {
        return sp.getBoolean("stopped", false);
    }

    // ── cloud relay config (the phone dials out) ────────────────────────
    String relayHost() {
        return sp.getString("relay_host", "");
    }

    String relayId() {
        return sp.getString("relay_id", "");
    }

    String relaySecret() {
        return sp.getString("relay_secret", "");
    }

    boolean relayTls() {
        return sp.getBoolean("relay_tls", false);
    }

    boolean relayEnabled() {
        return sp.getBoolean("relay_enabled", false);
    }

    String stopReason() {
        return sp.getString("stop_reason", "");
    }

    void setStopped(boolean b, String reason) {
        sp.edit().putBoolean("stopped", b).putString("stop_reason", b && reason != null ? reason : "").commit();
    }

    void migrate(Custody custody) {
        // nothing yet; placeholder for future format changes
    }

    private Set<String> csv(String key) {
        String s = sp.getString(key, "");
        Set<String> out = new HashSet<String>();
        if (!s.isEmpty()) out.addAll(Arrays.asList(s.split(",")));
        return out;
    }

    private void setCsv(String key, Set<String> v) {
        sp.edit().putString(key, v == null ? "" : join(v)).commit();
    }

    private static String join(Set<String> v) {
        StringBuilder s = new StringBuilder();
        for (String x : new java.util.TreeSet<String>(v)) {
            if (s.length() > 0) s.append(',');
            s.append(x);
        }
        return s.toString();
    }

    static Set<String> mutable(Set<String> in) {
        return in == null ? new HashSet<String>() : new HashSet<String>(in);
    }

    static Set<String> emptyIfNull(Set<String> in) {
        return in == null ? Collections.<String>emptySet() : in;
    }
}
