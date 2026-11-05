"""Supervise the UI and the single API/scheduler owner in one container."""
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

processes: list[subprocess.Popen] = []
stopping = False


def stop(signum=None, frame=None):
    global stopping
    if stopping:
        return
    stopping = True
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def main():
    data = Path(os.environ.get("DATA_DIR", "/data"))
    (data / "home").mkdir(parents=True, exist_ok=True)
    processes.append(subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "openbricks.main:app",
         "--host", "127.0.0.1", "--port", "8000", "--workers", "1"],
        cwd="/app/backend",
    ))
    # Fail immediately on invalid bootstrap settings, rather than serving a broken UI.
    ready = False
    for _ in range(120):
        if stopping or processes[0].poll() is not None:
            break
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=1) as response:
                ready = response.status == 200
            if ready:
                break
        except (OSError, urllib.error.URLError):
            time.sleep(0.5)
    if not ready:
        print("OpenBricks API did not become ready. See the startup error above.", flush=True)
        stop()
        return 1
    processes.append(subprocess.Popen(["node", "/app/web/server.js"], cwd="/app/web"))
    print("OpenBricks is listening on port " + os.environ.get("PORT", "3000"), flush=True)
    while not stopping and all(p.poll() is None for p in processes):
        time.sleep(0.5)
    failed = not stopping
    stop()
    deadline = time.monotonic() + 20
    for process in reversed(processes):
        try:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
