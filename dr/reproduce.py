"""Run both graded drills on Linux/WSL: python3 dr/reproduce.py.

Requires seeded state and running bare services. Refuses to overwrite evidence.
"""
import json
import pathlib
import subprocess
import sys
import time

import httpx

sys.path.insert(0, ".")
from tools.measure_rto import measure


def command(*args):
    subprocess.run([sys.executable, *args], check=True)


def main():
    standby = httpx.get("http://127.0.0.1:8002/v1/state", timeout=2).json()
    if standby["count"] != 0 or standby["weights"] or standby["pool_state"] != "warm":
        raise SystemExit("Standby must start empty, without weights, with pool=warm; archive old state before seeding")
    for name in ("drill-1-nodr.jsonl", "drill-2-withdr.jsonl"):
        if pathlib.Path("reports", name).exists():
            raise SystemExit("Archive existing drill evidence before rerunning")
    jobs = []

    def launch(*args):
        job = subprocess.Popen([sys.executable, *args])
        jobs.append(job)
        return job

    def save_measure(n, filename):
        result = measure(pathlib.Path("reports", filename), "chaos/chaos-events.jsonl",
                         "reports/health-events.jsonl", "reports/failover-events.jsonl", 300)
        pathlib.Path(f"reports/measure-drill-{n}.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2), flush=True)
        return result

    try:
        baseline = launch("loadgen/traffic.py", "--duration", "40", "--rps", "2",
                          "--out", "reports/drill-1-nodr.jsonl")
        time.sleep(8)
        command("chaos/kill_region.py", "--region", "a", "--mode", "netblock", "--mock")
        if baseline.wait() != 0:
            raise RuntimeError("Baseline traffic failed")
        assert save_measure(1, "drill-1-nodr.jsonl")["rto_verdict"] == "NO_RECOVERY"
        command("chaos/kill_region.py", "restore", "--region", "a", "--backend", "bare")
        launch("state/ingest.py", "--region", "a", "--rate", "0.5", "--duration", "150")
        launch("state/replicate.py", "--every", "30", "--duration", "150", "--backend", "fs")
        time.sleep(5)
        if not pathlib.Path("state/_replica/dr-artifacts/MANIFEST.json").exists():
            raise RuntimeError("First snapshot missing")
        traffic = launch("loadgen/traffic.py", "--duration", "100", "--rps", "2",
                         "--out", "reports/drill-2-withdr.jsonl")
        launch("dr/health_checker.py", "--interval", "5", "--threshold", "3",
               "--duration", "100", "--out", "reports/health-events.jsonl")
        time.sleep(12)
        command("chaos/kill_region.py", "--region", "a", "--mode", "netblock", "--mock")
        command("dr/runbook.py", "--primary", "a", "--target", "b", "--backend", "fs", "--auto")
        if traffic.wait() != 0:
            raise RuntimeError("Recovery traffic failed")
        result = save_measure(2, "drill-2-withdr.jsonl")
        assert result["valid"] and not result["warnings"] and result["rto_verdict"] == "PASS"
        for job in jobs:
            if job.wait() != 0:
                raise RuntimeError("Background task failed")
    finally:
        command("chaos/kill_region.py", "restore", "--region", "a", "--backend", "bare")
        for job in jobs:
            if job.poll() is None:
                job.terminate()
                job.wait()


if __name__ == "__main__":
    main()
