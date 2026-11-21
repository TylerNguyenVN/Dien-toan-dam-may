# HƯỚNG DẪN TRIỂN KHAI AWS + CLOUDFLARE
## ẨN IP GỐC, CHỐNG DDOS VÀ TỰ ĐỘNG CẬP NHẬT (AUTO UPDATE / CI-CD)

Tài liệu này hướng dẫn chi tiết cách cấu hình theo chuẩn doanh nghiệp (Enterprise Architecture) để đáp ứng trọn vẹn yêu cầu bảo vệ đồ án Cloud Computing.

---

## 1. NGUYÊN LÝ HOẠT ĐỘNG (ARCHITECTURAL FLOW)

```
[Người dùng / Attacker]
         │
         ▼ (HTTPS)
┌──────────────────────────────────────────────┐
│       CLOUDFLARE EDGE NETWORK                │
│  - Che giấu 100% IP của máy chủ AWS          │
│  - Chặn tấn công DDoS L3/L4/L7 (Layer 7 WAF) │
│  - Bật SSL/TLS Certificate tự động           │
└──────────────────────────────────────────────┘
         │
         ▼ (Chỉ cho phép dải IP Cloudflare)
┌──────────────────────────────────────────────┐
│       AWS APPLICATION LOAD BALANCER          │
│  - Security Group: CHẶN MỌI IP LẠ            │
│  - Chỉ mở cổng 443 cho Cloudflare CIDRs      │
└──────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────┐
│       AWS ECS FARGATE (Private Subnet)       │
│  - Chạy Container FastAPI Backend            │
│  - Không có Public IP                        │
└──────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────┐
│       AWS RDS POSTGRESQL (Private Subnet)    │
│  - Cách ly hoàn toàn khỏi Internet           │
└──────────────────────────────────────────────┘
```

---

## 2. BƯỚC 1: CẤU HÌNH CLOUDFLARE ĐỂ ẨN IP & CHỐNG DDOS

### 2.1 Trỏ tên miền vào Cloudflare
1. Đăng ký tài khoản miễn phí tại [https://dash.cloudflare.com/](https://dash.cloudflare.com/).
2. Thêm tên miền của bạn (ví dụ: `myprojectcloud.xyz` hoặc domain trường cấp).
3. Đổi Nameservers của domain sang 2 Nameservers của Cloudflare (ví dụ: `bob.ns.cloudflare.com`).

### 2.2 Tạo bản ghi DNS Proxy (Bật đám mây cam)
1. Vào tab **DNS** $\rightarrow$ **Records** $\rightarrow$ **Add record**:
   * **Type**: `CNAME`
   * **Name**: `@` (hoặc `analytics`)
   * **Target**: Điền DNS Name của AWS ALB (ví dụ: `csv-analytics-alb-123456789.ap-southeast-1.elb.amazonaws.com`).
   * **Proxy status**: **Bật biểu tượng đám mây màu cam (Proxied)**.
2. **Tác dụng**: Khách truy cập hoặc kẻ tấn công `ping myprojectcloud.xyz` sẽ chỉ nhận được địa chỉ IP Anycast của Cloudflare (ví dụ `104.21.x.x`), hoàn toàn **không bao giờ thấy IP thật của AWS**.

### 2.3 Bật tính năng chống DDoS & WAF
1. Vào mục **Security** $\rightarrow$ **Settings**:
   * **Security Level**: Chọn `Medium` hoặc `High`.
   * Khi bị tấn công dồn dập, chuyển sang `Under Attack Mode` (Cloudflare sẽ hiện màn hình xác minh JavaScript tự động).
2. Vào mục **SSL/TLS**: Chọn chế độ **Full (Strict)** để mã hóa 2 chiều:
   * Client $\leftrightarrow$ Cloudflare (HTTPS)
   * Cloudflare $\leftrightarrow$ AWS Load Balancer (HTTPS).

---

## 3. BƯỚC 2: KHÓA CHẶT AWS SECURITY GROUP (CHỈ CHO CLOUDFLARE VÀO)

Nếu kẻ tấn công bằng cách nào đó biết được IP gốc của AWS ALB, chúng sẽ tìm cách gọi thẳng vào IP đó để bypass Cloudflare.

Để triệt tiêu lỗ hổng này:
1. File cấu hình Terraform đã viết sẵn tại **`infra/terraform/aws_main.tf`** (dòng 80–120).
2. Quy định **AWS Security Group** chỉ mở cổng 443 cho đúng danh sách IP chính thức của Cloudflare:
   ```
   173.245.48.0/20
   103.21.244.0/22
   103.22.200.0/22
   103.31.4.0/22
   141.101.64.0/18
   108.162.192.0/18
   190.93.240.0/20
   188.114.96.0/20
   197.234.240.0/22
   198.41.128.0/17
   162.158.0.0/15
   104.16.0.0/13
   104.24.0.0/14
   172.64.0.0/13
   131.0.72.0/22
   ```
3. **Kết quả**: Mọi gói tin gửi trực tiếp đến IP của AWS mà không đi qua Cloudflare sẽ bị tường lửa AWS hủy bỏ (Drop Connection) ngay lập tức.

---

## 4. BƯỚC 3: CẤU HÌNH TỰ ĐỘNG CẬP NHẬT (AUTO UPDATE / CI-CD)

Đã tạo sẵn file GitHub Actions: **[`.github/workflows/deploy-aws-ecs.yml`](file:///.github/workflows/deploy-aws-ecs.yml)**.

### Quy trình tự động hóa hoạt động như sau:
1. **Developer đẩy code**: Tyler Nguyễn sửa code và gõ:
   ```bash
   git add .
   git commit -m "Update analytics dashboard"
   git push origin main
   ```
2. **GitHub Actions tự động kích hoạt**:
   * Chạy tự động 8 bài kiểm thử chấp nhận (Test 1, Test 2, Test 3, Security RBAC) bằng `pytest`.
   * Nếu có bất kỳ lỗi nào, pipeline dừng ngay lập tức $\rightarrow$ **Không bao giờ deploy code lỗi lên production**.
   * Nếu pass: Đăng nhập vào **Amazon ECR** (Elastic Container Registry) và build Docker image mới với commit SHA.
3. **Rolling Update không gián đoạn (Zero Downtime)**:
   * GitHub Actions gửi chỉ thị cập nhật Task Definition sang **Amazon ECS**.
   * ECS khởi động container mới song song với container cũ.
   * ECS gửi health probe đến `/health` của container mới. Khi container mới báo healthy, ECS chuyển dần traffic từ container cũ sang container mới, sau đó tắt container cũ.
   * Người dùng đang xem Dashboard không nhận thấy bất kỳ sự gián đoạn nào.

---

## 5. THIẾT LẬP GITHUB SECRETS ĐỂ CI-CD CHẠY ĐƯỢC

Trên GitHub Repository của dự án, vào **Settings** $\rightarrow$ **Secrets and variables** $\rightarrow$ **Actions** $\rightarrow$ Thêm 2 Secrets:
* `AWS_ACCESS_KEY_ID`: Access Key của IAM User có quyền ECS/ECR trên AWS.
* `AWS_SECRET_ACCESS_KEY`: Secret Access Key tương ứng.

Chỉ cần cấu hình một lần, từ đó về sau mọi lần sửa code chỉ cần `git push` là hệ thống trên Cloud sẽ **tự động cập nhật 100%**.
