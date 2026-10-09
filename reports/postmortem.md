# Postmortem — DR Drill Lab 23

## 1. Timeline (UTC)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T04:15:54.561020+00:00 | Outage | `chaos/chaos-events.jsonl:3` |
| 2026-10-09T04:15:56.190659+00:00 | User thấy lỗi đầu tiên | `reports/drill-2-withdr.jsonl:26` |
| 2026-10-09T04:16:15.035555+00:00 | Health checker phát hiện | `reports/health-events.jsonl:2` |
| 2026-10-09T04:16:17.313390+00:00 | Incident announced | `reports/runbook-run.jsonl:2` |
| 2026-10-09T04:16:23.966601+00:00 | Failover --auto hoàn tất (không có xác nhận người thật) | `reports/runbook-run.jsonl:3` |
| 2026-10-09T04:16:28.483081+00:00 | Request phục hồi từ B / RTO | `reports/drill-2-withdr.jsonl:42` |

## 2. RTO/RPO và gap analysis

- RTO mục tiêu 300s; đo 33.9s; gap (đo − mục tiêu) = -266.1s.
- RPO mục tiêu 300s; đo 8.0s / 4 documents; gap = -292.00s.
- Thành phần tốn thời gian nhất trong lần chạy này: phát hiện outage.
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

1. interval × threshold = 5 × 3 = 15.0s, tương đương 44.2% RTO.
   Detection thực đo 20.475s, vì còn timeout và pha polling.
2. Đổi interval thành 1s giảm phần danh nghĩa từ 15s xuống 3s, tức 12s.
   Đây là ước tính, chưa phải RTO đã đo: số probe tăng, dễ phản ứng với lỗi ngắn hạn;
   vẫn cần threshold, xác nhận vận hành và circuit breaker.
3. Nếu A mất dữ liệu vĩnh viễn sau outage 6 giờ, 4 documents là số bản ghi mới
   có trên A mà snapshot restore của lần đo này chưa chứa. Khách hàng có thể thiếu ticket/nội dung
   truy xuất tương ứng. Không thể suy ra tổng mất mát 6 giờ từ số này; cần kiểm tra write routing,
   snapshot cuối cùng, audit ingest và khả năng replay dữ liệu nguồn.
