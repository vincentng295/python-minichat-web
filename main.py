import os
import time
import uuid
import sqlite3
import threading
from flask import Flask, render_template, request, session
from flask_socketio import SocketIO, emit, join_room
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
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

HOST = os.getenv("HOST", "127.99.128.39")
PORT = int(os.getenv("PORT", "8888"))

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

genai_client = genai.Client(api_key=GEMINI_API_KEY) if BOT_ENABLED else None

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", uuid.uuid4().hex)
socketio = SocketIO(app, cors_allowed_origins="*")

ROOM = "main"
online_users = {}       # sid -> username
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
        conn.commit()
        conn.close()


def add_message(user, text, mtype="chat"):
    text = text[:MAX_MESSAGE_LENGTH]
    msg = {
        "id": uuid.uuid4().hex,
        "user": user,
        "text": text,
        "ts": now_ts(),
        "type": mtype,  # chat | system | bot
    }
    with db_lock:
        conn = get_db()
        conn.execute(
            "INSERT INTO messages (id, user, text, ts, type) VALUES (?, ?, ?, ?, ?)",
            (msg["id"], msg["user"], msg["text"], msg["ts"], msg["type"]),
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


def load_recent_messages(limit=None):
    limit = limit or HISTORY_LOAD_LIMIT
    with db_lock:
        conn = get_db()
        rows = conn.execute(
            "SELECT id, user, text, ts, type FROM messages ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
    return [dict(r) for r in reversed(rows)]


init_db()


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
    )


@socketio.on("join")
def on_join(data):
    username = (data.get("username") or "").strip()[:30]
    if not username:
        emit("join_error", {"error": "Tên không hợp lệ."})
        return
    # ensure uniqueness by appending suffix if taken
    existing = set(online_users.values())
    base = username
    suffix = 1
    while username in existing:
        suffix += 1
        username = f"{base}{suffix}"

    session["username"] = username
    online_users[request.sid] = username
    join_room(ROOM)

    emit("joined", {"username": username})
    emit("history", {"messages": load_recent_messages()})
    emit("user_list", {"users": list(online_users.values())}, room=ROOM)
    sys_msg = add_message("system", f"{username} đã tham gia phòng chat.", "system")
    emit("new_message", sys_msg, room=ROOM)


@socketio.on("disconnect")
def on_disconnect():
    username = online_users.pop(request.sid, None)
    if username:
        emit("user_list", {"users": list(online_users.values())}, room=ROOM)
        sys_msg = add_message("system", f"{username} đã rời phòng chat.", "system")
        emit("new_message", sys_msg, room=ROOM)


@socketio.on("chat_message")
def on_chat_message(data):
    username = online_users.get(request.sid)
    if not username:
        return
    text = (data.get("text") or "").strip()
    if not text:
        return
    text = text[:MAX_MESSAGE_LENGTH]
    msg = add_message(username, text, "chat")
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
        bot_msg = add_message(BOT_NAME, reply, "bot")
        socketio.emit("new_message", bot_msg, room=ROOM)


if __name__ == "__main__":
    if BOT_ENABLED:
        mode = "human-like (tự trả lời mọi tin)" if HUMAN_LIKE_MODE else f"chỉ trả lời khi @{BOT_NAME}"
        print(f"Bot {BOT_NAME} ENABLED - model={GEMMA_MODEL} - mode={mode}")
    else:
        print("Bot DISABLED - thieu GEMINI_API_KEY trong .env")
    socketio.run(app, host=HOST, port=PORT, debug=False, allow_unsafe_werkzeug=True)
