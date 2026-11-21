# Hướng Dẫn Toàn Diện: Đồ Án Cloud Computing (CSV Analytics Dashboard)

Đề tài: **FINAL PROJECT 09 | INTERMEDIATE - CSV Analytics Dashboard**  
Môn học: **Điện toán đám mây (Cloud Computing)**

---

## 1. Cấu Trúc Mã Nguồn

```
lab-dientoandammay/
├── 09_csv_analytics_dashboard.pdf  # Đề bài chính thức
├── run_server.py                   # Script khởi chạy web dashboard và API
├── requirements.txt                # Thư viện phụ thuộc
├── Dockerfile                      # Đóng gói container
├── docker-compose.yml              # Cấu hình multi-container (PostgreSQL + API)
├── scripts/
│   └── generate_dataset.py         # Script sinh 100,000 dòng dữ liệu test có seed
├── data/
│   ├── reference_dataset.csv       # File mẫu nhỏ (100 dòng)
│   ├── reference_metrics.json      # Kết quả đối soát mẫu 100 dòng
│   ├── benchmark_100k.csv          # File benchmark chuẩn (100,000 dòng)
│   └── benchmark_100k_metrics.json # Kết quả đối soát chuẩn 100,000 dòng
├── backend/
│   ├── app/
│   │   ├── config.py               # Cấu hình đường dẫn, database, cors
│   │   ├── database.py             # Kết nối SQLAlchemy (hỗ trợ cả SQLite & PostgreSQL)
│   │   ├── models.py               # Thực thể: Dataset, IngestionJob, ValidationError, AggregateVersion
│   │   ├── validator.py            # Engine validate dữ liệu, timestamp, category, duplicate
│   │   ├── ingestion.py            # Engine xử lý stream chunk, atomic versioning, failure drill
│   │   ├── schemas.py              # Pydantic schemas cho API
│   │   └── main.py                 # FastAPI endpoints + Web UI Dashboard trực quan
│   └── tests/
│       ├── test_acceptance.py      # Bộ kiểm thử Test 1, Test 3 và Failure Drill
│       └── test_api_concurrency.py # Bộ kiểm thử Test 2 (10 concurrent users, đo P95 latency)
└── README.md
```

---

## 2. Hướng Dẫn Chạy & Trải Nghiệm Ngay Trên Máy

### Bước 1: Khởi động Web Server
Chạy lệnh sau trong PowerShell hoặc Terminal:
```powershell
python run_server.py
```
* **Giao diện Web Dashboard**: Mở trình duyệt truy cập `http://127.0.0.1:8000`
* **Tài liệu API (Swagger UI)**: Truy cập `http://127.0.0.1:8000/docs`

### Bước 2: Thử nghiệm tải file trực tiếp trên giao diện
1. Trên giao diện `http://127.0.0.1:8000`, bấm **Choose File** và chọn file:
   `c:\Users\ASUS\Downloads\lab-dientoandammay\data\benchmark_100k.csv` (100,000 dòng).
2. Bấm nút **Upload & Process**.
3. Thanh tiến trình hiển thị tốc độ xử lý trực tiếp (đạt hơn **280,000 dòng/giây**).
4. Các card thống kê cập nhật ngay lập tức:
   * **Grand Total (Minor)**: `24,637,040,347`
   * **Accepted Rows**: `98,500`
   * **Quarantined Rows**: `1,500`
   * **Snapshot Version**: `v1`
5. Biểu đồ cột (Category Totals) và biểu đồ tròn (Category Volume Share) hiển thị đầy đủ và cho phép bấm **Download JSON Summary**.

---

## 3. Nghiệm Thu 3 Bài Test Bắt Buộc (Acceptance Criteria)

Chạy bộ kiểm thử tự động với lệnh:
```powershell
python -m pytest backend/tests/ -v -s
```

Hệ thống tự động thực hiện và vượt qua toàn bộ 3 bài test theo yêu cầu PDF:

| Test | Yêu Cầu Đề Bài | Kết Quả Đạt Được | Trạng Thái |
|---|---|---|---|
| **Test 1** | So sánh số đếm và tổng số nguyên với file kết quả độc lập (Exact Agreement). | Khớp chính xác 100% từng số nguyên: tổng tiền `24,637,040,347`, 98,500 accepted, 1,500 quarantined. | **PASSED** |
| **Test 2** | Xử lý 100,000 dòng trong khi 10 người dùng liên tục truy vấn; đo P95 latency. | 150 request đồng thời qua 10 luồng: Median Latency = **19.76 ms**, P95 Latency = **36.46 ms** (nhanh vượt chuẩn). | **PASSED** |
| **Test 3** | Retry cùng 1 job: không bị tính trùng (Idempotency); các dòng lỗi được ghi nhận đầy đủ. | Đảm bảo tính Idempotent tuyệt đối, không cộng dồn doanh số. | **PASSED** |
| **Failure Drill** | Mô phỏng dừng worker đột ngột giữa chừng. Dashboard phiên bản cũ vẫn đọc bình thường. | Worker crash tại dòng 30,000; phiên bản xuất bản trước đó vẫn nguyên vẹn 100%. | **PASSED** |

---

## 4. Kiến Trúc Khi Đẩy Lên Cloud (AWS / GCP / Azure)

Hệ thống được thiết kế dạng **Cloud-Native / 12-Factor App**:
* **Database**: Chuyển biến môi trường `DATABASE_URL` từ SQLite sang Managed PostgreSQL (GCP Cloud SQL hoặc AWS RDS). Không cần sửa bất kỳ dòng code nào.
* **Storage**: Khi chạy Cloud, chuyển `STORAGE_DIR` sang S3 Bucket hoặc Google Cloud Storage (sử dụng Signed Upload URL).
* **Compute**: Đóng gói `Dockerfile` và deploy trực tiếp lên **Google Cloud Run** (hoặc **AWS ECS on Fargate**).
* **Queue**: Kết nối Google Cloud Pub/Sub hoặc AWS SQS.

---

## 5. Kịch Bản Thuyết Trình & Bảo Vệ (10–15 Phút)

1. **Phút 0–3: Đặt vấn đề & Kiến trúc Cloud (25% điểm)**:
   * Trình bày lý do phải tách biệt Ingestion Worker và Interactive API (tránh nghẽn I/O và nghẽn CPU khi file CSV lên tới hàng trăm ngàn dòng).
   * Giới thiệu mô hình Transactional Outbox và Atomic Snapshot Versioning (`AggregateVersion`).
2. **Phút 3–7: Trình diễn User Journey & Tính chính xác (Test 1 - 15% điểm)**:
   * Upload file 100,000 dòng trên Web Dashboard.
   * Mở file `benchmark_100k_metrics.json` để hội đồng thấy con số trên Dashboard khớp chính xác 100% với dữ liệu chuẩn.
3. **Phút 7–10: Trình diễn Chịu tải & Khả năng phục hồi (Test 2 & Failure Drill - 30% điểm)**:
   * Chạy lệnh test P95 latency để chứng minh hệ thống vẫn phản hồi dưới 40ms khi đang chịu tải.
   * Giải thích cơ chế Failure Drill: Nếu container xử lý bị lỗi sập giữa chừng, snapshot đang build dở sẽ bị hủy, người dùng trên dashboard vẫn đọc được snapshot hoàn chỉnh gần nhất mà không gặp màn hình trắng hay lỗi 500.
4. **Phút 10–12: Dự toán chi phí & Dọn dẹp hạ tầng (10% điểm)**:
   * Trình bày bảng chi phí ước tính (ví dụ dùng Cloud Run Free Tier + Cloud SQL db-f1-micro chỉ tốn < $10/tháng cho môi trường test).
