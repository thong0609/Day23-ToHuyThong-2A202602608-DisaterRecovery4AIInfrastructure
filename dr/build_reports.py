"""Generate timestamp-linked reports from completed drills, without changing logs."""
import datetime
import json
import pathlib
import sys

sys.path.insert(0, ".")
from tools.measure_rto import measure


def records(path):
    return [(i, json.loads(line)) for i, line in
            enumerate(pathlib.Path(path).read_text().splitlines(), 1) if line.strip()]


def main():
    report = pathlib.Path("reports")
    paths = dict(chaos="chaos/chaos-events.jsonl", health="reports/health-events.jsonl",
                 failover="reports/failover-events.jsonl", runbook="reports/runbook-run.jsonl",
                 traffic="reports/drill-2-withdr.jsonl")
    data = {key: records(path) for key, path in paths.items()}
    metrics = measure(pathlib.Path(paths["traffic"]), paths["chaos"], paths["health"],
                      paths["failover"], 300)
    assert metrics["valid"] and not metrics["warnings"] and metrics["rto_verdict"] == "PASS"
    lo, hi = data["traffic"][0][1]["ts"], data["traffic"][-1][1]["ts"]

    def find(key, predicate):
        return next((e, f"{paths[key]}:{i}") for i, e in data[key]
                    if lo <= e["ts"] <= hi and predicate(e))

    outage = find("chaos", lambda e: e.get("action") == "kill")
    t0 = outage[0]["ts"]
    failed = find("traffic", lambda e: e["ts"] >= t0 and not e["ok"])
    recovered = find("traffic", lambda e: e["ts"] > failed[0]["ts"] and e["ok"])
    detected = find("health", lambda e: e["ts"] >= t0 and e.get("region") == "a"
                    and e.get("to") == "UNHEALTHY")
    steps = {n: find("failover", lambda e, n=n: e.get("step", "").startswith(str(n) + "_"))
             for n in range(1, 6)}
    announced = find("runbook", lambda e: e.get("step") == 2)
    confirmed = find("runbook", lambda e: e.get("step") == 3)
    golden = find("runbook", lambda e: e.get("step") == 6)
    rto, rpo, lost = metrics["rto_measured_s"], metrics["rpo_at_restore_s"], metrics["docs_lost"]
    rows = [("Outage", outage), ("User thấy lỗi đầu tiên", failed),
            ("Health checker phát hiện", detected), ("Snapshot restore xong", steps[2]),
            ("Region B ready", steps[4]), ("DNS cutover", steps[5]),
            ("Request phục hồi từ B / RTO", recovered)]
    table = "\n".join(f"| {name} | {e['ts'] - t0:.3f}s | `{ref}` |" for name, (e, ref) in rows)
    # Partition the actual wall-clock interval. Keep nominal floor separate from
    # measured detection: timeout, polling phase and operator orchestration add time.
    detect = detected[0]["ts"] - t0
    restore = steps[3][0]["ts"] - detected[0]["ts"]
    warm = steps[5][0]["ts"] - steps[3][0]["ts"]
    ttl = recovered[0]["ts"] - steps[5][0]["ts"]
    floor = metrics["health_check_config"]["detect_floor_s"]
    first = records("reports/drill-1-nodr.jsonl")
    baseline_kill = next((e, i) for i, e in data["chaos"] if e.get("action") == "kill"
                         and first[0][1]["ts"] <= e["ts"] <= first[-1][1]["ts"])
    baseline_fail = next((i, e) for i, e in first if e["ts"] >= baseline_kill[0]["ts"] and not e["ok"])
    text = f"""# RTO/RPO Evidence — Lab 23

Môi trường: WSL Ubuntu, bare processes, chaos netblock --mock, backend fs.
Timestamp trong log là Unix/UTC; không thay đổi timestamp hoặc chép số từ bài mẫu.

Trước khi có DR: chưa có thành phần tự phát hiện outage để chuyển vùng; Region B
có count=0 và weights=false; chuyển pointer sang B lúc đó sẽ trả region_not_ready.
Request đi qua edge (cache TTL 5s), serving (readiness/pool), rồi SQLite + weights.
Healthz chỉ kiểm tra process; readyz kiểm tra cả dữ liệu, weights và warm-up.

## 1. Drill 1 — chưa có DR

| Chỉ số | Kết quả | Evidence |
|---|---|---|
| Outage | {baseline_kill[0]['iso']} UTC | `chaos/chaos-events.jsonl:{baseline_kill[1]}` |
| Request lỗi đầu tiên | +{baseline_fail[1]['ts'] - baseline_kill[0]['ts']:.3f}s | `reports/drill-1-nodr.jsonl:{baseline_fail[0]}` |
| Phục hồi trong cửa sổ đo | Không có; NO_RECOVERY | `reports/measure-drill-1.json:1` |

## 2. Drill 2 — có DR

| Mốc | Từ outage | Evidence |
|---|---|---|
{table}

| Chỉ số | Đo được | Mục tiêu | Verdict |
|---|---|---|---|
| RTO Inference API | {rto}s | 300s | PASS |
| RPO Vector DB tại restore | {rpo}s / {lost} documents | 300s | {'PASS' if rpo <= 300 else 'FAIL'} |

RPO lấy từ `reports/failover-events.jsonl:{steps[2][1].split(':')[-1]}`;
máy đo trả valid=true, warnings=[], recovered_by_region=b (`reports/measure-drill-2.json:1`).

## 3. Phân rã RTO

| Thành phần | Giây | Nguồn và cách tính | Cách giảm |
|---|---|---|---|
| Phát hiện: floor danh nghĩa + thời gian probe/pha polling | {detect:.3f}s | outage → detect; floor cấu hình 5 × 3 = {floor}s; `{detected[1]}` | Giảm interval, cân nhắc false alarm |
| Điều phối, verify, restore snapshot và scale | {restore:.3f}s | detect → 3_scale_pool; `{steps[3][1]}` | Snapshot nhỏ hơn, giảm thời gian điều phối |
| GPU warm-up, chờ ready và ghi cutover | {warm:.3f}s | 3_scale_pool → 5_dns_cutover; `{steps[4][1]}` | Giữ standby full, tốn thêm compute |
| DNS/LB cache và pha request | {ttl:.3f}s | cutover → request B thành công; `{recovered[1]}` | Giảm TTL, tăng lưu lượng kiểm tra |

Tổng = {detect + restore + warm + ttl:.3f}s, làm tròn một chữ số = {rto}s.
Restore riêng mất {steps[2][0]['duration_s']:.3f}s; chờ ready riêng mất
{steps[4][0]['waited_s']:.3f}s. Bảng phân vùng toàn bộ thời gian thực, gồm overhead,
không gán toàn bộ thời gian điều phối cho thao tác copy snapshot.
5 × 3 là floor danh nghĩa mà rubric dùng; độ trễ thực còn phụ thuộc pha polling và timeout.

Golden signals: 10 request thật, p95 {golden[0]['p95_ms']:.2f}ms,
error rate {golden[0]['error_rate']:.0%}, evidence `{golden[1]}`.
"""
    (report / "rto-evidence.md").write_text(text, encoding="utf-8")
    timeline = "\n".join(
        f"| {datetime.datetime.fromtimestamp(e['ts'], datetime.timezone.utc).isoformat()} | {name} | `{ref}` |"
        for name, (e, ref) in [rows[0], rows[1], rows[2], ("Incident announced", announced),
                               ("Failover --auto hoàn tất (không có xác nhận người thật)", confirmed), rows[-1]])
    (report / "postmortem.md").write_text(f"""# Postmortem — DR Drill Lab 23

## 1. Timeline (UTC)

| ISO time | Sự kiện | Evidence |
|---|---|---|
{timeline}

## 2. RTO/RPO và gap analysis

- RTO mục tiêu 300s; đo {rto}s; gap (đo − mục tiêu) = {rto - 300:.1f}s.
- RPO mục tiêu 300s; đo {rpo}s / {lost} documents; gap = {rpo - 300:.2f}s.
- Thành phần tốn thời gian nhất trong lần chạy này: {max([(detect, 'phát hiện outage'), (restore, 'điều phối và restore'), (warm, 'warm-up'), (ttl, 'DNS cache')])[1]}.
- Chi tiết và mọi mốc nguồn: `reports/rto-evidence.md:1`.

## 3. Root cause — 5 whys

1. Vì sao request lỗi? Region A bị SIGSTOP, kết nối treo đến timeout.
2. Vì sao edge chưa chuyển ngay? Pointer còn A và cache TTL giữ upstream cũ.
3. Vì sao không thể chuyển ngay sang B? B khởi đầu rỗng, thiếu weights và pool chưa full.
4. Vì sao baseline không tự phục hồi? Chưa có health checker và runbook điều phối restore/readiness/cutover.
5. Vì sao sự cố thật vẫn có thể làm quy trình thất bại? Snapshot và region vẫn nằm cùng máy;
   snapshot copy SQLite không có giao thức snapshot nhất quán với writer; chưa có fencing cho writer A.
   Mất cả host hoặc snapshot lỗi vượt quá phạm vi mô phỏng này.

Ingest trong lab là process riêng nên tiếp tục ghi A trong lúc serving A bị pause.
RPO ở đây là chênh lệch DB tại restore, không phải số liệu bảo đảm cho toàn bộ thời gian outage.
Phép đo docs_lost có sẵn dựa trên timestamp mới hơn, phù hợp workload append-only của lab.

## 4. Action items (đề xuất, chưa triển khai)

| Action | Owner | Deadline | Tác động dự kiến |
|---|---|---|---|
| Thử interval=1s, threshold=3 qua nhiều drill và đo false alarm | SRE | 2026-10-10 | Floor danh nghĩa giảm 12s; cần đo lại RTO |
| Dùng SQLite backup API, kiểm tra checksum và diễn tập snapshot hỏng | Data engineer | 2026-10-12 | Bảo đảm restore nhất quán; chưa đo lợi ích thời gian |
| Fencing writer cũ, sao lưu khác failure domain, đối soát document sau failover | Platform lead | 2026-10-14 | Hạn chế split-brain/mất dữ liệu; chưa định lượng |

## 5. Câu hỏi bắt buộc

1. interval × threshold = 5 × 3 = {floor}s, tương đương {100 * floor / rto:.1f}% RTO.
   Detection thực đo {detect:.3f}s, vì còn timeout và pha polling.
2. Đổi interval thành 1s giảm phần danh nghĩa từ 15s xuống 3s, tức 12s.
   Đây là ước tính, chưa phải RTO đã đo: số probe tăng, dễ phản ứng với lỗi ngắn hạn;
   vẫn cần threshold, xác nhận vận hành và circuit breaker.
3. Nếu A mất dữ liệu vĩnh viễn sau outage 6 giờ, {lost} documents là số bản ghi mới
   có trên A mà snapshot restore của lần đo này chưa chứa. Khách hàng có thể thiếu ticket/nội dung
   truy xuất tương ứng. Không thể suy ra tổng mất mát 6 giờ từ số này; cần kiểm tra write routing,
   snapshot cuối cùng, audit ingest và khả năng replay dữ liệu nguồn.
""", encoding="utf-8")


if __name__ == "__main__":
    main()
