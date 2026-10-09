"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr.health_checker import probe

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """TODO: ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    rec = dict(ts=time.time(), iso=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               step=n, name=name, **kw)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(rec) + "\n")
    print(json.dumps(rec), flush=True)


def confirm(auto: bool, msg: str) -> bool:
    """TODO: auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() == "y"
    except EOFError:
        return False


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """TODO: 7 bước ở trên."""
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("Primary and target must be distinct regions")
    started = time.monotonic()
    # Independent confirmation: a warm standby may be alive without being ready.
    for attempt in range(3):
        time.sleep(5)
        ready, reason = probe(primary, 2)
        target_ready, target_reason = probe(target, 2)
        try:
            alive = httpx.get(f"{URL[target]}/healthz", timeout=2).status_code == 200
        except httpx.HTTPError:
            alive = False
        if ready or not alive:
            step(1, "xac_nhan_outage", ok=False, reason=reason, target_alive=alive)
            return dict(ok=False, reason="Primary ready or standby unavailable")
    step(1, "xac_nhan_outage", ok=True, primary=primary,
         target_ready=target_ready, target_reason=target_reason, consecutive_fails=3)
    chaos = pathlib.Path("chaos/chaos-events.jsonl")
    events = [json.loads(line) for line in chaos.read_text().splitlines()] if chaos.exists() else []
    outage = next((e["ts"] for e in reversed(events)
                   if e.get("action") == "kill" and e.get("region") == primary), None)
    step(2, "thong_bao_incident", t_outage=outage, primary=primary, target=target)
    if not confirm(auto, f"Fail over {primary} to {target}?"):
        return dict(ok=False, reason="Operator declined cutover")
    result = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", result=result, auto=auto)
    if not result.get("ok"):
        return result
    step(4, "verify_state_replica", state=result["state"])
    step(5, "dns_cutover", ok=result["ok"], target=target)
    samples, errors = [], 0
    for _ in range(10):
        tick = time.monotonic()
        try:
            response = httpx.get(f"{URL[target]}/v1/infer", timeout=2)
            success = response.status_code == 200 and response.json().get("region") == target
        except (httpx.HTTPError, ValueError):
            success = False
        errors += not success
        samples.append((time.monotonic() - tick) * 1000)
    p95 = sorted(samples)[9]  # nearest-rank p95 of ten observations
    healthy = errors == 0 and p95 < 1000
    step(6, "verify_golden_signals", requests=10, p95_ms=p95,
         error_rate=errors / 10, latency_ms=samples, ok=healthy)
    step(7, "post_incident", elapsed_s=time.monotonic() - started, ok=healthy,
         measure_command="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300")
    return dict(**{**result, "ok": healthy}, p95_ms=p95, error_rate=errors / 10)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    result = run(a.primary, a.target, a.backend, a.auto)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result.get("ok") else 1)
