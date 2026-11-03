"""The AndroidWorld environment backed by axdroid.

`AxdroidController` replaces android_world's gRPC-based
AndroidWorldController: UI state comes from the axdroid accessibility service
running on the emulator (semantic tree, no screenshots), while adb-side
requests fall through to `LocalAdbEnv`. `AxdroidWorldEnv` implements the
AsyncEnv contract the task suite talks to.
"""

from __future__ import annotations

import io
import re
import time

from android_world.env import adb_utils
from android_world.env import interface
from android_world.env import json_action
from android_world.env import representation_utils
from android_world.utils import file_utils

from axdroid.client import Phone, PhoneError
from axdroid.engine import Session
from axdroid.model import F_CHECKABLE, F_FOCUSABLE, F_FOCUSED, F_SCROLL, Node

from .adb_bridge import LocalAdbEnv

_AX_SERVICE = "com.axuse.axdroid/com.axuse.axdroid.AxService"


def _node_to_ui_element(n: Node) -> representation_utils.UIElement:
    bbox = None
    if n.bounds:
        l, t, r, b = n.bounds
        bbox = representation_utils.BoundingBox(x_min=l, x_max=r, y_min=t, y_max=b)
    flags = n.flags
    return representation_utils.UIElement(
        text=n.text or "",
        content_description=n.desc or "",
        class_name=n.cls,
        resource_id=n.rid,
        resource_name=n.rid,
        hint_text=getattr(n, "hint", None),
        bbox=bbox,
        bbox_pixels=bbox,
        is_checkable=bool(flags & F_CHECKABLE),
        is_checked=n.checked,
        is_clickable=n.clickable,
        is_editable=n.editable,
        is_enabled=n.enabled,
        is_focusable=bool(flags & F_FOCUSABLE),
        is_focused=bool(flags & F_FOCUSED),
        is_long_clickable=None,
        is_scrollable=bool(flags & F_SCROLL),
        is_selected=n.selected,
        is_visible=True,
        package_name=(n.win or {}).get("pkg"),
        metadata={"ref": n.ref, "node_id": n.id},
    )


class AxdroidController:
    """AndroidWorldController work-alike: axdroid for a11y, LocalAdbEnv for adb."""

    def __init__(self, bridge: LocalAdbEnv, session: Session):
        self._bridge = bridge
        self._s = session
        self._last_els: list[representation_utils.UIElement] = []

    # -- AndroidWorldController API ------------------------------------

    @property
    def env(self) -> LocalAdbEnv:
        return self._bridge

    @property
    def session(self) -> Session:
        return self._s

    def __getattr__(self, name):
        # upstream's controller delegates to its wrapped env (e.g.
        # execute_adb_call); keep that contract for adb_utils callers
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._bridge, name)

    def ensure_service(self) -> None:
        """(Re)enable the axdroid accessibility service if it went away —
        uiautomator dump (used by some benchmark setup helpers) can evict
        third-party a11y services."""
        try:
            self._s.p.call("status")
            return
        except PhoneError:
            pass
        self._bridge.shell("settings", "--user", "0", "put", "secure",
                           "enabled_accessibility_services", _AX_SERVICE)
        self._bridge.shell("settings", "--user", "0", "put", "secure",
                           "accessibility_enabled", "1")
        time.sleep(1.5)

    def get_ui_elements(self) -> list[representation_utils.UIElement]:
        els, _ = self._observe()
        self._last_els = els
        return els

    def _observe(self):
        """One axdroid snapshot → (UIElements, snapshot)."""
        self.ensure_service()
        for attempt in range(3):
            try:
                snap = self._s.observe()["snap"]
                break
            except PhoneError:
                if attempt == 2:
                    raise
                self.ensure_service()
        els = []
        for nid in snap.order:
            n = snap.nodes.get(nid)
            if n is None or not n.visible:
                continue
            els.append(_node_to_ui_element(n))
        return els, snap

    def forest_from_snapshot(self):
        """Synthesize the android_env a11y forest proto from the axdroid
        tree — some rewards (ContactsNewContactDraft) read state.forest
        directly and crashed on None."""
        from android_env.proto.a11y import android_accessibility_forest_pb2 as fb

        _, snap = self._observe()
        forest = fb.AndroidAccessibilityForest()
        # group nodes by window id
        by_win: dict = {}
        for nid in snap.order:
            n = snap.nodes.get(nid)
            if n is None:
                continue
            by_win.setdefault(getattr(n, "win", {}).get("id", "w0"), []).append(n)

        def fill_node(nd, n):
            nd.text = n.text or ""
            nd.content_description = n.desc or ""
            nd.class_name = n.cls or ""
            nd.view_id_resource_name = n.rid or ""
            nd.package_name = (n.win or {}).get("pkg", "")
            hint = getattr(n, "hint", None)
            if hint:
                nd.hint_text = hint
            if n.bounds:
                l, t, r, b = n.bounds
                nd.bounds_in_screen.left = l
                nd.bounds_in_screen.top = t
                nd.bounds_in_screen.right = r
                nd.bounds_in_screen.bottom = b
            nd.is_checked = bool(n.checked)
            nd.is_checkable = bool(n.flags & F_CHECKABLE)
            nd.is_clickable = n.clickable
            nd.is_editable = n.editable
            nd.is_enabled = n.enabled
            nd.is_focused = bool(n.flags & F_FOCUSED)
            nd.is_focusable = bool(n.flags & F_FOCUSABLE)
            nd.is_scrollable = bool(n.flags & F_SCROLL)
            nd.is_selected = n.selected
            nd.is_visible_to_user = n.visible

        for win_id, nodes in by_win.items():
            window = forest.windows.add()
            window.id = win_id if isinstance(win_id, int) else abs(hash(win_id)) % (2 ** 31)
            for n in nodes:
                fill_node(window.tree.nodes.add(), n)
        return forest

    def reset(self):
        self.get_ui_elements()

    def step(self, action=None):
        self.get_ui_elements()

    @property
    def device_screen_size(self) -> tuple[int, int]:
        out = self._bridge.shell("wm", "size")
        m = re.search(r"(\d+)x(\d+)", out)
        return (int(m.group(1)), int(m.group(2))) if m else (1080, 2340)

    @property
    def logical_screen_size(self) -> tuple[int, int]:
        return self.device_screen_size

    def pull_file(self, remote_db_file_path: str, timeout_sec=None):
        return file_utils.tmp_directory_from_device(
            remote_db_file_path.rsplit("/", 1)[0], self.env, timeout_sec)

    def push_file(self, local_db_file_path, remote_db_file_path, timeout_sec=None):
        """Push a DB file so the *app* can actually use it.

        adb push writes as root:root and leaves the app's stale -wal/-shm
        siblings in place — SQLite then replays the old WAL over the freshly
        seeded DB and the app silently reverts to an empty state. Make the
        push atomic: force-stop the app, push, drop stale WAL siblings,
        hand ownership to the app uid, restore the SELinux context.
        """
        b = self._bridge
        seg = remote_db_file_path.split("/")
        pkg = None
        if len(seg) > 5 and seg[1] == "data":
            pkg = seg[4] if seg[2] == "user" else seg[3]
        if pkg:
            b.shell("am", "force-stop", pkg)
        file_utils.copy_data_to_device(
            local_db_file_path, remote_db_file_path, self.env, timeout_sec)
        b.shell("rm", "-f", remote_db_file_path + "-wal",
                remote_db_file_path + "-shm")
        if pkg:
            uid = self._app_uid(pkg)
            if uid:
                b.shell("chown", f"{uid}:{uid}", remote_db_file_path)
                data_dir = "/".join(seg[:5]) if seg[2] == "user" else "/".join(seg[:4])
                b.shell("restorecon", "-R", data_dir)
            b.shell("am", "force-stop", pkg)

    def _app_uid(self, pkg: str) -> str | None:
        out = self._bridge.shell("dumpsys", "package", pkg)
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("userId="):
                return line.split("=", 1)[1].split()[0]
        return None

    def refresh_env(self):
        self.ensure_service()

    def close(self):
        pass


class AxdroidWorldEnv(interface.AsyncEnv):
    """AsyncEnv over the axdroid controller."""

    def __init__(self, controller: AxdroidController):
        self._controller = controller
        self._interaction_cache = ""

    @property
    def interaction_cache(self) -> str:
        return self._interaction_cache

    @interaction_cache.setter
    def interaction_cache(self, value: str) -> None:
        self._interaction_cache = value

    # -- AsyncEnv API ----------------------------------------------------

    @property
    def controller(self) -> AxdroidController:
        return self._controller

    def reset(self, go_home: bool = False) -> interface.State:
        self.interaction_cache = ""
        if go_home:
            try:
                self._controller._bridge.shell("input", "keyevent", "3")
            except Exception:
                pass
        return self.get_state()

    def get_state(self, wait_to_stabilize: bool = False) -> interface.State:
        els = self._controller.get_ui_elements()
        if wait_to_stabilize:
            for _ in range(2):
                time.sleep(0.4)
                els = self._controller.get_ui_elements()
        pixels = self._pixels()
        forest = self._controller.forest_from_snapshot()
        return interface.State(pixels=pixels, forest=forest, ui_elements=els,
                               auxiliaries={})

    def _pixels(self):
        import numpy as np
        from PIL import Image
        png = self._controller._bridge.screenshot_png()
        img = Image.open(io.BytesIO(png)).convert("RGB")
        return np.asarray(img)

    def execute_action(self, action: json_action.JSONAction) -> None:
        """JSONAction → axdroid semantic ops (used by scripted helpers; the
        benchmark agent talks to axdroid over MCP directly)."""
        s = self._controller.session
        t = action.action_type
        if t == json_action.ANSWER:
            self.interaction_cache = action.text
            return
        if t == json_action.STATUS:
            return
        if t == json_action.CLICK:
            if action.index is not None:
                el = self._controller._last_els[action.index]
                ref = (el.metadata or {}).get("ref") if el.metadata else None
                if ref:
                    s.tap(ref, confirm=True)
                    return
                cx, cy = el.bbox_pixels.center
                s.tap({"x": int(cx), "y": int(cy)}, confirm=True)
                return
            s.tap({"x": int(action.x), "y": int(action.y)}, confirm=True)
        elif t == json_action.INPUT_TEXT:
            s.type(action.text)
        elif t == json_action.KEYBOARD_ENTER:
            self._controller._bridge.shell("input", "keyevent", "66")
        elif t == json_action.NAVIGATE_BACK:
            s.button("back")
        elif t == json_action.NAVIGATE_HOME:
            s.button("home")
        elif t == json_action.OPEN_APP:
            s.launch(action.app_name)
        elif t == json_action.SCROLL:
            s.scroll(action.direction)
        elif t == json_action.SWIPE:
            raise NotImplementedError("swipe by coordinates not exposed here")
        elif t == json_action.WAIT:
            time.sleep(3)

    def display_message(self, message: str, header: str = "") -> None:
        pass

    def ask_question(self, question: str, timeout_seconds: float = -1.0):
        return None

    @property
    def foreground_activity_name(self) -> str:
        activity = adb_utils.get_current_activity(self.controller)[0]
        return activity or ""

    @property
    def device_screen_size(self) -> tuple[int, int]:
        return self._controller.device_screen_size

    @property
    def logical_screen_size(self) -> tuple[int, int]:
        return self._controller.logical_screen_size

    def close(self) -> None:
        pass

    @property
    def orientation(self) -> int:
        return adb_utils.get_orientation(self.controller)

    @property
    def physical_frame_boundary(self) -> tuple[int, int, int, int]:
        return adb_utils.get_physical_frame_boundary(self.controller)

    def hide_automation_ui(self) -> None:
        adb_utils.issue_generic_request(
            "shell settings put system pointer_location 0", self.controller)


def build_env(adb_path: str, serial: str, port: int = 8360) -> AxdroidWorldEnv:
    """LocalAdbEnv + axdroid Session + controller, wired together."""
    bridge = LocalAdbEnv(adb_path, serial)
    adb = __import__("axdroid.client", fromlist=["Adb"]).Adb(adb_path, serial)
    adb.forward(port, device_port=8360)   # app always listens on device 8360
    phone = Phone(f"http://127.0.0.1:{port}", adb=adb, forward=False)
    session = Session(phone)
    controller = AxdroidController(bridge, session)
    return AxdroidWorldEnv(controller)
