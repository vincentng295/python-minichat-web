import os
import re
import io
import time
import uuid
import sqlite3
import threading
import subprocess
import importlib
from flask import Flask, render_template, request, session, redirect, url_for, send_file, abort
from flask_socketio import SocketIO, emit, join_room
from dotenv import load_dotenv
from authlib.integrations.flask_client import OAuth
from google import genai
from google.genai import types
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    from PIL import Image
except ImportError:
    Image = None

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_BASEURL = os.getenv("GEMINI_BASE_URL", "").strip()
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "gemma-3-27b-it").strip()
BOT_NAME = os.getenv("BOT_NAME", "Gemma").strip()
BOT_ENABLED = bool(GEMINI_API_KEY)

# Chế độ "giống người thật": bot trả lời mọi tin nhắn (theo cooldown), không cần @tên bot
HUMAN_LIKE_MODE = os.getenv("HUMAN_LIKE_MODE", "false").strip().lower() in ("1", "true", "yes")

BOT_TEMPERATURE = float(os.getenv("BOT_TEMPERATURE", "0.6"))
BOT_MAX_OUTPUT_TOKENS = int(os.getenv("BOT_MAX_OUTPUT_TOKENS", "300"))
# THINKING_LEVEL để trống = tắt; các giá trị hợp lệ tuỳ model (vd: MINIMAL, LOW, MEDIUM, HIGH)
BOT_THINKING_LEVEL = os.getenv("BOT_THINKING_LEVEL", "").strip()
BOT_ENABLE_GOOGLE_SEARCH = os.getenv("BOT_ENABLE_GOOGLE_SEARCH", "false").strip().lower() in ("1", "true", "yes")

# Avatar cho bot (tuỳ chọn). Nếu file tồn tại, ảnh sẽ được tự động crop vuông
# ở giữa (auto cut) và giới hạn tối đa 512x512 khi phục vụ qua route /avatar/bot.
BOT_AVATAR_FILE = os.getenv(
    "BOT_AVATAR_FILE",
    os.path.join(os.path.dirname(__file__), "static", "virtualman.png"),
)
BOT_AVATAR_MAX_SIZE = 512

HOST = os.getenv("HOST", "127.99.128.39")
PORT = int(os.getenv("PORT", "8888"))

# =========================================
# Google OAuth (tuỳ chọn - không bắt buộc)
# =========================================
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_OAUTH_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

# Giới hạn lưu trữ - cấu hình qua .env
MAX_HISTORY = int(os.getenv("MAX_HISTORY_MESSAGES", "20000"))
MAX_MESSAGE_LENGTH = int(os.getenv("MAX_MESSAGE_LENGTH", "2000"))
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "chat.db"))
# Load một số tin gần nhất vào bộ nhớ khi khởi động / gửi cho client mới join
HISTORY_LOAD_LIMIT = int(os.getenv("HISTORY_LOAD_LIMIT", "200"))
# Thời gian tối thiểu (giây) giữa 2 lần bot trả lời, tránh bot trả lời mọi tin nhắn
BOT_REPLY_COOLDOWN_SECONDS = float(os.getenv("BOT_REPLY_COOLDOWN_SECONDS", "10"))
# File chứa system instruction tuỳ chỉnh cho bot, nội dung sẽ được inject vào đầu
BOT_INSTRUCTION_FILE = os.getenv(
    "BOT_INSTRUCTION_FILE",
    os.path.join(os.path.dirname(__file__), "instruction.txt"),
)

# =========================================
# Cloudflare Tunnel (cloudflared)
# =========================================
TUNNEL_ENABLED = os.getenv("TUNNEL", "false").strip().lower() in ("1", "true", "yes")
TUNNEL_HOST = os.getenv("TUNNEL_HOST", "").strip()
TUNNEL_TOKEN = os.getenv("TUNNEL_TOKEN", "").strip()

_cloudflared_proc = None
_cloudflared_url = None


def load_bot_instruction():
    try:
        with open(BOT_INSTRUCTION_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            return content or None
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"Không đọc được BOT_INSTRUCTION_FILE ({BOT_INSTRUCTION_FILE}): {e}")
        return None


BOT_CUSTOM_INSTRUCTION = load_bot_instruction()

genai_client = (
    genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptionsDict(
            base_url=GEMINI_BASEURL,
            base_url_resource_scope=types.ResourceScope.COLLECTION,
        ) if GEMINI_BASEURL else None
    )
    if BOT_ENABLED else None
)
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", uuid.uuid4().hex)
socketio = SocketIO(app, cors_allowed_origins="*")

oauth = OAuth(app)
if GOOGLE_OAUTH_ENABLED:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
else:
    print("[*] GOOGLE_CLIENT_ID/SECRET chưa thiết lập - đăng nhập Google bị tắt (không bắt buộc).")

ROOM = "main"
online_users = {}       # sid -> {"username": str, "avatar": str|None}
db_lock = threading.Lock()
bot_lock = threading.Lock()
last_bot_reply_ts = 0.0


def now_ts():
    return time.time()


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_lock:
        conn = get_db()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                user TEXT NOT NULL,
                text TEXT NOT NULL,
                ts REAL NOT NULL,
                type TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts)")
        # Migration: thêm cột avatar cho DB cũ (tạo trước khi có tính năng Google OAuth)
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
        if "avatar" not in existing_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN avatar TEXT")
        conn.commit()
        conn.close()


def add_message(user, text, mtype="chat", avatar=None):
    text = text[:MAX_MESSAGE_LENGTH]
    msg = {
        "id": uuid.uuid4().hex,
        "user": user,
        "text": text,
        "ts": now_ts(),
        "type": mtype,  # chat | system | bot
        "avatar": avatar,
    }
    with db_lock:
        conn = get_db()
        conn.execute(
            "INSERT INTO messages (id, user, text, ts, type, avatar) VALUES (?, ?, ?, ?, ?, ?)",
            (msg["id"], msg["user"], msg["text"], msg["ts"], msg["type"], msg["avatar"]),
        )
        # Giữ tối đa MAX_HISTORY tin nhắn - xoá bớt tin cũ nhất khi vượt ngưỡng
        conn.execute("""
            DELETE FROM messages WHERE id IN (
                SELECT id FROM messages ORDER BY ts DESC
                LIMIT -1 OFFSET ?
            )
        """, (MAX_HISTORY,))
        conn.commit()
        conn.close()
    return msg


def load_recent_messages(limit=None, for_display=False):
    limit = limit or HISTORY_LOAD_LIMIT
    with db_lock:
        conn = get_db()
        rows = conn.execute(
            "SELECT id, user, text, ts, type, avatar FROM messages ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
    result = [dict(r) for r in reversed(rows)]
    if for_display:
        # Ở chế độ giống người thật, giả lập tin nhắn bot như tin nhắn user bình
        # thường cho client. Dữ liệu gốc trong DB vẫn giữ type="bot" để dùng
        # làm ngữ cảnh (phân biệt role model/user) khi gọi model.
        result = [mask_bot_type(m) for m in result]
    return result


def mask_bot_type(msg):
    """Ở chế độ giống người thật, hiển thị tin nhắn bot cho client như tin nhắn
    chat bình thường (bỏ nhãn/khung riêng của bot). DB và ngữ cảnh gọi model
    vẫn giữ nguyên type="bot" gốc — hàm này chỉ áp dụng cho bản gửi hiển thị."""
    if HUMAN_LIKE_MODE and msg["type"] == "bot":
        msg = dict(msg)
        msg["type"] = "chat"
    return msg


init_db()


# =========================================
# Avatar bot: crop vuông giữa ảnh (auto cut) + giới hạn tối đa 512x512
# =========================================
_bot_avatar_bytes = None
_bot_avatar_mimetype = None
_bot_avatar_mtime = None


def _process_bot_avatar():
    """Đọc BOT_AVATAR_FILE (nếu có), crop vuông ở giữa và resize (chỉ thu nhỏ,
    không phóng to) về tối đa BOT_AVATAR_MAX_SIZE x BOT_AVATAR_MAX_SIZE.
    Kết quả được cache trong bộ nhớ."""
    global _bot_avatar_bytes, _bot_avatar_mimetype, _bot_avatar_mtime

    if not os.path.exists(BOT_AVATAR_FILE):
        _bot_avatar_bytes = None
        return

    if Image is None:
        print("[!] Thiếu thư viện Pillow - không thể xử lý BOT_AVATAR_FILE, bỏ qua avatar bot.")
        _bot_avatar_bytes = None
        return

    try:
        mtime = os.path.getmtime(BOT_AVATAR_FILE)
        if _bot_avatar_bytes is not None and _bot_avatar_mtime == mtime:
            return  # đã cache, ảnh gốc không đổi

        with Image.open(BOT_AVATAR_FILE) as img:
            img = img.convert("RGBA") if img.mode in ("P", "LA") else img.convert("RGB") if img.mode not in ("RGB", "RGBA") else img

            # Crop vuông ở giữa (auto cut cạnh dài hơn)
            w, h = img.size
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            img = img.crop((left, top, left + side, top + side))

            # Chỉ thu nhỏ nếu lớn hơn giới hạn, không phóng to ảnh nhỏ
            if side > BOT_AVATAR_MAX_SIZE:
                img = img.resize((BOT_AVATAR_MAX_SIZE, BOT_AVATAR_MAX_SIZE), Image.LANCZOS)

            buf = io.BytesIO()
            if img.mode == "RGBA":
                img.save(buf, format="PNG", optimize=True)
                mimetype = "image/png"
            else:
                img.save(buf, format="JPEG", quality=88, optimize=True)
                mimetype = "image/jpeg"

        _bot_avatar_bytes = buf.getvalue()
        _bot_avatar_mimetype = mimetype
        _bot_avatar_mtime = mtime
        print(f"[*] Avatar bot đã xử lý: {BOT_AVATAR_FILE} -> {side}x{side} (cắt giữa), "
              f"tối đa {BOT_AVATAR_MAX_SIZE}x{BOT_AVATAR_MAX_SIZE}.")
    except Exception as e:
        print(f"[!] Không xử lý được BOT_AVATAR_FILE ({BOT_AVATAR_FILE}): {e}")
        _bot_avatar_bytes = None


_process_bot_avatar()
BOT_AVATAR_URL = "/avatar/bot" if _bot_avatar_bytes else None


@app.route("/avatar/bot")
def avatar_bot():
    _process_bot_avatar()  # cho phép cập nhật nếu file avatar đổi khi server đang chạy
    if not _bot_avatar_bytes:
        abort(404)
    return send_file(
        io.BytesIO(_bot_avatar_bytes),
        mimetype=_bot_avatar_mimetype,
        max_age=3600,
    )

def call_gemma(prompt, history_context):
    """Gọi model Gemma qua SDK google-genai (client.models.generate_content)."""
    if not BOT_ENABLED or genai_client is None:
        return None

    default_note = (
        "Bạn là một người tham gia trò chuyện tên là '%s' trong một phòng chat "
        "công cộng nhiều người. Trả lời ngắn gọn, tự nhiên, thân thiện bằng "
        "tiếng Việt (trừ khi được hỏi bằng ngôn ngữ khác). Không cần lặp lại câu hỏi."
        % BOT_NAME
    )
    if BOT_CUSTOM_INSTRUCTION:
        # Nội dung instruction.txt được inject vào đầu system instruction
        system_note = BOT_CUSTOM_INSTRUCTION + "\n\n" + default_note
    else:
        system_note = default_note

    contents = []
    for h in history_context[-10:]:
        role = "model" if h["type"] == "bot" else "user"
        text = f"{h['user']}: {h['text']}" if role == "user" else h["text"]
        contents.append(types.Content(role=role, parts=[types.Part.from_text(text=text)]))
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=prompt)]))

    config_kwargs = {
        "temperature": BOT_TEMPERATURE,
        "max_output_tokens": BOT_MAX_OUTPUT_TOKENS,
        "system_instruction": [types.Part.from_text(text=system_note)],
    }
    if BOT_THINKING_LEVEL:
        config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=BOT_THINKING_LEVEL)
    if BOT_ENABLE_GOOGLE_SEARCH:
        config_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]

    generate_content_config = types.GenerateContentConfig(**config_kwargs)

    try:
        response_text = ""
        for chunk in genai_client.models.generate_content_stream(
            model=GEMMA_MODEL,
            contents=contents,
            config=generate_content_config,
        ):
            if chunk.text:
                response_text += chunk.text
        print("Calling Gemini, return:", response_text)
        return response_text.strip() or None
    except Exception as e:
        print("Gemma call failed:", e)
        return None


@app.route("/")
def index():
    return render_template(
        "index.html",
        bot_enabled=BOT_ENABLED,
        bot_name=BOT_NAME,
        max_message_length=MAX_MESSAGE_LENGTH,
        human_like_mode=HUMAN_LIKE_MODE,
        google_oauth_enabled=GOOGLE_OAUTH_ENABLED,
        google_name=session.get("google_name"),
        google_avatar=session.get("google_avatar"),
    )


@app.route("/login/google")
def login_google():
    """Bắt đầu luồng OAuth với Google. Đây là tính năng TUỲ CHỌN - người
    dùng vẫn có thể vào phòng chat bằng username thường mà không cần login."""
    if not GOOGLE_OAUTH_ENABLED:
        return "Đăng nhập Google chưa được cấu hình trên server này.", 404
    redirect_uri = url_for("google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/login/google/callback")
def google_callback():
    if not GOOGLE_OAUTH_ENABLED:
        return "Đăng nhập Google chưa được cấu hình trên server này.", 404
    try:
        token = oauth.google.authorize_access_token()
        userinfo = token.get("userinfo") or {}
    except Exception as e:
        print("Google OAuth callback lỗi:", e)
        return redirect(url_for("index"))

    session["google_id"] = userinfo.get("sub")
    session["google_name"] = (userinfo.get("name") or userinfo.get("email") or "Google User")[:30]
    session["google_avatar"] = userinfo.get("picture")
    session["google_email"] = userinfo.get("email")
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    """Đăng xuất khỏi Google (chỉ xoá session OAuth, không ảnh hưởng tới
    việc vào phòng chat bằng username thường)."""
    session.pop("google_id", None)
    session.pop("google_name", None)
    session.pop("google_avatar", None)
    session.pop("google_email", None)
    return redirect(url_for("index"))


@socketio.on("join")
def on_join(data):
    data = data or {}

    # Tuỳ chọn: nếu client đã đăng nhập Google (session có sẵn từ luồng OAuth)
    # và yêu cầu dùng danh tính Google, ưu tiên tên + avatar từ Google.
    # Nếu không, dùng username tự nhập như trước (hành vi mặc định, không đổi).
    use_google = bool(data.get("use_google")) and bool(session.get("google_name"))
    if use_google:
        username = session["google_name"]
        avatar = session.get("google_avatar")
    else:
        username = (data.get("username") or "").strip()[:30]
        avatar = None
        if not username:
            emit("join_error", {"error": "Tên không hợp lệ."})
            return

    # ensure uniqueness by appending suffix if taken
    existing = {info["username"] for info in online_users.values()}
    base = username
    suffix = 1
    while username in existing:
        suffix += 1
        username = f"{base}{suffix}"

    session["username"] = username
    online_users[request.sid] = {"username": username, "avatar": avatar}
    join_room(ROOM)

    emit("joined", {"username": username, "avatar": avatar})
    emit("history", {"messages": load_recent_messages(for_display=True)})
    emit("user_list", {"users": list(online_users.values())}, room=ROOM)
    sys_msg = add_message("system", f"{username} đã tham gia phòng chat.", "system")
    emit("new_message", sys_msg, room=ROOM)


@socketio.on("disconnect")
def on_disconnect():
    info = online_users.pop(request.sid, None)
    if info:
        username = info["username"]
        emit("user_list", {"users": list(online_users.values())}, room=ROOM)
        sys_msg = add_message("system", f"{username} đã rời phòng chat.", "system")
        emit("new_message", sys_msg, room=ROOM)


@socketio.on("chat_message")
def on_chat_message(data):
    info = online_users.get(request.sid)
    if not info:
        return
    username = info["username"]
    avatar = info.get("avatar")
    text = (data.get("text") or "").strip()
    if not text:
        return
    text = text[:MAX_MESSAGE_LENGTH]
    msg = add_message(username, text, "chat", avatar=avatar)
    emit("new_message", msg, room=ROOM)

    mentioned = BOT_ENABLED and (
        f"@{BOT_NAME.lower()}" in text.lower() or BOT_NAME.lower() in text.lower()
    )
    should_reply = BOT_ENABLED and (HUMAN_LIKE_MODE or mentioned)
    if should_reply and try_reserve_bot_slot():
        socketio.start_background_task(handle_bot_reply, username, text)


def try_reserve_bot_slot():
    """Trả về True nếu đã qua thời gian cooldown và đặt lịch trả lời ngay bây giờ.
    Dùng lock để tránh 2 tin nhắn cùng lúc đều vượt qua kiểm tra cooldown."""
    global last_bot_reply_ts
    with bot_lock:
        now = now_ts()
        if now - last_bot_reply_ts < BOT_REPLY_COOLDOWN_SECONDS:
            return False
        last_bot_reply_ts = now
        return True


def handle_bot_reply(username, text):
    socketio.sleep(0.3)
    recent_context = load_recent_messages(limit=10)
    reply = call_gemma(f"{username}: {text}", recent_context)
    if reply:
        bot_msg = add_message(BOT_NAME, reply, "bot", avatar=BOT_AVATAR_URL)
        # DB vẫn lưu type="bot" để phân biệt role khi xây ngữ cảnh cho model;
        # chỉ mask type khi phát cho client hiển thị (chế độ giống người thật).
        socketio.emit("new_message", mask_bot_type(bot_msg), room=ROOM)


# =========================================
# Cloudflare Tunnel helpers
# =========================================

def ensure_cloudflared_binary():
    """Đảm bảo có file thực thi cloudflared trong thư mục hiện tại.
    Tải về bằng download-cloudflared.py nếu chưa có."""
    local_name = "cloudflared.exe" if os.name == "nt" else "cloudflared"
    local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), local_name)
    if os.path.exists(local_path):
        return local_path

    try:
        downloader = importlib.import_module("download-cloudflared")
        downloader.install_cloudflared()
    except Exception as e:
        print(f"[!] Không thể tự động tải cloudflared: {e}")
        return None

    return local_path if os.path.exists(local_path) else None


def write_cloudflared_config():
    """Sinh config.yml cho named tunnel: trỏ hostname TUNNEL_HOST về server
    chat đang chạy tại HOST:PORT."""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yml")
    config_yml_content = (
        f"tunnel: {TUNNEL_TOKEN}\n\n"
        "ingress:\n"
        f"  - hostname: {TUNNEL_HOST}\n"
        f"    service: http://{HOST}:{PORT}\n"
        "  - service: http_status:404\n"
    )
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(config_yml_content)
    return config_path


def launch_cloudflared(bin_path):
    """Khởi chạy tiến trình cloudflared.
    - Nếu có TUNNEL_TOKEN: chạy named tunnel (domain = TUNNEL_HOST, cấu hình
      sẵn trên Cloudflare dashboard cho token đó), dùng config.yml để trỏ
      hostname về server chat.
    - Nếu không có TUNNEL_TOKEN (hoặc thiếu TUNNEL_HOST): chạy quick tunnel,
      Cloudflare sẽ tự cấp một domain tạm dạng *.trycloudflare.com."""
    if TUNNEL_TOKEN and TUNNEL_HOST:
        config_path = write_cloudflared_config()
        cmd = [bin_path, "tunnel", "--config", config_path, "run", "--token", TUNNEL_TOKEN]
    else:
        if TUNNEL_TOKEN or TUNNEL_HOST:
            print("[!] TUNNEL_HOST hoặc TUNNEL_TOKEN chưa được thiết lập đầy đủ - "
                  "bỏ qua cấu hình named tunnel, dùng domain tạm trycloudflare.com.")
        cmd = [bin_path, "tunnel", "--url", f"http://{HOST}:{PORT}"]

    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def monitor_cloudflared(proc):
    global _cloudflared_url
    ansi_escape = re.compile(r"\x1b\[[0-9;]*[mK]")
    try:
        with proc.stdout:
            for line in iter(proc.stdout.readline, ""):
                clean_line = ansi_escape.sub("", line).strip()
                if not clean_line:
                    continue
                print(f"[CLOUDFLARED] {clean_line}")

                if TUNNEL_TOKEN and TUNNEL_HOST:
                    if _cloudflared_url is None and re.search(r"[Rr]egistered tunnel connection", clean_line):
                        _cloudflared_url = TUNNEL_HOST
                        print("\n" + "=" * 60)
                        print(f" Phòng chat đang chạy tại: https://{TUNNEL_HOST}")
                        print("=" * 60 + "\n")
                    continue

                match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", clean_line)
                if match:
                    new_url = match.group(0)
                    if new_url != _cloudflared_url:
                        _cloudflared_url = new_url
                        print("\n" + "=" * 60)
                        print(f" Phòng chat đang chạy tại: {new_url}")
                        print("=" * 60 + "\n")
    except Exception:
        pass


def start_tunnel_and_watchdog():
    """Chạy trong thread nền: đảm bảo cloudflared luôn sống trong khi app chạy."""
    global _cloudflared_proc

    bin_path = ensure_cloudflared_binary()
    if not bin_path:
        print("[!] TUNNEL=true nhưng không tìm được/tải được cloudflared. Bỏ qua tunnel.")
        return

    _cloudflared_proc = launch_cloudflared(bin_path)
    threading.Thread(target=monitor_cloudflared, args=(_cloudflared_proc,), daemon=True).start()

    while True:
        time.sleep(1)
        if _cloudflared_proc.poll() is not None:
            print("[!] Tiến trình cloudflared dừng đột ngột, đang khởi động lại...")
            _cloudflared_proc = launch_cloudflared(bin_path)
            threading.Thread(target=monitor_cloudflared, args=(_cloudflared_proc,), daemon=True).start()


def stop_tunnel():
    if _cloudflared_proc is not None:
        try:
            _cloudflared_proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    if BOT_ENABLED:
        mode = "human-like (tự trả lời mọi tin)" if HUMAN_LIKE_MODE else f"chỉ trả lời khi @{BOT_NAME}"
        print(f"Bot {BOT_NAME} ENABLED - model={GEMMA_MODEL} - mode={mode}")
    else:
        print("Bot DISABLED - thieu GEMINI_API_KEY trong .env")

    if TUNNEL_ENABLED:
        threading.Thread(target=start_tunnel_and_watchdog, daemon=True).start()
    else:
        print("[*] TUNNEL=false - không chạy cloudflared, chỉ phục vụ nội bộ.")

    try:
        socketio.run(app, host=HOST, port=PORT, debug=False, allow_unsafe_werkzeug=True)
    finally:
        stop_tunnel()