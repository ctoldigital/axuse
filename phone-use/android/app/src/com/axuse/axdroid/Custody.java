package com.axuse.axdroid;

/**
 * Owner custody: the stop latch outranks every agent on every transport.
 *
 * stop() persists across service restarts. Reading keeps working; acting
 * refuses with STOPPED_BY_OWNER until resume(). Session and idle boxes bound
 * a leaked token or a runaway loop (0 = unlimited), mirroring axphone.
 */
final class Custody {
    private final PolicyStore policy;
    private long startedAt = System.currentTimeMillis();
    private volatile long lastActivityAt = System.currentTimeMillis();

    Custody(PolicyStore policy) {
        this.policy = policy;
    }

    void stop(String reason) {
        policy.setStopped(true, reason);
    }

    void resume() {
        policy.setStopped(false, null);
        startedAt = System.currentTimeMillis();
        touch();
    }

    boolean isStopped() {
        return policy.stopped();
    }

    void touch() {
        lastActivityAt = System.currentTimeMillis();
    }

    /** null = fine. Otherwise a refusal code (STOPPED_BY_OWNER, SESSION_EXPIRED, IDLE_TIMEOUT). */
    String check(boolean act) {
        if (!act) return null;
        if (policy.stopped()) return "STOPPED_BY_OWNER";
        int maxMin = policy.maxSessionMin();
        if (maxMin > 0 && (System.currentTimeMillis() - startedAt) > maxMin * 60_000L) return "SESSION_EXPIRED";
        int idleMin = policy.idleMin();
        if (idleMin > 0 && (System.currentTimeMillis() - lastActivityAt) > idleMin * 60_000L) return "IDLE_TIMEOUT";
        return null;
    }

    String facts() {
        return (policy.stopped() ? "stopped(" + policy.stopReason() + ")" : "running")
                + " session_min=" + (System.currentTimeMillis() - startedAt) / 60000
                + " max_session_min=" + policy.maxSessionMin()
                + " idle_min=" + policy.idleMin();
    }
}
