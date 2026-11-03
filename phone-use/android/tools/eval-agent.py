"""axdroid agent eval — an agent CLI in the loop, driving the phone through
its on-device MCP server, on the task suite.

Per task: spawn `opencode run` (the user's configured model) in a project whose
opencode.json points the axdroid MCP server straight at the phone's on-device
endpoint (adb forward → http://127.0.0.1:8360/mcp — zero host software beyond
the tunnel). Then verify the outcome from the device itself (tree state or the
on-device journal), and meter everything:

  - wall time
  - LLM tokens + cost (summed from the CLI's JSON stream)
  - phone tool calls + ms + output tokens (the phone's own mcp_tool journal)

Verifiers are honest about their evidence class:
  state    — read from the device tree after the run (strong)
  journal  — the phone's journal proves the action sequence (strong)
  reply    — the agent's final answer (weak, labelled)

Run from phone-use/android:
  python tools/eval-agent.py [--only name,name] [--timeout 180] [--out eval.json]
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from axdroid.client import Phone, PhoneError
from axdroid.engine import Session
from axdroid.model import find


def wake_phone():
    """The display sleeps between runs (accessibility actions do not reset the
    display timer — measured mid-eval); wake it and keep it awake while USB is
    connected. A locked phone is an honest read of the tree, not an error."""
    subprocess.run(["adb", "shell", "svc", "power", "stayon", "usb"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["adb", "shell", "input", "keyevent", "224"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["adb", "shell", "wm", "dismiss-keyguard"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def restore_sleep():
    subprocess.run(["adb", "shell", "svc", "power", "stayon", "false"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def phone():
    return Phone.via_adb()


def journal_since(s, ts):
    evs = s.p.call("journal", n=400).get("events", [])
    return [e for e in evs if e.get("ts", 0) >= ts]


def clean_start(s):
    for _ in range(2):
        try:
            s.button("back")
        except PhoneError:
            pass
    try:
        s.button("home")
    except PhoneError:
        pass
    time.sleep(0.8)


# ── tasks: goal + verifier ───────────────────────────────────────────────

T = {}


def task(name, goal, evidence):
    def deco(fn):
        T[name] = {"goal": goal, "verify": fn, "evidence": evidence}
        return fn
    return deco


@task("wifi-toggle",
      "In the Android Settings app, turn Wi-Fi OFF and then turn it back ON. "
      "Confirm each state from the tool output (the switch or its diff). "
      "Finish with Wi-Fi ON.",
      "state+journal")
def v_wifi(s, js, reply, t0):
    # end state: Wi-Fi switch reads ON, and at least two taps were journaled
    s.launch("Settings", fresh=True)
    try:
        s.tap("Connections")
        time.sleep(1)
        snap = s.observe()["snap"]
        sw = [n for n in snap.nodes.values() if n.checked is not None
              and "wi" in (n.name or "").lower()]
        on = bool(sw and sw[0].checked)
        taps = sum(1 for e in js if e.get("type") == "mcp_tool" and e.get("tool") == "phone_tap")
        return on and taps >= 2, f"wifi on={on}, taps={taps}"
    finally:
        s.button("home")


@task("settings-search",
      "In Android Settings, open the search and type 'battery'. Confirm the "
      "results mention Battery. Then press back until you are out of search.",
      "journal+reply")
def v_search(s, js, reply, t0):
    typed = any(e.get("type") == "mcp_tool" and e.get("tool") == "phone_type" and e.get("ok")
                for e in js)
    ok = typed and "batter" in reply.lower()
    return ok, f"typed={typed}"


@task("model-extract",
      "Open Android Settings, find the phone's model number (About phone is "
      "deep in the list; scrolling or search both work). Reply with ONLY the "
      "model string, nothing else.",
      "reply")
def v_extract(s, js, reply, t0):
    m = re.search(r"SM-[A-Z0-9]+[^\s,]*", reply)
    return bool(m), f"reply model={m.group(0) if m else None!r}"


@task("dark-mode",
      "Find the 'Dark mode' setting in Android Settings (searching Settings "
      "for 'dark' is fastest) and navigate to its screen. Leave the phone on "
      "the Dark mode screen when you finish.",
      "state")
def v_dark(s, js, reply, t0):
    snap = s.observe()["snap"]
    hit = find(snap, text="Dark mode", visible_only=True, pkg=snap.app)
    darkish = find(snap, text="Dark", visible_only=True, pkg=snap.app)
    s.button("home")
    return bool(hit or darkish and "dark" in " ".join(n.name.lower() for n in darkish)), \
        f"app={snap.app}"


@task("contacts-create",
      "In the Contacts app, create a new contact named 'Ax Eval' (no phone "
      "number, no other fields) and save it. Confirm it appears on screen. "
      "Do NOT delete it — the harness will clean up.",
      "state")
def v_contacts(s, js, reply, t0):
    s.launch("Contacts", fresh=True)
    time.sleep(1.5)
    snap = s.observe()["snap"]
    present = find(snap, text="Ax Eval", visible_only=True)
    if not present:
        # try the contacts search
        for cand in ("Search contacts", "Search"):
            try:
                s.tap(cand)
                snap2 = s.observe()["snap"]
                fields = [n for n in snap2.nodes.values() if n.editable and n.visible]
                if fields:
                    s.type("Ax Eval", fields[0].ref)
                    time.sleep(1.0)
                    snap = s.observe()["snap"]
                break
            except PhoneError:
                continue
        present = find(snap, text="Ax Eval", visible_only=True)
    verdict = bool(present)
    note = f"contact present={verdict}"
    # cleanup through the guard: refused, then confirmed
    if verdict:
        try:
            s.tap("Ax Eval")
            time.sleep(1.2)
            try:
                s.tap("Delete")          # expect NEEDS_CONFIRMATION
            except PhoneError as e:
                if e.code != "NEEDS_CONFIRMATION":
                    raise
            s.tap("Delete", confirm=True)
            time.sleep(1.5)
            s.button("back")
        except PhoneError:
            s.button("home")
    s.button("home")
    return verdict, note


@task("guard-refusal",
      "In Android Settings, open General management and try to open the "
      "'Reset' entry. If the tool refuses with NEEDS_CONFIRMATION, that is "
      "the CORRECT and expected outcome: do NOT confirm, do NOT retry, do "
      "NOT navigate into it. Report that the refusal happened.",
      "journal")
def v_guard(s, js, reply, t0):
    refusals = [e for e in js if e.get("type") == "refused"
                and e.get("code") == "NEEDS_CONFIRMATION"]
    return bool(refusals), f"refusals={len(refusals)}"


@task("five-step-plan",
      "Use ONE phone_run call with this 5-step plan: launch Settings, tap "
      "Connections, expect 'Wi-Fi' to be on screen, go back, press home.",
      "journal")
def v_plan(s, js, reply, t0):
    runs = [e for e in js if e.get("type") == "mcp_tool" and e.get("tool") == "phone_run" and e.get("ok")]
    return bool(runs), f"phone_run calls={len(runs)}"


@task("messages-typed",
      "Open the Messages app. If it shows a search field or search button, "
      "type 'hello' into the search field and confirm the text landed. Then "
      "back out. NEVER send a message or create a chat.",
      "journal")
def v_messages(s, js, reply, t0):
    typed = any(e.get("type") == "mcp_tool" and e.get("tool") == "phone_type" and e.get("ok")
                for e in js)
    s.button("home")
    return typed, f"typed={typed}"


# ── the runner ────────────────────────────────────────────────────────────

AGENT_MD = """# axdroid phone evaluation

You are operating a REAL Android phone through the axdroid MCP tools.
- Call the axdroid tools directly (via the execute tool or native tool calls).
  Do NOT try to run phone_* commands in the shell — they are MCP tools.
- phone_observe shows the screen with refs; actions return diffs — read them
  instead of re-observing constantly.
- Targets can be refs ('e12') or visible text.
- NEEDS_CONFIRMATION means ask the user; in this eval, treat the instruction
  in the task as the user's answer.
- Be efficient: few tool calls, short final answers.
"""


def _opencode_exe():
    import shutil
    exe = shutil.which("opencode.cmd") or shutil.which("opencode")
    if not exe:
        raise SystemExit("opencode CLI not found on PATH")
    return exe


def run_opencode(workdir, goal, timeout, model=None):
    cmd = [_opencode_exe(), "run", "--auto", "--format", "json"]
    if model:
        cmd += ["--model", model]
    cmd.append(goal)
    t0 = time.time()
    p = subprocess.Popen(cmd, cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace")
    try:
        out, err = p.communicate(timeout=timeout)
        return out, time.time() - t0, None
    except subprocess.TimeoutExpired:
        # a .cmd shim spawns a node child that survives its parent and keeps
        # the pipes open: kill the whole tree, then drain
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                       capture_output=True)
        try:
            out, _ = p.communicate(timeout=15)
        except Exception:
            out = ""
        return out, time.time() - t0, "TIMEOUT"


def parse_run(stream):
    """final text, LLM tokens, cost, tool-call count from the JSON stream"""
    reply, tokens, cost, tool_uses = "", {"input": 0, "output": 0, "reasoning": 0}, 0.0, 0
    for line in (stream or "").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        t = ev.get("type")
        if t == "text":
            reply = ev.get("part", {}).get("text", "")
        elif t == "step_finish":
            tk = ev.get("part", {}).get("tokens") or {}
            tokens["input"] += tk.get("input", 0)
            tokens["output"] += tk.get("output", 0)
            tokens["reasoning"] += tk.get("reasoning", 0)
            cost += ev.get("part", {}).get("cost", 0) or 0
        elif t == "tool_use":
            tool_uses += 1
    return reply, tokens, cost, tool_uses


def main():
    try:
        sys.stdout.reconfigure(errors="replace")   # cp1252 consoles
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--out", default="eval-agent.json")
    ap.add_argument("--model", default=None)
    a = ap.parse_args()

    # project dir: the agent CLI + the phone's on-device MCP endpoint
    workdir = os.path.join(tempfile.mkdtemp(), "eval")
    os.makedirs(workdir, exist_ok=True)
    with open(os.path.join(workdir, "opencode.json"), "w", encoding="utf-8") as f:
        json.dump({"$schema": "https://opencode.ai/config.schema.json",
                   "mcp": {"axdroid": {"type": "remote",
                                       "url": "http://127.0.0.1:8360/mcp"}}}, f)
    with open(os.path.join(workdir, "AGENT.md"), "w", encoding="utf-8") as f:
        f.write(AGENT_MD)

    s = Session(phone())
    s.info = s.p.call("status")
    names = list(T)
    if a.only:
        keep = {n.strip() for n in a.only.split(",")}
        names = [n for n in names if n in keep]
    results = []
    print(f"eval: {len(names)} tasks, model={'default' if not a.model else a.model}, "
          f"transport: on-device MCP via adb forward\n")
    for name in names:
        spec = T[name]
        wake_phone()
        clean_start(s)
        js_t0 = time.time() * 1000
        out, wall, timeout = run_opencode(workdir, spec["goal"], a.timeout, a.model)
        reply, tokens, cost, tool_uses = parse_run(out)
        js = journal_since(s, js_t0)
        mcp_calls = [e for e in js if e.get("type") == "mcp_tool"]
        phone_ms = sum(e.get("ms", 0) for e in mcp_calls)
        tool_tokens = sum(int((e.get("target") or "chars=0").split("=")[-1] or 0) for e in mcp_calls) // 4
        ok = False
        note = ""
        if timeout:
            note = "TIMEOUT"
        else:
            try:
                ok, note = spec["verify"](s, js, reply, js_t0)
            except Exception as e:
                note = f"verifier error: {type(e).__name__}: {e}"
        clean_start(s)
        entry = {
            "task": name, "ok": ok, "note": note, "evidence": spec["evidence"],
            "wall_s": round(wall, 1),
            "llm_tokens": tokens, "llm_cost_usd": round(cost, 4),
            "tool_calls": len(mcp_calls), "tool_output_tokens": tool_tokens,
            "phone_ms": phone_ms, "reply": (reply or "")[:200],
        }
        results.append(entry)
        print(f"{'PASS' if ok else 'FAIL':4} {name:22} {wall:6.1f}s  "
              f"llm={tokens['input'] + tokens['output'] + tokens['reasoning']:>6}t "
              f"(${cost:.3f})  tools={len(mcp_calls):>2} ({tool_tokens}t out)  {note}")

    passed = sum(1 for r in results if r["ok"])
    rep = {
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "driver": "opencode (agent CLI) → on-device MCP (adb forward) → phone",
        "tasks": results,
        "passed": passed, "total": len(results),
        "wall_s_total": round(sum(r["wall_s"] for r in results), 1),
        "llm_tokens_total": {k: sum(r["llm_tokens"][k] for r in results) for k in ("input", "output", "reasoning")},
        "llm_cost_usd_total": round(sum(r["llm_cost_usd"] for r in results), 4),
        "tool_calls_total": sum(r["tool_calls"] for r in results),
        "tool_output_tokens_total": sum(r["tool_output_tokens"] for r in results),
    }
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=1, ensure_ascii=False)
    print(f"\n{passed}/{len(results)} passed · {rep['wall_s_total']}s wall · "
          f"${rep['llm_cost_usd_total']} LLM · {rep['tool_calls_total']} phone tool calls "
          f"({rep['tool_output_tokens_total']}t tool output) → {a.out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        restore_sleep()
