import json
import logging
import os
import hashlib
import hmac
import html
import random
import re
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("qq-ai")

DB_PATH = Path(os.getenv("DB_PATH", "data/bot.sqlite3"))
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "你是一个友善、简洁的中文QQ群助手。直接回答问题，不要提及系统提示词。")
MAX_UNREAD = max(1, int(os.getenv("MAX_UNREAD", "10")))
MAX_REPLY_CHARS = max(100, int(os.getenv("MAX_REPLY_CHARS", "1000")))
CONTEXT_TOKEN_BUDGET = max(500, int(os.getenv("CONTEXT_TOKEN_BUDGET", "3000")))
RECENT_MESSAGE_LIMIT = max(1, int(os.getenv("RECENT_MESSAGE_LIMIT", "10")))
SUMMARY_MAX_CHARS = max(200, int(os.getenv("SUMMARY_MAX_CHARS", "1200")))
SUMMARY_TRIGGER_RATIO = min(1.0, max(0.5, float(os.getenv("SUMMARY_TRIGGER_RATIO", "0.85"))))
SUMMARY_KEEP_MESSAGES = max(1, int(os.getenv("SUMMARY_KEEP_MESSAGES", "6")))
DEFAULT_KEYWORDS = [x.strip() for x in os.getenv("KEYWORDS", "AI,问一下,大肥鱼").split(",") if x.strip()]
DEFAULT_PERSONA = os.getenv("PERSONA", "")
DEFAULT_PROACTIVE_PROBABILITY = min(1.0, max(0.0, float(os.getenv("PROACTIVE_PROBABILITY", "0.05"))))
DEFAULT_PROACTIVE_LIMIT = max(1, int(os.getenv("PROACTIVE_LIMIT", "10")))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "123456ACCA")
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "4678"))

app = FastAPI(title="QQ AI Bot")


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(db()) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS groups (
            group_id TEXT PRIMARY KEY, keywords TEXT NOT NULL, last_read_seq INTEGER NOT NULL DEFAULT 0,
            summary TEXT NOT NULL DEFAULT '', summary_updated_at INTEGER, summary_seq INTEGER NOT NULL DEFAULT 0,
            persona TEXT NOT NULL DEFAULT '', proactive_probability REAL NOT NULL DEFAULT 0.05,
            proactive_limit INTEGER NOT NULL DEFAULT 10, proactive_active INTEGER NOT NULL DEFAULT 0,
            proactive_remaining INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL, user_id TEXT, message_id TEXT,
            text TEXT NOT NULL, created_at INTEGER, role TEXT NOT NULL DEFAULT 'user'
        );
        CREATE INDEX IF NOT EXISTS idx_messages_group_seq ON messages(group_id, seq);
        CREATE TABLE IF NOT EXISTS admin_auth (
            id INTEGER PRIMARY KEY CHECK (id = 1), password_hash TEXT NOT NULL, salt TEXT NOT NULL
        );
        """)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(groups)")}
        if "summary" not in columns:
            conn.execute("ALTER TABLE groups ADD COLUMN summary TEXT NOT NULL DEFAULT ''")
        if "summary_updated_at" not in columns:
            conn.execute("ALTER TABLE groups ADD COLUMN summary_updated_at INTEGER")
        if "summary_seq" not in columns:
            conn.execute("ALTER TABLE groups ADD COLUMN summary_seq INTEGER NOT NULL DEFAULT 0")
        for name, definition in {
            "persona": "TEXT NOT NULL DEFAULT ''",
            "proactive_probability": "REAL NOT NULL DEFAULT 0.05",
            "proactive_limit": "INTEGER NOT NULL DEFAULT 10",
            "proactive_active": "INTEGER NOT NULL DEFAULT 0",
            "proactive_remaining": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE groups ADD COLUMN {name} {definition}")
        message_columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
        if "role" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
        if conn.execute("SELECT 1 FROM admin_auth WHERE id=1").fetchone() is None:
            salt = secrets.token_hex(16)
            digest = hashlib.md5((ADMIN_PASSWORD + salt).encode("utf-8")).hexdigest()
            conn.execute("INSERT INTO admin_auth(id,password_hash,salt) VALUES (1,?,?)", (digest, salt))
        conn.commit()


def ensure_group(conn: sqlite3.Connection, group_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM groups WHERE group_id=?", (group_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO groups(group_id, keywords, persona, proactive_probability, proactive_limit) VALUES (?, ?, ?, ?, ?)",
            (group_id, json.dumps(DEFAULT_KEYWORDS, ensure_ascii=False), DEFAULT_PERSONA, DEFAULT_PROACTIVE_PROBABILITY, DEFAULT_PROACTIVE_LIMIT),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM groups WHERE group_id=?", (group_id,)).fetchone()
    return row


def delete_group_data(group_id: str) -> None:
    """Remove all persisted state for a group after the bot leaves it."""
    with closing(db()) as conn:
        conn.execute("DELETE FROM messages WHERE group_id=?", (group_id,))
        conn.execute("DELETE FROM groups WHERE group_id=?", (group_id,))
        conn.commit()
    log.info("已清理退出群聊 %s 的配置和消息", group_id)


def extract_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts = []
        for seg in message:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") == "text":
                parts.append(str(seg.get("data", {}).get("text", "")))
            elif seg.get("type") == "at":
                parts.append(f"@{seg.get('data', {}).get('qq')}")
        return "".join(parts)
    return str(message or "")


def mentioned(event: dict) -> bool:
    self_id = str(event.get("self_id", ""))
    message = event.get("message")
    if isinstance(message, list):
        return any(seg.get("type") == "at" and str(seg.get("data", {}).get("qq")) == self_id for seg in message if isinstance(seg, dict))
    return bool(self_id and re.search(rf"\[CQ:at,qq={re.escape(self_id)}(?:,[^]]*)?\]", str(message)))


def is_admin(event: dict) -> bool:
    return (event.get("sender") or {}).get("role") == "owner"


def keyword_command(text: str) -> Optional[tuple[str, str]]:
    m = re.match(r"^!关键词\s+(添加|删除|列表)(?:\s+(.+))?$", text.strip(), re.I)
    return (m.group(1), (m.group(2) or "").strip()) if m else None


def persona_command(text: str) -> Optional[tuple[str, str]]:
    m = re.match(r"^!人设\s*(设置|查看|清除)?(?:\s+(.+))?$", text.strip(), re.I)
    if not m:
        return None
    return (m.group(1) or "查看", (m.group(2) or "").strip())


def save_message(event: dict, text: str, role: str = "user") -> tuple[str, int]:
    group_id = str(event.get("group_id"))
    with closing(db()) as conn:
        ensure_group(conn, group_id)
        cur = conn.execute("INSERT INTO messages(group_id,user_id,message_id,text,created_at,role) VALUES (?,?,?,?,?,?)", (group_id, str((event.get("sender") or {}).get("user_id", "")), str(event.get("message_id", "")), text, int(event.get("time", 0)), role))
        conn.commit()
        return group_id, cur.lastrowid


def unread(group_id: str) -> list[sqlite3.Row]:
    with closing(db()) as conn:
        row = ensure_group(conn, group_id)
        rows = conn.execute("SELECT * FROM messages WHERE group_id=? AND seq>? AND role='user' ORDER BY seq DESC LIMIT ?", (group_id, row["last_read_seq"], MAX_UNREAD)).fetchall()
        return list(reversed(rows))


def mark_read(group_id: str, seq: int) -> None:
    with closing(db()) as conn:
        conn.execute("UPDATE groups SET last_read_seq=MAX(last_read_seq, ?) WHERE group_id=?", (seq, group_id))
        conn.commit()


def group_context(group_id: str) -> tuple[str, list[sqlite3.Row]]:
    with closing(db()) as conn:
        group = ensure_group(conn, group_id)
        rows = conn.execute("SELECT * FROM messages WHERE group_id=? AND seq>? ORDER BY seq DESC LIMIT ?", (group_id, group["summary_seq"], RECENT_MESSAGE_LIMIT)).fetchall()
    return group["summary"], list(reversed(rows))


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 1.5) + 100)


async def maybe_roll_summary(group_id: str, summary: str, rows: list[sqlite3.Row]) -> tuple[str, list[sqlite3.Row]]:
    context = "\n".join(f"[{r['role']}][{r['user_id']}] {r['text']}" for r in rows)
    if estimate_tokens(summary + context) < CONTEXT_TOKEN_BUDGET * SUMMARY_TRIGGER_RATIO or len(rows) <= SUMMARY_KEEP_MESSAGES:
        return summary, rows
    with closing(db()) as conn:
        all_rows = conn.execute("SELECT * FROM messages WHERE group_id=? AND seq>? ORDER BY seq", (group_id, (conn.execute("SELECT summary_seq FROM groups WHERE group_id=?", (group_id,)).fetchone()[0] or 0))).fetchall()
    if len(all_rows) <= SUMMARY_KEEP_MESSAGES:
        return summary, rows
    older = list(all_rows[:-SUMMARY_KEEP_MESSAGES])
    source = "\n".join(f"[{r['role']}][{r['user_id']}] {r['text']}" for r in older)
    summary_prompt = (
        "请把下面的QQ群对话压缩成简短、客观的共享记忆。只保留已确认事实、重要偏好、讨论结论和未解决问题，"
        f"不要超过 {SUMMARY_MAX_CHARS} 个字符，不要输出标题或解释。\n已有摘要：{summary or '无'}\n新增对话：\n{source}"
    )
    try:
        new_summary = await ask_deepseek(summary_prompt, system_prompt="你是对话摘要器，只输出可供后续对话使用的事实摘要。")
        new_summary = new_summary.strip()[:SUMMARY_MAX_CHARS]
        if not new_summary:
            return summary, rows
        boundary = older[-1]["seq"]
        with closing(db()) as conn:
            conn.execute("UPDATE groups SET summary=?, summary_updated_at=?, summary_seq=? WHERE group_id=?", (new_summary, int(time.time()), boundary, group_id))
            conn.execute("DELETE FROM messages WHERE group_id=? AND seq<=?", (group_id, boundary))
            conn.commit()
        return new_summary, list(reversed(all_rows[-SUMMARY_KEEP_MESSAGES:]))
    except Exception:
        log.exception("群 %s 摘要压缩失败，继续使用原始消息", group_id)
        return summary, rows


def update_keywords(group_id: str, action: str, value: str) -> str:
    with closing(db()) as conn:
        row = ensure_group(conn, group_id)
        words = json.loads(row["keywords"])
        if action == "列表":
            return "当前关键词：" + ("、".join(words) if words else "（无）")
        if not value:
            return "请提供关键词。"
        if action == "添加" and value not in words:
            words.append(value)
        elif action == "删除":
            words = [w for w in words if w != value]
        conn.execute("UPDATE groups SET keywords=? WHERE group_id=?", (json.dumps(words, ensure_ascii=False), group_id))
        conn.commit()
        return "已更新。当前关键词：" + ("、".join(words) if words else "（无）")


def update_persona(group_id: str, action: str, value: str) -> str:
    with closing(db()) as conn:
        row = ensure_group(conn, group_id)
        if action == "查看":
            return "当前人设：" + (row["persona"] or "（使用默认人设）")
        if action == "清除":
            value = ""
        elif not value:
            return "请提供人设内容。"
        conn.execute("UPDATE groups SET persona=? WHERE group_id=?", (value, group_id))
        conn.commit()
        return "人设已更新。"


def get_group_config(group_id: str) -> sqlite3.Row:
    with closing(db()) as conn:
        return ensure_group(conn, group_id)


def set_group_config(group_id: str, keywords: list[str], persona: str, probability: float, limit: int) -> None:
    with closing(db()) as conn:
        ensure_group(conn, group_id)
        conn.execute(
            "UPDATE groups SET keywords=?, persona=?, proactive_probability=?, proactive_limit=? WHERE group_id=?",
            (json.dumps([x.strip() for x in keywords if x.strip()], ensure_ascii=False), persona.strip(), min(1.0, max(0.0, probability)), max(1, limit), group_id),
        )
        conn.commit()


def admin_password_valid(password: str) -> bool:
    with closing(db()) as conn:
        row = conn.execute("SELECT password_hash, salt FROM admin_auth WHERE id=1").fetchone()
    return bool(row and hashlib.md5((password + row["salt"]).encode("utf-8")).hexdigest() == row["password_hash"])


def admin_cookie_value() -> str:
    with closing(db()) as conn:
        row = conn.execute("SELECT password_hash, salt FROM admin_auth WHERE id=1").fetchone()
    if not row:
        return ""
    return hmac.new(row["salt"].encode("utf-8"), row["password_hash"].encode("utf-8"), hashlib.sha256).hexdigest()


def effective_system_prompt(persona: str) -> str:
    return SYSTEM_PROMPT + (f"\n群主人设要求：{persona}" if persona.strip() else "")


def should_trigger(event: dict, text: str, group_id: str) -> bool:
    with closing(db()) as conn:
        row = ensure_group(conn, group_id)
        words = json.loads(row["keywords"])
    return mentioned(event) or any(word and word.lower() in text.lower() for word in words)


async def ask_deepseek(prompt: str, system_prompt: Optional[str] = None) -> str:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY 未配置")
    payload = {"model": DEEPSEEK_MODEL, "messages": [{"role": "system", "content": system_prompt or SYSTEM_PROMPT}, {"role": "user", "content": prompt}], "temperature": 0.7, "stream": False}
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(f"{DEEPSEEK_BASE_URL}/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()[:MAX_REPLY_CHARS]


async def send_group_message(ws: WebSocket, group_id: str, message: str) -> None:
    await ws.send_json({"action": "send_group_msg", "params": {"group_id": int(group_id) if group_id.isdigit() else group_id, "message": message}, "echo": f"reply-{group_id}"})


async def handle_event(ws: WebSocket, event: dict) -> None:
    if event.get("post_type") == "notice" and event.get("notice_type") == "group_decrease":
        group_id = str(event.get("group_id", ""))
        user_id = str(event.get("user_id", ""))
        self_id = str(event.get("self_id", ""))
        if group_id and user_id and user_id == self_id:
            delete_group_data(group_id)
        return
    if event.get("post_type") != "message" or event.get("message_type") != "group":
        return
    if str((event.get("sender") or {}).get("user_id", "")) == str(event.get("self_id", "")):
        return
    text = extract_text(event.get("message"))
    group_id, seq = save_message(event, text)
    command = keyword_command(text)
    if command:
        if is_admin(event):
            await send_group_message(ws, group_id, update_keywords(group_id, command[0], command[1]))
        mark_read(group_id, seq)
        return
    pcommand = persona_command(text)
    if pcommand:
        if is_admin(event):
            await send_group_message(ws, group_id, update_persona(group_id, pcommand[0], pcommand[1]))
        mark_read(group_id, seq)
        return
    config = get_group_config(group_id)
    triggered = should_trigger(event, text, group_id)
    proactive = bool(config["proactive_active"] and config["proactive_remaining"] > 0)
    if not triggered and not proactive:
        if random.random() < float(config["proactive_probability"]):
            with closing(db()) as conn:
                conn.execute("UPDATE groups SET proactive_active=1, proactive_remaining=? WHERE group_id=?", (config["proactive_limit"], group_id))
                conn.commit()
            proactive = True
    if not triggered and not proactive:
        return
    rows = unread(group_id)
    if not rows:
        return
    summary, context_rows = group_context(group_id)
    summary, context_rows = await maybe_roll_summary(group_id, summary, context_rows)
    prompt = "共享摘要：" + (summary or "无") + "\n最近群聊：\n" + "\n".join(f"[{r['role']}][{r['user_id']}] {r['text']}" for r in context_rows) + "\n请回复最新问题。"
    try:
        answer = await ask_deepseek(prompt, system_prompt=effective_system_prompt(summary and config["persona"] or config["persona"]))
        await send_group_message(ws, group_id, answer)
        save_message({"group_id": group_id, "sender": {"user_id": "bot"}, "message_id": "", "time": int(time.time())}, answer, role="assistant")
        mark_read(group_id, rows[-1]["seq"])
        if proactive:
            with closing(db()) as conn:
                conn.execute("UPDATE groups SET proactive_remaining=MAX(proactive_remaining-1, 0), proactive_active=CASE WHEN proactive_remaining<=1 THEN 0 ELSE 1 END WHERE group_id=?", (group_id,))
                conn.commit()
    except Exception:
        log.exception("处理群 %s 消息失败", group_id)
        await send_group_message(ws, group_id, "暂时无法回复，请稍后再试。")


@app.on_event("startup")
async def startup() -> None:
    init_db()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "deepseek_configured": bool(DEEPSEEK_API_KEY)}


def logged_in(request: Request) -> bool:
    return request.client is not None and request.client.host in {"127.0.0.1", "::1"} and hmac.compare_digest(request.cookies.get("qq_ai_admin", ""), admin_cookie_value())


def require_local(request: Request) -> None:
    if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="管理页面仅允许本机访问")


@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request) -> str:
    require_local(request)
    return "<html><meta charset='utf-8'><title>QQ AI 管理</title><h2>QQ AI 管理</h2><form method='post'><input type='password' name='password' placeholder='管理密码'><button>登录</button></form></html>"


@app.post("/admin/login")
async def admin_login(request: Request, password: str = Form(...)) -> RedirectResponse:
    require_local(request)
    response = RedirectResponse("/admin", status_code=303)
    if admin_password_valid(password):
        response.set_cookie("qq_ai_admin", admin_cookie_value(), httponly=True, samesite="strict", max_age=86400)
    return response


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request) -> HTMLResponse:
    if not logged_in(request):
        return RedirectResponse("/admin/login", status_code=303)
    with closing(db()) as conn:
        groups = conn.execute("SELECT group_id, keywords, persona, proactive_probability, proactive_limit FROM groups ORDER BY group_id").fetchall()
    rows = []
    for group in groups:
        gid = html.escape(group["group_id"])
        words = html.escape(", ".join(json.loads(group["keywords"])))
        persona = html.escape(group["persona"] or "")
        rows.append(f"<section><h3>群 {gid}</h3><form method='post' action='/admin/group/{gid}'><label>关键词（逗号分隔）</label><br><input name='keywords' value='{words}' style='width:420px'><br><label>人设</label><br><textarea name='persona' rows='4' cols='60'>{persona}</textarea><br><label>主动唤醒概率（0-1）</label><br><input name='probability' value='{group['proactive_probability']}'><br><label>主动讨论条数</label><br><input name='limit' value='{group['proactive_limit']}'><br><button>保存</button></form></section><hr>")
    return HTMLResponse("<html><meta charset='utf-8'><title>QQ AI 管理</title><h2>QQ AI 管理</h2>" + ("".join(rows) or "<p>暂无群配置，机器人收到群消息后会自动创建。</p>") + "</html>")


@app.post("/admin/group/{group_id}")
async def admin_group(request: Request, group_id: str, keywords: str = Form(""), persona: str = Form(""), probability: float = Form(DEFAULT_PROACTIVE_PROBABILITY), limit: int = Form(DEFAULT_PROACTIVE_LIMIT)) -> RedirectResponse:
    if not logged_in(request):
        return RedirectResponse("/admin/login", status_code=303)
    set_group_config(group_id, keywords.split(","), persona, probability, limit)
    return RedirectResponse("/admin", status_code=303)


@app.websocket("/ws")
@app.websocket("/onebot/v11/ws")
async def onebot_ws(ws: WebSocket) -> None:
    await ws.accept()
    log.info("OneBot connected")
    try:
        while True:
            await handle_event(ws, await ws.receive_json())
    except WebSocketDisconnect:
        log.info("OneBot disconnected")
