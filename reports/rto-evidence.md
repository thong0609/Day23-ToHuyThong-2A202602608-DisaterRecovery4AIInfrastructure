# RTO/RPO Evidence — Lab 23

Môi trường: WSL Ubuntu, bare processes, chaos netblock --mock, backend fs.
Timestamp trong log là Unix/UTC; không thay đổi timestamp hoặc chép số từ bài mẫu.

Trước khi có DR: chưa có thành phần tự phát hiện outage để chuyển vùng; Region B
có count=0 và weights=false; chuyển pointer sang B lúc đó sẽ trả region_not_ready.
Request đi qua edge (cache TTL 5s), serving (readiness/pool), rồi SQLite + weights.
Healthz chỉ kiểm tra process; readyz kiểm tra cả dữ liệu, weights và warm-up.

## 1. Drill 1 — chưa có DR

| Chỉ số | Kết quả | Evidence |
|---|---|---|
| Outage | 2026-10-09T04:15:03 UTC | `chaos/chaos-events.jsonl:1` |
| Request lỗi đầu tiên | +0.099s | `reports/drill-1-nodr.jsonl:17` |
| Phục hồi trong cửa sổ đo | Không có; NO_RECOVERY | `reports/measure-drill-1.json:1` |

## 2. Drill 2 — có DR

| Mốc | Từ outage | Evidence |
|---|---|---|
| Outage | 0.000s | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | 1.630s | `reports/drill-2-withdr.jsonl:26` |
| Health checker phát hiện | 20.475s | `reports/health-events.jsonl:2` |
| Snapshot restore xong | 23.082s | `reports/failover-events.jsonl:2` |
| Region B ready | 29.363s | `reports/failover-events.jsonl:4` |
| DNS cutover | 29.404s | `reports/failover-events.jsonl:5` |
| Request phục hồi từ B / RTO | 33.922s | `reports/drill-2-withdr.jsonl:42` |

| Chỉ số | Đo được | Mục tiêu | Verdict |
|---|---|---|---|
| RTO Inference API | 33.9s | 300s | PASS |
| RPO Vector DB tại restore | 8.0s / 4 documents | 300s | PASS |

RPO lấy từ `reports/failover-events.jsonl:2`;
máy đo trả valid=true, warnings=[], recovered_by_region=b (`reports/measure-drill-2.json:1`).

## 3. Phân rã RTO

| Thành phần | Giây | Nguồn và cách tính | Cách giảm |
|---|---|---|---|
| Phát hiện: floor danh nghĩa + thời gian probe/pha polling | 20.475s | outage → detect; floor cấu hình 5 × 3 = 15.0s; `reports/health-events.jsonl:2` | Giảm interval, cân nhắc false alarm |
| Điều phối, verify, restore snapshot và scale | 2.618s | detect → 3_scale_pool; `reports/failover-events.jsonl:3` | Snapshot nhỏ hơn, giảm thời gian điều phối |
| GPU warm-up, chờ ready và ghi cutover | 6.312s | 3_scale_pool → 5_dns_cutover; `reports/failover-events.jsonl:4` | Giữ standby full, tốn thêm compute |
| DNS/LB cache và pha request | 4.518s | cutover → request B thành công; `reports/drill-2-withdr.jsonl:42` | Giảm TTL, tăng lưu lượng kiểm tra |

Tổng = 33.922s, làm tròn một chữ số = 33.9s.
Restore riêng mất 0.283s; chờ ready riêng mất
6.249s. Bảng phân vùng toàn bộ thời gian thực, gồm overhead,
không gán toàn bộ thời gian điều phối cho thao tác copy snapshot.
5 × 3 là floor danh nghĩa mà rubric dùng; độ trễ thực còn phụ thuộc pha polling và timeout.

Golden signals: 10 request thật, p95 63.81ms,
error rate 0%, evidence `reports/runbook-run.jsonl:6`.
