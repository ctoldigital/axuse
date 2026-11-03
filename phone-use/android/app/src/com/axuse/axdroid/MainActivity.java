package com.axuse.axdroid;

import android.accessibilityservice.AccessibilityServiceInfo;
import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.view.Gravity;
import android.view.accessibility.AccessibilityManager;
import android.widget.Button;
import android.widget.CompoundButton;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

import java.util.List;

/**
 * Owner console: pairing token, server/LAN toggles, the stop latch, and the
 * one step we cannot do for the owner — turning the accessibility service on.
 * Plain views, no dependencies; the agent never needs this screen.
 */
public class MainActivity extends Activity {
    private final Handler handler = new Handler();
    private TextView statusText;
    private TextView tokenText;
    private Switch lanSwitch;
    private Switch serverSwitch;
    private final Runnable refresher = new Runnable() {
        @Override
        public void run() {
            refresh();
            handler.postDelayed(this, 1000);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        ScrollView scroll = new ScrollView(this);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (16 * getResources().getDisplayMetrics().density);
        root.setPadding(pad, pad, pad, pad);
        scroll.addView(root);
        LinearLayout.LayoutParams w = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);

        TextView title = new TextView(this);
        title.setText("axuse axdroid — agent bridge");
        title.setTextSize(20);
        title.setPadding(0, 0, 0, 8);
        root.addView(title);

        statusText = new TextView(this);
        statusText.setTextSize(14);
        statusText.setPadding(0, 0, 0, 12);
        root.addView(statusText);

        Button enable = new Button(this);
        enable.setText("1 · Enable the accessibility service");
        enable.setOnClickListener(v -> {
            try {
                startActivity(new Intent("android.settings.ACCESSIBILITY_SETTINGS"));
            } catch (Exception e) {
                toast("open Settings → Accessibility → Installed apps → axdroid");
            }
        });
        root.addView(enable);

        serverSwitch = new Switch(this);
        serverSwitch.setText("2 · RPC server on port 8360");
        serverSwitch.setOnCheckedChangeListener(new CompoundButton.OnCheckedChangeListener() {
            @Override
            public void onCheckedChanged(CompoundButton b, boolean on) {
                prefs().setServerEnabled(on);
                AxService s = AxService.instance;
                if (s != null) {
                    if (on) s.startServer();
                    else s.stopServer();
                    s.postStatusNotification();
                }
                refresh();
            }
        });
        root.addView(serverSwitch);

        lanSwitch = new Switch(this);
        lanSwitch.setText("Allow LAN connections (token required)");
        lanSwitch.setOnCheckedChangeListener(new CompoundButton.OnCheckedChangeListener() {
            @Override
            public void onCheckedChanged(CompoundButton b, boolean on) {
                prefs().setLan(on);
                AxService s = AxService.instance;
                if (s != null && prefs().serverEnabled()) {
                    s.stopServer();
                    s.startServer();
                }
                refresh();
            }
        });
        root.addView(lanSwitch);
        Switch shotSwitch = new Switch(this);
        shotSwitch.setText("Allow screenshots (only for screens with no readable text)");
        shotSwitch.setChecked(prefs().screenshots());
        shotSwitch.setOnCheckedChangeListener((CompoundButton b, boolean on) -> prefs().setScreenshots(on));
        root.addView(shotSwitch);

        TextView tokenLabel = new TextView(this);
        tokenLabel.setText("Pairing token (tap to copy — needed for LAN only)");
        tokenLabel.setPadding(0, pad, 0, 4);
        root.addView(tokenLabel);

        tokenText = new TextView(this);
        tokenText.setTypeface(android.graphics.Typeface.MONOSPACE);
        tokenText.setTextSize(18);
        tokenText.setGravity(Gravity.CENTER);
        tokenText.setPadding(0, pad / 2, 0, pad);
        tokenText.setOnClickListener(v -> {
            ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
            if (cm != null) cm.setPrimaryClip(ClipData.newPlainText("axdroid token", prefs().token()));
            toast("token copied");
        });
        root.addView(tokenText);

        Button reset = new Button(this);
        reset.setText("Reset token");
        reset.setOnClickListener(v -> {
            prefs().regenToken();
            refresh();
        });
        root.addView(reset);

        Button stop = new Button(this);
        stop.setText("Stop agent now (reads still work)");
        stop.setOnClickListener(v -> {
            AxService s = AxService.instance;
            if (s == null) {
                toast("service not running");
                return;
            }
            s.custody.stop("app");
            s.postStatusNotification();
            refresh();
        });
        root.addView(stop);

        Button resume = new Button(this);
        resume.setText("Resume agent");
        resume.setOnClickListener(v -> {
            AxService s = AxService.instance;
            if (s == null) {
                toast("service not running");
                return;
            }
            s.custody.resume();
            s.postStatusNotification();
            refresh();
        });
        root.addView(resume);

        // ── cloud relay: the phone dials out; nothing is opened locally ──
        TextView relayTitle = new TextView(this);
        relayTitle.setText("Cloud relay (the phone dials out — no ports opened)");
        relayTitle.setPadding(0, pad, 0, 4);
        root.addView(relayTitle);

        final android.widget.EditText relayHost = new android.widget.EditText(this);
        relayHost.setHint("relay host:port (e.g. relay.example.com:7000)");
        relayHost.setText(prefs().relayHost());
        root.addView(relayHost);

        LinearLayout ids = new LinearLayout(this);
        ids.setOrientation(LinearLayout.HORIZONTAL);
        final android.widget.EditText relayId = new android.widget.EditText(this);
        relayId.setHint("phone id");
        relayId.setText(prefs().relayId());
        LinearLayout.LayoutParams half = new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        ids.addView(relayId, half);
        final android.widget.EditText relaySecret = new android.widget.EditText(this);
        relaySecret.setHint("secret");
        relaySecret.setText(prefs().relaySecret());
        ids.addView(relaySecret, half);
        root.addView(ids);

        Switch relayTls = new Switch(this);
        relayTls.setText("Relay uses TLS");
        relayTls.setChecked(prefs().relayTls());
        root.addView(relayTls);

        Switch relayEnabled = new Switch(this);
        relayEnabled.setText("Connect to the relay (reconnects automatically)");
        relayEnabled.setChecked(prefs().relayEnabled());
        relayEnabled.setOnCheckedChangeListener((CompoundButton b, boolean on) -> {
            android.content.SharedPreferences sp = getSharedPreferences("axdroid", MODE_PRIVATE);
            sp.edit().putString("relay_host", relayHost.getText().toString().trim())
                    .putString("relay_id", relayId.getText().toString().trim())
                    .putString("relay_secret", relaySecret.getText().toString().trim())
                    .putBoolean("relay_tls", relayTls.isChecked())
                    .putBoolean("relay_enabled", on).commit();
            AxService s = AxService.instance;
            if (s != null) s.startRelayIfNeeded();
            refresh();
        });
        root.addView(relayEnabled);

        TextView help = new TextView(this);
        help.setTextSize(12);
        help.setPadding(0, pad, 0, pad);
        help.setText("Agents connect with adb:  adb forward tcp:8360 tcp:8360\n"
                + "then http://127.0.0.1:8360 (loopback needs no token) — or MCP\n"
                + "directly at http://127.0.0.1:8360/mcp (the phone answers itself).\n"
                + "On Wi-Fi use http://<phone-ip>:8360 with the token above.\n"
                + "Cloud agents: connect the relay above, then approve them with\n"
                + "the link code from 'axdroid link' or this app.\n\n"
                + "The agent reads the accessibility tree and acts with\n"
                + "semantic actions. A screenshot is taken only when the\n"
                + "screen has no readable text (switch above to forbid).\n"
                + "Every action is journaled on the phone.");
        root.addView(help);

        setContentView(scroll);
        if (android.os.Build.VERSION.SDK_INT >= 33) {
            requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 1);
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        handler.post(refresher);
    }

    @Override
    protected void onPause() {
        super.onPause();
        handler.removeCallbacks(refresher);
    }

    private PolicyStore prefs() {
        return AxService.instance != null ? AxService.instance.policy : new PolicyStore(this);
    }

    private boolean serviceEnabled() {
        AccessibilityManager am = (AccessibilityManager) getSystemService(Context.ACCESSIBILITY_SERVICE);
        if (am == null) return false;
        List<AccessibilityServiceInfo> list = am.getEnabledAccessibilityServiceList(AccessibilityServiceInfo.FEEDBACK_GENERIC);
        for (AccessibilityServiceInfo i : list) {
            if (i.getId() != null && i.getId().contains("com.axuse.axdroid")) return true;
        }
        return false;
    }

    private void refresh() {
        boolean on = serviceEnabled();
        AxService s = AxService.instance;
        PolicyStore p = prefs();
        StringBuilder sb = new StringBuilder();
        sb.append("Accessibility service: ").append(on ? "ON" : "OFF — tap the button above").append('\n');
        if (s != null) {
            sb.append("Server: ").append(p.serverEnabled() ? (p.lan() ? "LAN 0.0.0.0:8360" : "localhost:8360") : "off").append('\n');
            sb.append("Screen changes seen: ").append(s.changeCount.get()).append('\n');
            sb.append("Relay: ");
            if (p.relayEnabled()) {
                sb.append(p.relayHost()).append(s.relay != null && s.relay.isRunning()
                        ? " · connected" : " · connecting…");
            } else {
                sb.append("off");
            }
            sb.append('\n');
            sb.append("Custody: ").append(s.custody.facts());
        } else {
            sb.append("Bridge: waiting for the service…");
        }
        statusText.setText(sb.toString());
        tokenText.setText(p.token());
        lanSwitch.setOnCheckedChangeListener(null);
        serverSwitch.setOnCheckedChangeListener(null);
        lanSwitch.setChecked(p.lan());
        serverSwitch.setChecked(p.serverEnabled());
        wire(lanSwitch, serverSwitch);
    }

    private void wire(Switch lan, Switch server) {
        server.setOnCheckedChangeListener((CompoundButton b, boolean on) -> {
            prefs().setServerEnabled(on);
            AxService sv = AxService.instance;
            if (sv != null) {
                if (on) sv.startServer();
                else sv.stopServer();
                sv.postStatusNotification();
            }
            refresh();
        });
        lan.setOnCheckedChangeListener((CompoundButton b, boolean on) -> {
            prefs().setLan(on);
            AxService sv = AxService.instance;
            if (sv != null && prefs().serverEnabled()) {
                sv.stopServer();
                sv.startServer();
            }
            refresh();
        });
    }

    private void toast(String s) {
        Toast.makeText(this, s, Toast.LENGTH_SHORT).show();
    }
}
