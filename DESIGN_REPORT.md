# BÁO CÁO THIẾT KẾ KIẾN TRÚC VÀ NGHIỆM THU ĐỒ ÁN
## ĐỀ TÀI: CSV ANALYTICS DASHBOARD (PROJECT 09 | INTERMEDIATE)
**Môn học**: Điện toán đám mây (Cloud Computing)  
**Tác giả**: Tyler Nguyễn  
**Mã đề tài**: Final Project 09  

---

## 1. TỔNG QUAN HỆ THỐNG VÀ PHẠM VI ỨNG DỤNG

### 1.1 Mục tiêu
Hệ thống giải quyết bài toán tiếp nhận và phân tích tập dữ liệu giao dịch tài chính quy mô lớn (chuẩn 100,000+ dòng) dưới dạng CSV.
Trọng tâm thiết kế tuân thủ nguyên tắc **Cloud Asynchronous Decoupling**:
* Tách biệt hoàn toàn luồng ghi / tiếp nhận dữ liệu nặng (Ingestion Worker) khỏi luồng đọc / truy vấn dashboard của người dùng (Interactive Web API).
* Đảm bảo tính nhất quán dữ liệu bằng mô hình **Atomic Snapshot Versioning** (`AggregateVersion`), giúp người dùng không bao giờ nhìn thấy trạng thái dữ liệu đang build dở.
* Khả năng chịu lỗi cao (**Fault Tolerance & Failure Resilience**): Container xử lý có thể bị dừng đột ngột giữa chừng nhưng Dashboard phiên bản trước đó vẫn hoạt động bình thường với thời gian gián đoạn bằng không (Zero Downtime).

### 1.2 Người dùng và Ma trận Phân quyền (RBAC)
Hệ thống thiết lập 2 vai trò người dùng (Roles) qua cơ chế xác thực Token / API Key:

| Vai trò (Role) | Tài khoản Demo (Header Bearer) | Quyền hạn nghiệp vụ |
|---|---|---|
| **Dataset Owner** | `owner-token-2026` (`alice_owner`) | Toàn quyền Upload tập dữ liệu CSV mới, kích hoạt tiến trình Ingestion, thực hiện kịch bản Failure Drill. |
| **Analyst** | `analyst-token-2026` (`bob_analyst`) | Xem trực quan hóa Dashboard, tra cứu bảng dữ liệu lỗi Quarantined, chuyển đổi giữa các phiên bản Snapshot, tải file kết quả tổng hợp. **Bị chặn 403 Forbidden** nếu cố tình gọi API upload dữ liệu. |

---

## 2. KIẾN TRÚC HẠ TẦNG VÀ LUỒNG DỮ LIỆU (CLOUD ARCHITECTURE)

### 2.1 Sơ đồ Kiến trúc Logic & Ranh giới Bảo mật (Trust Boundaries & Failure Domains)

```mermaid
flowchart TD
    subgraph PUBLIC_ZONE["Public Internet / Untrusted Zone"]
        Client["Browser Client / curl CLI"]
    end

    subgraph INGRESS_SECURITY["Ingress & Identity Boundary (HTTPS TLS 1.3)"]
        AuthFilter["Token Validation & RBAC (Bearer / OIDC)"]
    end

    subgraph COMPUTE_VPC["Private Cloud VPC / Serverless Compute Domain"]
        API["Managed Web API Service (FastAPI / Cloud Run)"]
        Worker["Async Background Ingestion Worker"]
        Queue["Durable Queue / Background Task Engine"]
    end

    subgraph DATA_PERSISTENCE["Private Data & Storage Domain (Isolated Network)"]
        DB[("Managed PostgreSQL (Cloud SQL / RDS)
        - datasets
        - ingestion_jobs
        - validation_errors
        - aggregate_versions
        - outbox_events")]
        ObjectStore["Private Object Storage (GCS / S3)
        - Raw CSV Files
        - Downloadable Result Summaries"]
    end

    Client -->|1. HTTPS Request| AuthFilter
    AuthFilter -->|2. Authenticated Session| API
    API -->|3. Store Raw File| ObjectStore
    API -->|4. Insert Dataset & Job State| DB
    API -->|5. Handoff Ingestion Task| Queue
    Queue -->|6. Dispatch Job| Worker
    Worker -->|7. Stream Read Chunk| ObjectStore
    Worker -->|8. Validate & Write Errors| DB
    Worker -->|9. Atomic Publish Snapshot| DB
    Client -->|10. Query Published Version (Sub-40ms)| API
    API -->|11. Fast Read Published Index| DB
```

### 2.2 Ranh giới bảo mật & Miền sự cố (Failure Domains)
* **Trust Boundary**: CSDL PostgreSQL và Storage Bucket được đặt trong Private Network/VPC. Không mở port 5432 ra Internet. Web API đóng vai trò cổng kiểm soát duy nhất (Gatekeeper).
* **Failure Domain Isolation**: Worker xử lý chạy độc lập. Nếu Worker bị cạn kiệt RAM (OOM) hoặc bị tắt đột ngột, sự cố được khoanh vùng trong Job đó, không làm sập API hoặc ảnh hưởng đến người dùng đang đọc Dashboard.

---

## 3. MÔ HÌNH DỮ LIỆU VÀ CHÍNH SÁCH XỬ LÝ (DATA MODEL & POLICIES)

### 3.1 Cấu trúc Dữ liệu Đầu vào (CSV Schema)
* `event_id`: Chuỗi định danh giao dịch (UUIDv4).
* `category`: Nhóm giao dịch (`PAYMENT`, `REFUND`, `TRANSFER`, `FEE`, `SUBSCRIPTION`).
* `timestamp_utc`: Định dạng chuẩn ISO 8601 UTC (`YYYY-MM-DDTHH:MM:SSZ`).
* `amount_minor`: Số nguyên (Integer minor unit, ví dụ: cents/xu) để triệt tiêu hoàn toàn sai số dấu phẩy động của số thực.

### 3.2 Chính sách Dữ liệu Lỗi (Malformed-Row & Quarantine Policy)
Một dòng CSV sẽ bị cách ly vào bảng `validation_errors` nếu vi phạm bất kỳ điều kiện nào sau đây:
1. `MISSING_EVENT_ID`: Cột `event_id` bị để trống.
2. `DUPLICATE_EVENT_ID`: `event_id` đã xuất hiện trước đó trong cùng tập dữ liệu.
3. `INVALID_CATEGORY`: Category không nằm trong danh mục 5 nhóm hợp lệ.
4. `BAD_TIMESTAMP_FORMAT` / `INVALID_CALENDAR_DATE`: Sai format ISO 8601 hoặc ngày không tồn tại (ví dụ: ngày 31 tháng 2).
5. `FLOAT_AMOUNT` / `NON_NUMERIC_AMOUNT` / `NON_POSITIVE_AMOUNT`: Giá trị tiền chứa dấu chấm thập phân, chứa chữ cái hoặc không phải số nguyên dương.

### 3.3 Tính Nguyên tử khi Xuất bản (Atomic Aggregate Versioning)
* Trong suốt quá trình Worker tính toán, bản ghi `AggregateVersion` giữ trạng thái `BUILDING`.
* Chỉ khi toàn bộ 100,000 dòng được duyệt xong và tính toán toàn vẹn, trạng thái mới chuyển sang `PUBLISHED` trong một Transaction duy nhất.
* Người dùng truy vấn Dashboard luôn đọc `AggregateVersion` có `status = 'PUBLISHED'` gần nhất.

---

## 4. KẾT QUẢ NGHIỆM THU CÁC BÀI KIỂM THỬ (ACCEPTANCE TEST EVIDENCE)

Toàn bộ 8 ca kiểm thử tự động đã chạy thành công 100% qua lệnh `python -m pytest backend/tests/ -v -s`:

```
============================= TEST SESSION SUMMARY =============================
backend/tests/test_acceptance.py::test_1_exact_agreement_benchmark_100k           PASSED
backend/tests/test_acceptance.py::test_3_idempotency_and_no_double_counting         PASSED
backend/tests/test_acceptance.py::test_failure_drill_and_resilient_publishing     PASSED
backend/tests/test_api_concurrency.py::test_api_health_and_list                   PASSED
backend/tests/test_api_concurrency.py::test_2_process_and_10_concurrent_readers  PASSED
backend/tests/test_security_rbac.py::test_unauthorized_direct_api_call_rejected   PASSED
backend/tests/test_security_rbac.py::test_analyst_forbidden_from_upload           PASSED
backend/tests/test_security_rbac.py::test_dataset_owner_authorized_to_upload     PASSED
============================= 8 PASSED IN 2.23s ================================
```

### Chi tiết số liệu đối soát:

#### Test 1 — So sánh kết quả đối soát độc lập (Exact Agreement):
* **Tập dữ liệu**: `data/benchmark_100k.csv` (100,000 dòng).
* **Mã băm SHA-256**: `920920b3c75e7b73d49c56d942984e1177cccdcadb06be4706f16c070efc1e3c`.
* **Dòng hợp lệ (Accepted)**: `98,500` dòng (khớp 100%).
* **Dòng lỗi bị cách ly (Quarantined)**: `1,500` dòng (khớp 100%).
* **Tổng doanh số nguyên (`grand_total_amount_minor`)**: **`24,637,040,347`** (khớp chính xác 100% từng số nguyên với file `benchmark_100k_metrics.json`).
* **Tốc độ xử lý (Throughput)**: **`288,474 rows/giây`**.

#### Test 2 — Thử nghiệm Tải đồng thời & Độ trễ (Concurrency & Latency):
* **Kịch bản**: 10 luồng người dùng liên tục gửi 150 requests đọc dữ liệu Dashboard trong khi Worker đang xử lý nạp file 100,000 dòng.
* **Thời gian phản hồi trung vị (Median Latency)**: **`19.76 ms`**.
* **Độ trễ phân vị 95 (P95 Latency)**: **`36.46 ms`** (thấp hơn nhiều so với ngưỡng quy định 100ms).

#### Test 3 — Tính lũy thừa và Chống cộng trùng (Idempotency):
* Gửi lệnh retry lặp lại cùng một Job: Hệ thống xác định Job đã `COMPLETED`, trả về ngay lập tức (Idempotent no-op) và **không bị tính trùng doanh số**.

#### Kịch bản diễn tập lỗi (Failure Drill):
* Kích hoạt dừng Worker đột ngột tại dòng thứ 30,000:
  * Job chuyển trạng thái `FAILED`.
  * Snapshot đang tính dở bị hủy bỏ.
  * **Dashboard vẫn phục vụ ổn định phiên bản `v1` đã xuất bản trước đó, không gây gián đoạn dịch vụ**.

#### Kiểm tra Bảo mật API trực tiếp (Security RBAC):
* Gọi API upload không có Token hoặc Token giả mạo: Trả về **`401 Unauthorized`**.
* Gọi API upload bằng Token của Analyst (`analyst-token-2026`): Trả về **`403 Forbidden`**.
* Gọi API upload bằng Token của Dataset Owner (`owner-token-2026`): Trả về **`202 Accepted`**.

---

## 5. DỰ TOÁN CHI PHÍ VÀ QUY TRÌNH DỌN DẸP HẠ TẦNG

### 5.1 Ước tính chi phí hàng tháng (GCP - Singapore `asia-southeast1`)
* **Cloud Run (Serverless API & Worker)**: Miễn phí theo Free Tier (dưới 2 triệu requests/tháng).
* **Cloud SQL for PostgreSQL (`db-f1-micro`)**: ~$7.67 / tháng (10 GB SSD).
* **Cloud Storage (GCS) & Bandwidth Egress**: ~$0.22 / tháng.
* **Tổng ngân sách hàng tháng**: **~$7.89 / tháng** (hoàn toàn được bao gồm trong tài khoản Free Tier của sinh viên).

### 5.2 Dọn dẹp tài nguyên (Cleanup)
Sau khi kết thúc nghiệm thu, chạy lệnh hủy toàn bộ tài nguyên qua Terraform:
```bash
cd infra/terraform
terraform destroy -auto-approve
```

---

## 6. KỊCH BẢN THUYẾT TRÌNH BẢO VỆ ĐỒ ÁN (10 PHÚT)

* **Phút 01 – 03**: Mở Báo cáo, trình bày Sơ đồ Kiến trúc Cloud (Hình vẽ mục 2.1) và giải thích tại sao phải dùng Async Pipeline + Atomic Versioning.
* **Phút 03 – 06**: Mở trình duyệt `http://127.0.0.1:8000`, thực hiện upload file `benchmark_100k.csv`. Chỉ ra các con số KPI, biểu đồ cột/tròn và bảng chi tiết 1,500 dòng bị cách ly do lỗi.
* **Phút 06 – 08**: Demo trực tiếp tính năng **Failure Drill**: Tick chọn ô crash tại dòng 30,000, bấm Upload. Chỉ cho hội đồng thấy banner Actionable Alert xuất hiện và Dashboard vẫn mở xem được phiên bản cũ bình thường.
* **Phút 08 – 10**: Mở Terminal chạy lệnh `python -m pytest backend/tests/ -v -s` để hội đồng chứng kiến toàn bộ 8 bài kiểm thử (Test 1, Test 2, Test 3, RBAC) vượt qua tự động trong 2 giây. Kết thúc bằng bảng chi phí $7.89/tháng.
