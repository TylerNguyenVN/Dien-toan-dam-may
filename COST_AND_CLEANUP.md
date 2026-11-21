# Báo Cáo Phân Tích Chi Phí Hàng Tháng & Quy Trình Dọn Dẹp Hạ Tầng

Tài liệu phục vụ tiêu chí đánh giá **Cost analysis and communication (10%)** và **Cleanup instructions** theo đề bài.

---

## 1. Ước Tính Chi Phí Hàng Tháng Theo Workload (Monthly Workload-Based Cost Estimate)

* **Khu vực triển khai (Region)**: `asia-southeast1` (Singapore) hoặc `us-central1` (Iowa).
* **Workload giả định hàng tháng**:
  * Upload & Ingest: 50 file lớn (tương đương 5,000,000 dòng giao dịch).
  * Truy vấn Dashboard: 50,000 requests/tháng.
  * Dung lượng lưu trữ: 5 GB CSV raw + 10 GB CSDL.
  * Thời gian chạy thử nghiệm: 100 giờ chạy container API/Worker.

### Bảng Chi Tiết Dự Toán Trên GCP (Google Cloud Platform)

| Dịch vụ Cloud | Cấu hình / Tier | Thời lượng / Dung lượng | Đơn giá | Ước tính hàng tháng |
|---|---|---|---|---|
| **Cloud Run (API & Worker)** | Serverless (1 vCPU, 512 MB RAM, scale-to-zero) | 100 giờ active compute | Miễn phí 2 triệu requests đầu/tháng (Free Tier) | **$0.00** |
| **Cloud SQL for PostgreSQL** | `db-f1-micro` (0.6 GB RAM, 10 GB SSD disk) | 730 giờ (1 tháng chạy liên tục) | ~$0.0105 / giờ | **$7.67** |
| **Cloud Storage (GCS)** | Standard Regional Storage | 5 GB dữ liệu raw | $0.02 / GB / tháng | **$0.10** |
| **Cloud Pub/Sub (Queue)** | Standard Messaging | ~5,000,000 tin nhắn | Miễn phí 10 GB đầu | **$0.00** |
| **Network Egress (Băng thông ra)** | Internet Egress | ~2 GB dữ liệu trả về client | Miễn phí 1 GB đầu, $0.12/GB | **$0.12** |
| **TỔNG CỘNG** | | | | **~$7.89 / tháng** |

> **Nhận xét kiến trúc**:
> Nhờ sử dụng kiến trúc **Serverless Container (Cloud Run)** có khả năng scale-to-zero khi không có request, chi phí tính toán compute gần như bằng 0 trong Free Tier. Thành phần tốn kém duy nhất là CSDL Managed PostgreSQL (`db-f1-micro`) chỉ chiếm ~$7.89/tháng, hoàn toàn nằm gọn trong tài khoản $300 Free Credit của sinh viên.

---

## 2. Thiết Lập Cảnh Báo Ngân Sách (Budget Alerts)

Để tránh phát sinh chi phí ngoài ý muốn:
1. Tạo Budget Alert trên GCP Billing Console với mức hạn mức **$10.00 USD/tháng**.
2. Thiết lập 3 mốc thông báo qua Email:
   * **50% ($5.00)**: Cảnh báo theo dõi.
   * **90% ($9.00)**: Cảnh báo hạn mức.
   * **100% ($10.00)**: Cảnh báo khẩn cấp để tắt các tài nguyên không dùng.

---

## 3. Quy Trình Dọn Dẹp Hạ Tầng Sau Nghiệm Thu (Cleanup Instructions)

Sau khi giảng viên chấm điểm xong, thực hiện các bước sau để tránh bị trừ tiền:

### Lựa chọn 1: Hủy tự động bằng Terraform
Nếu tạo hạ tầng qua Terraform:
```bash
cd infra/terraform
terraform destroy -auto-approve
```

### Lựa chọn 2: Hủy thủ công qua gcloud CLI
```bash
# 1. Xóa Cloud Run Service
gcloud run services delete csv-analytics-api --region=asia-southeast1 --quiet

# 2. Xóa Cloud SQL Database Instance (thành phần tốn phí nhất)
gcloud sql instances delete csv-analytics-db --quiet

# 3. Xóa Bucket lưu trữ
gcloud storage rm --recursive gs://csv-analytics-cloud-csv-storage

# 4. Kiểm tra lại toàn bộ tài nguyên còn lại để đảm bảo bill = 0
gcloud sql instances list
gcloud run services list
```
Sau 4 lệnh trên, toàn bộ tài nguyên cloud tính phí được xóa hoàn toàn 100%.
