# Phòng Chat Chung với Trợ Lý Ảo & Đăng Nhập Google

Ứng dụng phòng chat thời gian thực (Real-time Chat) được xây dựng bằng **Python Flask**, **Flask-SocketIO**, tích hợp **Google OAuth** (đăng nhập bằng Google) và trợ lý ảo thông minh sử dụng **Google GenAI SDK** (`google-genai`).

---

## Các Tính Năng Chính

* **Phòng chat thời gian thực**: Sử dụng Socket.IO để truyền tải tin nhắn tức thì, hiển thị danh sách người dùng online và thông báo người vào/rời phòng.
* **Đăng nhập Google (Tùy chọn)**: Hỗ trợ xác thực tài khoản Google qua OpenID Connect (Authlib) với avatar và tên hiển thị tự động. Người dùng vẫn có thể chọn tham gia nhanh bằng tên vãng lai nếu không cấu hình OAuth.
* **Trợ lý ảo tích hợp (Gemma/Gemini)**:
  * Hỗ trợ chế độ `@tên_bot` hoặc chế độ trò chuyện tự nhiên giống người thật (`HUMAN_LIKE_MODE`).
  * Tự động xử lý lịch sử hội thoại, tuỳ chỉnh system instruction từ file ngoài (`instruction.txt`).
* **Cloudflare Tunnel**: Hỗ trợ tự động public ứng dụng ra internet thông qua `cloudflared` (Quick Tunnel hoặc Named Tunnel).

---

## Hướng Dẫn Cài Đặt & Triển Khai

### 1. Clone hoặc chuẩn bị mã nguồn
### 2. Cài đặt môi trường ảo và các gói phụ kiện

Mở terminal tại thư mục dự án và chạy các lệnh sau:

```bash
# Tạo môi trường ảo (khuyên dùng)
python -m venv venv

# Kích hoạt môi trường ảo
# Trên Windows:
venv\Scripts\activate
# Trên Linux/macOS:
source venv/bin/activate

# Cài đặt các thư viện
pip install -r requirements.txt
```

### 3. Cấu hình tệp môi trường (`.env`)

Tạo một tệp tên là `.env` ở thư mục gốc của dự án và điền các thông tin cấu hình phù hợp, xem file [.env.example](.env.example)

> **Lưu ý về Google OAuth:** Để lấy `GOOGLE_CLIENT_ID` và `GOOGLE_CLIENT_SECRET`, bạn cần tạo một Project trên [Google Cloud Console](https://console.cloud.google.com/), cấu hình màn hình đồng thuận OAuth (OAuth consent screen) và tạo thông tin xác thực loại *Web application*. Thêm URI chuyển hướng (Redirect URI) trỏ tới: `http://<domain_hoac_ip_cua_ban>:8888/login/google/callback`.

---

## Chạy Ứng Dụng

Khởi chạy ứng dụng bằng lệnh:

```bash
python main.py
```

* Truy cập phòng chat qua trình duyệt tại địa chỉ: `http://127.0.0.1:8888`
* Nếu bạn bật `TUNNEL=true` trong `.env`, ứng dụng sẽ tự động tải `cloudflared` (nếu chưa có) và hiển thị một đường dẫn công khai trực tuyến qua terminal.
