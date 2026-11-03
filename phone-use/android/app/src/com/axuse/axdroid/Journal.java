package com.axuse.axdroid;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.io.FileWriter;
import java.io.IOException;
import java.util.ArrayList;
import java.util.List;

/**
 * Persistent activity journal: every tool call, refusal and owner action,
 * with the actor and a target hint — never typed text. Rotates at 512 KB,
 * keeps one generation. Same contract as the axphone journal.
 */
final class Journal {
    private static final long MAX = 512 * 1024;
    private final File file;
    private final Object lock = new Object();

    Journal(android.content.Context ctx) {
        file = new File(ctx.getFilesDir(), "journal.jsonl");
    }

    void append(String type, String tool, boolean ok, String code, String actor, long ms, String target) {
        synchronized (lock) {
            try {
                if (file.exists() && file.length() > MAX) {
                    File old = new File(file.getParentFile(), "journal.1.jsonl");
                    //noinspection ResultOfMethodCallIgnored
                    old.delete();
                    //noinspection ResultOfMethodCallIgnored
                    file.renameTo(old);
                }
                JSONObject o = new JSONObject();
                o.put("ts", System.currentTimeMillis());
                o.put("type", type);
                if (tool != null) o.put("tool", tool);
                o.put("ok", ok);
                if (code != null) o.put("code", code);
                if (actor != null) o.put("actor", actor);
                if (ms > 0) o.put("ms", ms);
                if (target != null && target.length() > 80) target = target.substring(0, 80);
                if (target != null) o.put("target", target);
                FileWriter w = new FileWriter(file, true);
                try {
                    w.write(o.toString() + "\n");
                } finally {
                    w.close();
                }
            } catch (Exception ignored) {
                // the journal must never break an action
            }
        }
    }

    List<JSONObject> tail(int n) {
        List<JSONObject> out = new ArrayList<JSONObject>();
        synchronized (lock) {
            try {
                BufferedReader r = new BufferedReader(new FileReader(file));
                try {
                    List<String> lines = new ArrayList<String>();
                    String line;
                    while ((line = r.readLine()) != null) lines.add(line);
                    for (int i = Math.max(0, lines.size() - n); i < lines.size(); i++) {
                        try {
                            out.add(new JSONObject(lines.get(i)));
                        } catch (Exception ignored) {
                        }
                    }
                } finally {
                    r.close();
                }
            } catch (IOException ignored) {
            }
        }
        return out;
    }
}
