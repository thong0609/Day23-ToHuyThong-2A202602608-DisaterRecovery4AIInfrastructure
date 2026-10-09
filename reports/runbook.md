# Runbook — Region A down

Phạm vi: lab local WSL/Linux, A=:8001, B=:8002, edge=:8080; backend fs.
Chạy từ thư mục gốc repo, trong virtualenv đã cài requirements.txt.
On-call thực hiện; incident commander (IC) quyết định chuyển vùng và rollback;
data engineer xác nhận snapshot/model version và đối soát dữ liệu.

## Chuẩn bị và chạy lại từ đầu

Lưu trữ evidence cũ trước khi chạy lại. Không ghi đè log của bài đã nộp.
Môi trường Windows checkout có CRLF: Python đọc và chuẩn hóa xuống dòng khi chạy
shell như dưới đây, không phải sửa file scripts/.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
make seed
python3 -c 'import pathlib,subprocess; subprocess.run(["bash","-c",pathlib.Path("scripts/up_bare.sh").read_text(),"scripts/up_bare.sh"],check=True)'
curl -fsS localhost:8080/v1/infer
python3 dr/reproduce.py
python3 dr/build_reports.py
python3 -m pytest tests/ -v
```

`dr/reproduce.py` chạy cả baseline và drill có DR theo thời lượng GUIDE, chờ mọi
process con, lưu kết quả máy đo và khôi phục process A khi kết thúc. Từ chối chạy
nếu hai file traffic đã tồn tại. Chỉ chạy trong workspace diễn tập riêng/đã lưu trữ
lần trước; cần bảo đảm B chưa chứa weights và pool=warm để đo đủ warm-up.

## Xử lý incident (7 bước)

Lệnh điều phối duy nhất sau khi xác nhận outage:
`python3 dr/runbook.py --primary a --target b --backend fs`.
Mặc định hỏi y/N trước khi failover. Chỉ dùng `--auto` trong drill/CI.
Các lệnh kiểm tra dưới đây không gọi lại failover, không tự sửa pointer DNS.

| # | Bước | Lệnh / hành động | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status`; kiểm tra `reports/health-events.jsonl` | A readiness thất bại 3 lần liên tiếp; B `/healthz` còn 200. B chưa ready là bình thường trước restore | On-call |
| 2 | Mở incident, ghi thời gian | `python3 dr/runbook.py --primary a --target b --backend fs`; đọc prompt rồi nhập `y` khi IC đồng ý | Log step 2 có ts thông báo và t_outage; IC đã xác nhận. Lệnh tiếp tục làm bước 3–7 | On-call + IC |
| 3 | Restore snapshot | `cat reports/failover-events.jsonl` | `2_restore_snapshot` có rpo_seconds, docs_lost và embed_model_version; không có abort | Data engineer |
| 4 | Scale pool và chờ ready | `curl -fsS localhost:8002/readyz` | 200, pool full, có vectors và weights; log `4_wait_ready` | Platform on-call |
| 5 | DNS/LB cutover | `curl -fsS localhost:8080/edge/state`; `curl -fsS localhost:8080/v1/infer` | Sau TTL, active_region=b và response được B phục vụ; log `5_dns_cutover` | On-call |
| 6 | Verify golden signals | `cat reports/runbook-run.jsonl` | step 6: 10 request thật vào B, p95 < 1000ms và error rate=0; đồng thời traffic qua edge phục hồi | On-call |
| 7 | Đo và postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300`; `python3 dr/build_reports.py` | valid=true, warnings=[], PASS, RTO ≤300s; evidence và postmortem khớp log | On-call + IC |

Nếu restore lỗi hoặc ready timeout: dừng, giữ pointer hiện tại, đọc abort trong
failover-events; sửa snapshot/standby trước khi thử lại. Không chuyển sang B chưa ready.
Nếu golden signals không đạt: giữ incident mở, IC đánh giá rollback; không tự động
chuyển đi chuyển lại. Runbook trả ok=false, không xem đó là đã giải quyết incident.

## Rollback có kiểm soát

Chỉ IC có quyền phê duyệt. Điều kiện xem xét: sau cutover B có error rate >0 hoặc
p95 ≥1000ms trong kiểm tra 10 request; A đã phục hồi và readiness ổn định. On-call
không đưa traffic về A chỉ vì process sống lại. Data engineer phải xác nhận dữ liệu
và model version phù hợp, khóa writer cũ và đối soát ghi phát sinh trên B trước.

1. Khôi phục A trong mô phỏng: `python3 chaos/kill_region.py restore --region a --backend bare`.
2. Kiểm tra `curl -fsS localhost:8001/readyz` ba lần cách nhau 5 giây.
3. Nếu B là nguồn dữ liệu đã được xác nhận đầy đủ, dừng ingest của lab và tạo snapshot:
   `python3 state/snapshot.py put --region b --backend fs`.
4. Sau phê duyệt IC: `python3 dr/failover.py --target a --backend fs`.
   Hàm vẫn restore → scale → wait_ready → cutover; lỗi trước cutover thì dừng.
5. Kiểm tra `curl -fsS localhost:8080/v1/infer` sau TTL, phải phục vụ bởi A;
   đo lại golden signals và lưu log rollback riêng.

Không chạy rollback khi B thiếu dữ liệu mà A đang có: dừng để data engineer hợp nhất
và xác nhận nguồn snapshot. Snapshot đơn giản trong lab không tự giải quyết split-brain.
Không trộn log rollback vào evidence của drill đã nộp.

## Dọn dịch vụ sau diễn tập

```bash
python3 chaos/kill_region.py restore --region a --backend bare
python3 -c 'import pathlib,subprocess; subprocess.run(["bash","-c",pathlib.Path("scripts/down_bare.sh").read_text(),"scripts/down_bare.sh"],check=True)'
```

Log evidence cần nộp gồm hai drill, chaos-events, health-events, failover-events,
replication, runbook-run và measure-drill-1/2.json. Một số file bị .gitignore bỏ qua:
phải bảo đảm chúng có trong gói nộp (nếu dùng Git, thêm đích danh bằng `git add -f`).
