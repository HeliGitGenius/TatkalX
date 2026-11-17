"""Operator stop/start of a node managed by scripts/cluster.py.

    python scripts/node_ctl.py stop A     # graceful stop; stays down
    python scripts/node_ctl.py start A    # supervisor starts it again
"""
import sys
from pathlib import Path

CONTROL = Path(__file__).resolve().parents[1] / "runtime" / "control"

if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("stop", "start") or sys.argv[2].upper() not in ("A", "B", "C"):
        sys.exit("usage: node_ctl.py stop|start A|B|C")
    CONTROL.mkdir(parents=True, exist_ok=True)
    flag = CONTROL / f"{sys.argv[2].upper()}.down"
    if sys.argv[1] == "stop":
        flag.touch()
    else:
        flag.unlink(missing_ok=True)
    print(f"node {sys.argv[2].upper()}: {sys.argv[1]} requested")
