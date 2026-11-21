# HƯỚNG DẪN KẾT HỢP CLOUDFLARE + VERCEL + AWS (KIẾN TRÚC DOANH NGHIỆP)
## BẢO MẬT ẨN IP, CHỐNG DDOS VÀ KHÔNG PHỤ THUỘC NỀN TẢNG (ZERO VENDOR LOCK-IN)

Tài liệu này hướng dẫn cách kết hợp cả **Cloudflare** và **Vercel** theo chuẩn thực tế tại các công ty công nghệ lớn, đồng thời giúp Tyler Nguyễn nắm vững nguyên lý để **sau này bỏ Vercel vẫn tự cấu hình được 100%**.

---

## 1. MÔ HÌNH KIẾN TRÚC PHÂN TẦNG (MULTI-LAYER ARCHITECTURE)

```
[Người dùng Internet]
        │
        ▼ (HTTPS)
┌──────────────────────────────────────────────────────────┐
│             TẦNG 1: CLOUDFLARE (CỔNG AN NINH TỔNG)       │
│  - Quản lý DNS gốc của tên miền (Nameservers)           │
│  - Bật Proxy ẩn 100% IP của mọi máy chủ phía sau         │
│  - Tường lửa WAF, Bot Management & chống DDoS không giới hạn │
└──────────────────────────────────────────────────────────┘
        │
   ┌────┴───────────────────────────┐
   │ (Subdomain: analytics.domain)  │ (Subdomain: api.domain)
   ▼                                ▼
┌─────────────────────────┐      ┌─────────────────────────────┐
│ TẦNG 2A: VERCEL         │      │ TẦNG 2B: AWS / GCP BACKEND  │
│ - Chạy Web Dashboard UI │      │ - Xử lý Ingestion 100k dòng │
│ - Tự động CI/CD khi Git │      │ - Database PostgreSQL       │
│ - Tối ưu CDN Frontend   │      │ - Storage S3 lưu trữ CSV    │
└─────────────────────────┘      └─────────────────────────────┘
```

---

## 2. BƯỚC 1: QUẢN LÝ TÊN MIỀN TẠI CLOUDFLARE (BẮT BUỘC ĐỂ KHÔNG BỊ TRÓI BUỘC)

Tại sao phải để Cloudflare quản lý DNS thay vì để Vercel quản lý?
* **Lý do**: Nếu giao Nameserver cho Vercel, khi bạn không dùng Vercel nữa bạn sẽ phải đổi lại Nameserver rất phiền phức.
* **Cách chuẩn**: **Đặt Nameserver ở Cloudflare**. Cloudflare là "trung tâm điều phối". Bạn muốn trỏ web sang Vercel, sang AWS, hay sang máy chủ riêng ở nhà (VPS) chỉ mất **5 giây đổi 1 bản ghi DNS**.

### Các bước làm:
1. Đăng ký tài khoản tại [Cloudflare.com](https://dash.cloudflare.com/).
2. Bấm **Add a Site** $\rightarrow$ Điền tên miền của bạn (ví dụ: `tylertech.xyz`).
3. Vào nhà đăng ký domain (Namecheap, GoDaddy, Porkbun, v.v.) đổi 2 dòng Nameservers sang của Cloudflare (ví dụ: `eva.ns.cloudflare.com`, `will.ns.cloudflare.com`).

---

## 3. BƯỚC 2: TÍCH HỢP CLOUDFLARE VÀ VERCEL (CỰC KỲ ĐƠN GIẢN)

### 3.1 Trên Vercel
1. Vào dự án của bạn trên Vercel $\rightarrow$ **Settings** $\rightarrow$ **Domains**.
2. Nhập tên miền muốn gán cho Dashboard:
   * Ví dụ: `analytics.tylertech.xyz` (hoặc domain chính `tylertech.xyz`).
   * Vercel sẽ hiển thị hướng dẫn: Cần tạo 1 bản ghi `CNAME` trỏ về `cname.vercel-dns.com`.

### 3.2 Trên Cloudflare
1. Vào mục **DNS** $\rightarrow$ **Records** $\rightarrow$ **Add record**:
   * **Type**: `CNAME`
   * **Name**: `analytics` (hoặc `@` nếu dùng domain gốc)
   * **Target**: `cname.vercel-dns.com`
   * **Proxy status**: **BẬT ĐÁM MÂY CAM (Proxied)**.
2. Vào mục **SSL/TLS** trên Cloudflare:
   * **RẤT QUAN TRỌNG**: Chọn chế độ **Full** hoặc **Full (Strict)**.
   * *Lưu ý*: Nếu chọn `Flexible`, Cloudflare và Vercel sẽ bị lỗi lặp chuyển hướng vô tận (Error: Too Many Redirects).

---

## 4. BƯỚC 3: SAU NÀY KHÔNG DÙNG VERCEL NỮA THÌ ĐỔI THẾ NÀO?

Đây là phần then chốt để Tyler Nguyễn làm chủ kiến trúc hạ tầng:

Giả sử sau này bạn chuyển sang dùng **AWS riêng**, **Google Cloud**, hoặc **VPS Nginx riêng** (DigitalOcean / Linode / Server vật lý):

### Bạn KHÔNG CẦN đụng chạm gì đến domain hay nhà đăng ký!
Bạn chỉ việc mở Cloudflare và làm duy nhất 1 thao tác:

1. **Nếu chuyển sang AWS (ECS/ALB)**:
   * Vào Cloudflare DNS $\rightarrow$ Sửa bản ghi `CNAME`:
   * Đổi `cname.vercel-dns.com` $\rightarrow$ thành DNS của AWS: `csv-analytics-alb-xxxx.ap-southeast-1.elb.amazonaws.com`.
2. **Nếu chuyển sang VPS riêng (Docker / Nginx)**:
   * Vào Cloudflare DNS $\rightarrow$ Đổi bản ghi `CNAME` thành bản ghi `A`:
   * Điền địa chỉ IP của VPS (ví dụ: `103.x.x.x`).
   * Vẫn giữ **bật đám mây cam (Proxied)**.

👉 **Kết quả**:
* Web lập tức chuyển sang máy chủ mới mà **không bị gián đoạn (Zero Downtime)**.
* Tường lửa Cloudflare vẫn tiếp tục che giấu IP VPS mới của bạn, chống DDoS như cũ mà không mất 1 xu tiền phí.

---

## 5. FILE CẤU HÌNH VERCEL SẴN TRONG PROJECT

Đã tạo sẵn file **`vercel.json`** tại thư mục gốc của dự án:
```json
{
  "version": 2,
  "name": "csv-analytics-dashboard",
  "builds": [
    {
      "src": "backend/app/main.py",
      "use": "@vercel/python"
    }
  ],
  "routes": [
    {
      "src": "/(.*)",
      "dest": "backend/app/main.py"
    }
  ]
}
```

Khi Tyler Nguyễn import repo này vào Vercel, Vercel sẽ tự động build và chạy cả Web Dashboard lẫn API trực tiếp chỉ trong 1 cú click chuột.
