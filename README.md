# CSV Analytics Dashboard - Cloud Computing Project

Hệ thống xử lý phân tích dữ liệu CSV lớn (100,000+ dòng) bất đồng bộ với cơ chế Atomic Snapshot Versioning và Fault-Tolerance trên nền tảng Cloud.

## Chạy nhanh
```powershell
# Cài đặt thư viện (nếu chưa có)
pip install -r requirements.txt

# Chạy Web Dashboard và API
python run_server.py
```
Truy cập:
* Web Dashboard: http://127.0.0.1:8000
* Swagger API Docs: http://127.0.0.1:8000/docs

## Chạy bộ kiểm thử nghiệm thu (Test 1, 2, 3 & Failure Drill)
```powershell
python -m pytest backend/tests/ -v -s
```

Xem chi tiết kiến trúc và kịch bản bảo vệ tại [PROJECT_GUIDE.md](PROJECT_GUIDE.md).
