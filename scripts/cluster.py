"""Local process supervisor for the three backend nodes (the non-Docker equivalent of
`docker compose up node-a node-b node-c` with `restart: on-failure`).

    python scripts/cluster.py                 # start A, B, C on ports 8001-8003 (foreground)
    python scripts/cluster.py --nodes A,B     # subset
    python scripts/node_ctl.py stop A         # operator stop: keep node A down
    python scripts/node_ctl.py start A        # bring it back

Behaviour:
  * A node that exits with a non-zero code (e.g. injected crash, exit 75) is restarted after
    --restart-delay seconds -- long enough to observe failover on the dashboard.
  * A node with a control file runtime/control/<NODE>.down is kept stopped.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "runtime" / "control"
LOGS = ROOT / "runtime" / "logs"
PORTS = {"A": 8001, "B": 8002, "C": 8003}


def spawn(node: str) -> subprocess.Popen:
    env = os.environ.copy()
    env["NODE_ID"] = node
    log = open(LOGS / f"node-{node}.log", "a", buffering=1)
    log.write(f"\n===== starting node {node} at {time.strftime('%H:%M:%S')} =====\n")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORTS[node]),
         "--log-level", "warning", "--timeout-graceful-shutdown", "5"],
        cwd=ROOT / "backend", env=env, stdout=log, stderr=subprocess.STDOUT,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", default="A,B,C")
    ap.add_argument("--restart-delay", type=float, default=float(os.environ.get("RESTART_DELAY_S", 8)))
    args = ap.parse_args()
    nodes = [n.strip().upper() for n in args.nodes.split(",")]
    CONTROL.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    (ROOT / "runtime" / "supervisor.pid").write_text(str(os.getpid()))
    procs: dict[str, subprocess.Popen | None] = {n: None for n in nodes}
    down_since: dict[str, float] = {}
    stopping = False

    def shutdown(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    print(f"supervisor: managing nodes {nodes} (restart delay {args.restart_delay}s)", flush=True)
    while not stopping:
        for n in nodes:
            p = procs[n]
            forced_down = (CONTROL / f"{n}.down").exists()
            if p is not None and p.poll() is None:
                if forced_down:
                    print(f"supervisor: operator stop -> terminating node {n}", flush=True)
                    p.terminate()
                    p.wait(10)
                    procs[n] = None
                continue
            if p is not None:  # process exited on its own
                print(f"supervisor: node {n} exited with code {p.returncode}", flush=True)
                procs[n] = None
                down_since[n] = time.time()
            if forced_down:
                continue
            if n not in down_since or time.time() - down_since[n] >= args.restart_delay:
                print(f"supervisor: starting node {n} on :{PORTS[n]}", flush=True)
                procs[n] = spawn(n)
                down_since.pop(n, None)
        time.sleep(0.5)
    for p in procs.values():
        if p and p.poll() is None:
            p.terminate()
    for p in procs.values():
        if p:
            p.wait(10)
    print("supervisor: all nodes stopped", flush=True)


if __name__ == "__main__":
    main()
