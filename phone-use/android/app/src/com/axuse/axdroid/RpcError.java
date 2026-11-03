package com.axuse.axdroid;

import org.json.JSONObject;

/** A device-side error with a machine code — always safe to return, never retried blindly. */
final class RpcError extends Exception {
    final String code;
    final JSONObject facts = new JSONObject();

    RpcError(String code, String message) {
        super(message);
        this.code = code;
    }

    RpcError(String code, String message, String key, Object value) {
        this(code, message);
        try {
            facts.put(key, value);
        } catch (Exception ignored) {
        }
    }

    JSONObject toError() {
        JSONObject o = new JSONObject();
        try {
            o.put("code", code);
            o.put("message", getMessage());
            JSONObject f = facts;
            if (f.length() > 0) o.put("facts", f);
        } catch (Exception ignored) {
        }
        return o;
    }
}
