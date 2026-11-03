"""Render gate reports into the public compatibility list (UTF-8 safe)."""
import json
import sys

sys.path.insert(0, ".")
from axdroid.gates import publish_gates

reports = [json.load(open(p, encoding="utf-8")) for p in sys.argv[1:-1]]
with open(sys.argv[-1], "w", encoding="utf-8") as f:
    f.write(publish_gates(reports))
print("wrote", sys.argv[-1])
