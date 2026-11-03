"""axdroid journal — the persistent activity log (ARCHITECTURE §4.5, App Review).

Every actuation, refusal and owner action is appended to
<AXDROID_HOME>/journal.jsonl: one JSON object per line, newest last. This is
what the companion app's activity screen reads, what the owner can audit, and
what App Review means by "indication when recording user activity" — it is
disclosed, on-device-hub, and contains no screen content, only element names
the agent acted on.

Rotates to journal.jsonl.1 at ~2 MiB so an unattended hub can't fill its disk.
"""

from __future__ import annotations

import json
import os
import time


class Journal:
    def __init__(self, path: str | None, *, max_bytes: int = 2_000_000):
        self.path = path
        self.max_bytes = max_bytes

    def append(self, event: str, **facts) -> None:
        if not self.path:
            return
        rec = {"t": round(time.time(), 3), **facts, "kind": event}
        try:
            self._rotate_if_needed()
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError:  # the log must never break the phone path
            pass

    def _rotate_if_needed(self) -> None:
        try:
            if self.path and os.path.exists(self.path) and os.path.getsize(self.path) > self.max_bytes:
                os.replace(self.path, self.path + ".1")
        except OSError:
            pass

    def tail(self, n: int = 50) -> list[dict]:
        """Newest-last slice of the journal. The file is bounded by rotation
        (~2 MiB), so a forward read is simpler and exact."""
        if not self.path or not os.path.exists(self.path):
            return []
        out: list[dict] = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        out.append(json.loads(ln))
                    except ValueError:
                        pass
        except OSError:
            return []
        return out[-n:]
