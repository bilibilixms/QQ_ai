import json
import logging
import os
import random
import re
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv():
        pass
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("qq-fishing")
DB_PATH = Path(os.getenv("DB_PATH", "data/bot.sqlite3"))
FISH_COST, INITIAL_POINTS, DAILY_LIMIT, COOLDOWN_SECONDS = 5, 100, 3, 300
CASTS_PER_TRIP, XP_PER_CAST, MAX_LEVEL = 3, 10, 7
MAPS = {"林间小溪": {"cost": 0, "description": "清澈的小溪，适合新手。"}}
SELL_ORDER_TTL = 300
FISH_VALUES = {"小鲫鱼": 5, "草鱼": 8, "鲤鱼": 10, "彩虹锦鲤": 20, "金鱼": 25, "龙鱼": 50, "斗鱼": 60, "深海神龙鱼": 200}
OUTCOMES = [("fish", "小鲫鱼", 60), ("fish", "草鱼", 12.5), ("fish", "鲤鱼", 12.5), ("fish", "彩虹锦鲤", 5), ("fish", "金鱼", 5), ("fish", "龙鱼", 2), ("fish", "斗鱼", 2), ("fish", "深海神龙鱼", 1), ("empty", "空竿", 8), ("points", 0.5, 5), ("points", 2, 3), ("points", 10, 0.2)]
app = FastAPI(title="QQ 群本地钓鱼小游戏")

def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row; conn.execute("PRAGMA foreign_keys=ON"); return conn

def init_db() -> None:
    with closing(db()) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (group_id TEXT NOT NULL, qq_id TEXT NOT NULL, nickname TEXT NOT NULL DEFAULT '', points INTEGER NOT NULL DEFAULT 100, daily_fish_count INTEGER NOT NULL DEFAULT 0, daily_fish_date TEXT NOT NULL DEFAULT '', last_fish_at REAL, registered_at INTEGER NOT NULL, PRIMARY KEY(group_id,qq_id));
        CREATE TABLE IF NOT EXISTS fish_inventory (group_id TEXT NOT NULL, qq_id TEXT NOT NULL, fish_name TEXT NOT NULL, quantity INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(group_id,qq_id,fish_name), FOREIGN KEY(group_id,qq_id) REFERENCES users(group_id,qq_id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS sell_orders (order_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, qq_id TEXT NOT NULL, status TEXT NOT NULL, items TEXT NOT NULL DEFAULT '{}', total INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_sell_orders_user ON sell_orders(group_id,qq_id,status);
        """); conn.commit()
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        for name, definition in (("level", "INTEGER NOT NULL DEFAULT 1"), ("experience", "INTEGER NOT NULL DEFAULT 0"), ("current_map", "TEXT NOT NULL DEFAULT '林间小溪'"), ("map_casts", "INTEGER NOT NULL DEFAULT 0")):
            if name not in columns: conn.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
        conn.commit()

def now_ts() -> int: return int(time.time())
def today() -> str: return datetime.now().date().isoformat()
def user_key(event: dict) -> tuple[str, str]: return str(event.get("group_id", "")), str((event.get("sender") or {}).get("user_id", ""))
def nickname(event: dict) -> str:
    sender = event.get("sender") or {}; return str(sender.get("card") or sender.get("nickname") or "")
def is_owner(event: dict) -> bool: return (event.get("sender") or {}).get("role") == "owner"

def extract_text(message: Any) -> str:
    if isinstance(message, str): return message
    if isinstance(message, list):
        out = []
        for seg in message:
            if isinstance(seg, dict):
                if seg.get("type") == "text": out.append(str((seg.get("data") or {}).get("text", "")))
                elif seg.get("type") == "at": out.append("@" + str((seg.get("data") or {}).get("qq", "")))
        return "".join(out)
    return str(message or "")

def at_users(message: Any) -> list[str]:
    raw = message if isinstance(message, str) else extract_text(message)
    if isinstance(message, list): return list(dict.fromkeys(re.findall(r"@[0-9]+", raw))) and [x[1:] for x in re.findall(r"@[0-9]+", raw)]
    return list(dict.fromkeys(re.findall(r"\[CQ:at,qq=([^,\]]+)[^\]]*\]|@([0-9]{5,})", raw)))

def clean_text(text: str, self_id: str) -> str:
    text = re.sub(r"\[CQ:at,qq=[^\]]+\]", " ", text)
    if self_id: text = re.sub(rf"@{re.escape(self_id)}\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()

async def send(ws: WebSocket, group_id: str, message: str) -> None:
    await ws.send_json({"action":"send_group_msg", "params":{"group_id": int(group_id) if group_id.isdigit() else group_id, "message":message}, "echo":f"reply-{group_id}"})

def register(group_id: str, qq_id: str, name: str) -> str:
    with closing(db()) as conn:
        if conn.execute("SELECT 1 FROM users WHERE group_id=? AND qq_id=?", (group_id,qq_id)).fetchone(): return "你已经注册过了，无需重复注册。"
        conn.execute("INSERT INTO users(group_id,qq_id,nickname,points,registered_at) VALUES(?,?,?,?,?)", (group_id,qq_id,name,INITIAL_POINTS,now_ts())); conn.commit()
    return f"注册成功！初始积分 {INITIAL_POINTS} 分。"

def user(conn, group_id: str, qq_id: str): return conn.execute("SELECT * FROM users WHERE group_id=? AND qq_id=?", (group_id,qq_id)).fetchone()
def xp_to_next(level: int) -> int:
    if level >= MAX_LEVEL: return 0
    return level * (150 if level >= 5 else 100)

def level_text(level: int, experience: int) -> str:
    if level >= MAX_LEVEL: return f"当前等级：7级\n经验：{experience}\n已达到最高等级。"
    return f"当前等级：{level}级\n经验：{experience}\n距离下一级还需：{xp_to_next(level)}点经验。"

def map_list_text() -> str:
    return "可选地图：\n" + "\n".join(f"- {name}（门票 {data['cost']} 积分）{data['description']}" for name, data in MAPS.items()) + "\n发送“选择地图 地图名”进入。"

def enter_map(group_id: str, qq_id: str, map_name: str) -> str:
    map_name = map_name.strip()
    if map_name not in MAPS: return f"暂未开放地图“{map_name}”。\n{map_list_text()}"
    with closing(db()) as conn:
        u = user(conn, group_id, qq_id)
        if u is None: return "你还没有注册，请先发送“注册”。"
        cost = MAPS[map_name]["cost"]
        if u["current_map"] == map_name and u["map_casts"] < CASTS_PER_TRIP:
            return f"你已经在【{map_name}】，本次旅程还可抛竿 {CASTS_PER_TRIP-u['map_casts']} 次。"
        if u["points"] < cost: return f"积分不足，进入【{map_name}】需要 {cost} 分，你当前有 {u['points']} 分。"
        conn.execute("UPDATE users SET points=points-?,current_map=?,map_casts=0 WHERE group_id=? AND qq_id=?", (cost, map_name, group_id, qq_id)); conn.commit()
    return f"已进入【{map_name}】（消耗 {cost} 积分）。本次可抛竿 {CASTS_PER_TRIP} 次。"

def add_experience(conn, group_id: str, qq_id: str, amount: int) -> tuple[int, int, int]:
    row = user(conn, group_id, qq_id)
    level, experience = int(row["level"]), int(row["experience"]) + amount
    gained = 0
    while level < MAX_LEVEL and experience >= xp_to_next(level):
        experience -= xp_to_next(level); level += 1; gained += 1
    conn.execute("UPDATE users SET level=?,experience=? WHERE group_id=? AND qq_id=?", (level, experience, group_id, qq_id))
    return level, experience, gained
def fish_once(group_id: str, qq_id: str, owner: bool) -> str:
    with closing(db()) as conn:
        u = user(conn,group_id,qq_id)
        if u is None: return "你还没有注册，请先发送“注册”。"
        if u["map_casts"] >= CASTS_PER_TRIP: return f"你已完成【{u['current_map']}】本次旅程的 {CASTS_PER_TRIP} 次抛竿，请重新选择地图。"
        if u["daily_fish_date"] != today():
            conn.execute("UPDATE users SET daily_fish_count=0,daily_fish_date=? WHERE group_id=? AND qq_id=?", (today(),group_id,qq_id)); u = user(conn,group_id,qq_id)
        if u["points"] < FISH_COST: return f"积分不足，钓鱼需要 {FISH_COST} 分，你当前有 {u['points']} 分。"
        if not owner:
            if u["daily_fish_count"] >= DAILY_LIMIT: return f"今日钓鱼次数已达上限（{DAILY_LIMIT} 次），明天再来吧。"
            if u["last_fish_at"] and time.time()-float(u["last_fish_at"]) < COOLDOWN_SECONDS:
                remain = int(COOLDOWN_SECONDS-(time.time()-float(u["last_fish_at"]))); return f"还在冷却中，请 {max(1,(remain+59)//60)} 分钟后再试。"
        count = u["daily_fish_count"] + (0 if owner else 1)
        conn.execute("UPDATE users SET points=points-?,daily_fish_count=?,daily_fish_date=?,last_fish_at=? WHERE group_id=? AND qq_id=?", (FISH_COST,count,today(),time.time(),group_id,qq_id))
        kind,value,_ = random.choices(OUTCOMES,weights=[x[2] for x in OUTCOMES],k=1)[0]
        if kind == "fish":
            conn.execute("INSERT INTO fish_inventory(group_id,qq_id,fish_name,quantity) VALUES(?,?,?,1) ON CONFLICT(group_id,qq_id,fish_name) DO UPDATE SET quantity=quantity+1", (group_id,qq_id,value)); detail=f"钓到【{value}】，单鱼价值 {FISH_VALUES[value]} 分。"
        elif kind == "points":
            reward=int(FISH_COST*float(value)); conn.execute("UPDATE users SET points=points+? WHERE group_id=? AND qq_id=?", (reward,group_id,qq_id)); detail=f"获得积分奖励 {reward} 分。"
        else: detail="这次是空竿，没有额外收益。"
        level, experience, levelups = add_experience(conn, group_id, qq_id, XP_PER_CAST)
        casts = int(u["map_casts"]) + 1
        conn.execute("UPDATE users SET map_casts=? WHERE group_id=? AND qq_id=?", (casts, group_id, qq_id))
        conn.commit(); points=conn.execute("SELECT points FROM users WHERE group_id=? AND qq_id=?", (group_id,qq_id)).fetchone()[0]
    trip = f"本次【{u['current_map']}】已抛竿 {casts}/{CASTS_PER_TRIP} 次。"
    if casts >= CASTS_PER_TRIP: trip += "本次旅程结束，请重新选择地图。"
    levelup = f"恭喜升级到 {level} 级！" if levelups else ""
    return f"{detail}\n消耗 {FISH_COST} 分，获得 {XP_PER_CAST} 点经验。当前等级 {level} 级（经验 {experience}）。\n{trip}\n{levelup}" + ("" if owner else f"今日已钓 {count}/{DAILY_LIMIT} 次。")
def inventory_rows(group_id: str, qq_id: str):
    with closing(db()) as conn: rows=conn.execute("SELECT fish_name,quantity FROM fish_inventory WHERE group_id=? AND qq_id=? AND quantity>0 ORDER BY fish_name", (group_id,qq_id)).fetchall()
    return list(rows)
def inventory_text(group_id: str, qq_id: str) -> str:
    rows=inventory_rows(group_id,qq_id)
    if not rows: return "你的鱼塘空空如也，快去钓鱼吧！"
    total=sum(FISH_VALUES[r["fish_name"]]*r["quantity"] for r in rows); lines=["你的鱼塘："]
    lines += [f"{r['fish_name']} × {r['quantity']}（单鱼 {FISH_VALUES[r['fish_name']]} 分，小计 {FISH_VALUES[r['fish_name']]*r['quantity']} 分）" for r in rows]; lines.append(f"鱼塘总积分：{total} 分"); return "\n".join(lines)

def cancel(conn, group_id, qq_id): conn.execute("DELETE FROM sell_orders WHERE group_id=? AND qq_id=?", (group_id,qq_id))
def parse_items(text: str) -> Optional[dict[str,int]]:
    n=text.replace("×","x").replace("＊","x").replace("*","x").replace("，"," ").replace(","," "); matches=re.findall(r"(小鲫鱼|草鱼|鲤鱼|彩虹锦鲤|金鱼|龙鱼|斗鱼|深海神龙鱼)\s*x\s*(\d+)",n)
    if not matches: return None
    items={}
    for name,count in matches:
        if int(count)<=0: return None
        items[name]=items.get(name,0)+int(count)
    return items
def sale_preview(group_id,qq_id,items):
    with closing(db()) as conn:
        if user(conn,group_id,qq_id) is None: return "你还没有注册，请先发送“注册”。"
        for fish,qty in items.items():
            row=conn.execute("SELECT quantity FROM fish_inventory WHERE group_id=? AND qq_id=? AND fish_name=?", (group_id,qq_id,fish)).fetchone()
            if row is None or row["quantity"]<qty: return f"库存不足：{fish} 需要 {qty} 条，当前只有 {row['quantity'] if row else 0} 条。"
        total=sum(FISH_VALUES[n]*q for n,q in items.items()); conn.execute("UPDATE sell_orders SET status='pending',items=?,total=?,expires_at=? WHERE group_id=? AND qq_id=? AND status='selecting'", (json.dumps(items,ensure_ascii=False),total,now_ts()+SELL_ORDER_TTL,group_id,qq_id)); conn.commit()
    return "售卖确认单：" + "、".join(f"{n}×{q}" for n,q in items.items()) + f"\n预计收益：{total} 分\n请发送“确认”完成交易，发送其他内容将取消。"
def confirm_sale(group_id,qq_id):
    with closing(db()) as conn:
        order=conn.execute("SELECT * FROM sell_orders WHERE group_id=? AND qq_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1", (group_id,qq_id)).fetchone()
        if order is None: return "当前没有待确认的卖鱼订单。"
        if order["expires_at"]<now_ts(): cancel(conn,group_id,qq_id); conn.commit(); return "卖鱼订单已超时取消。"
        items=json.loads(order["items"])
        for fish,qty in items.items():
            row=conn.execute("SELECT quantity FROM fish_inventory WHERE group_id=? AND qq_id=? AND fish_name=?", (group_id,qq_id,fish)).fetchone()
            if row is None or row["quantity"]<qty: cancel(conn,group_id,qq_id); conn.commit(); return f"库存发生变化，订单已取消（{fish} 库存不足）。"
        for fish,qty in items.items():
            conn.execute("UPDATE fish_inventory SET quantity=quantity-? WHERE group_id=? AND qq_id=? AND fish_name=?", (qty,group_id,qq_id,fish)); conn.execute("DELETE FROM fish_inventory WHERE group_id=? AND qq_id=? AND fish_name=? AND quantity<=0", (group_id,qq_id,fish))
        conn.execute("UPDATE users SET points=points+? WHERE group_id=? AND qq_id=?", (order["total"],group_id,qq_id)); cancel(conn,group_id,qq_id); conn.commit(); return f"卖鱼成功，到账 {order['total']} 分。"

def leaderboard(group_id):
    with closing(db()) as conn:
        users=conn.execute("SELECT qq_id,nickname FROM users WHERE group_id=?", (group_id,)).fetchall(); scores=[]
        for u in users:
            rows=conn.execute("SELECT fish_name,quantity FROM fish_inventory WHERE group_id=? AND qq_id=? AND quantity>0", (group_id,u["qq_id"])).fetchall(); scores.append((sum(FISH_VALUES[r["fish_name"]]*r["quantity"] for r in rows),u["nickname"] or u["qq_id"],u["qq_id"]))
    scores.sort(key=lambda x:(-x[0],x[2])); return "本群暂无注册用户。" if not scores else "本群鱼塘排行（仅统计鱼塘积分）：\n"+"\n".join(f"{i}. {n}（{q}）— {s} 分" for i,(s,n,q) in enumerate(scores[:10],1))
def player_leaderboard(group_id: str) -> str:
    with closing(db()) as conn:
        rows = conn.execute("SELECT nickname,qq_id,level,experience,points FROM users WHERE group_id=? ORDER BY level DESC, experience DESC, points DESC, qq_id", (group_id,)).fetchall()
    if not rows: return "本群暂无注册用户。"
    return "本群等级排名：\n" + "\n".join(f"{i}. {r['nickname'] or r['qq_id']}（{r['qq_id']}）— {r['level']}级，{r['experience']}经验" for i, r in enumerate(rows[:10], 1))

def help_text() -> str:
    return "钓鱼机器人玩法：\n1. 发送“注册”创建账号。\n2. 发送“地图列表”查看地图，发送“选择地图 林间小溪”进入。\n3. 进入地图后发送“钓鱼”，每次消耗 5 积分，每张地图最多抛竿 3 次。\n4. 钓到的鱼会放入鱼塘，可发送“查看我的鱼塘”“卖鱼”。\n5. 发送“查看个人等级”查看等级经验，发送“查看排名”查看本群等级排行。"
def admin_points(group_id,target,action,amount):
    with closing(db()) as conn:
        u=user(conn,group_id,target)
        if u is None: return "目标用户尚未注册。"
        if action=="清零": new=0
        else:
            if amount is None or amount<=0: return "积分数值必须是正整数。"
            new=u["points"]+(amount if action=="充值" else -amount)
            if new<0: return "扣减积分不能超过用户当前积分。"
        conn.execute("UPDATE users SET points=? WHERE group_id=? AND qq_id=?", (new,group_id,target)); conn.commit(); return f"操作成功：{target} 当前积分 {new} 分。"
def delete_member_data(group_id,qq_id):
    with closing(db()) as conn: conn.execute("DELETE FROM sell_orders WHERE group_id=? AND qq_id=?", (group_id,qq_id)); conn.execute("DELETE FROM fish_inventory WHERE group_id=? AND qq_id=?", (group_id,qq_id)); conn.execute("DELETE FROM users WHERE group_id=? AND qq_id=?", (group_id,qq_id)); conn.commit()
def delete_group_data(group_id):
    """Delete every row owned by a group while preserving global tables."""
    with closing(db()) as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        for row in tables:
            table = row["name"]
            if table == "admin_auth":
                continue
            columns = {info[1] for info in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
            if "group_id" in columns:
                conn.execute(f'DELETE FROM "{table}" WHERE group_id=?', (group_id,))
        conn.commit()
async def handle_event(ws,event):
    if event.get("post_type")=="notice" and event.get("notice_type")=="group_decrease":
        gid=str(event.get("group_id","")); uid=str(event.get("user_id","")); delete_group_data(gid) if uid==str(event.get("self_id","")) else delete_member_data(gid,uid); return
    if event.get("post_type")!="message" or event.get("message_type")!="group" or str((event.get("sender") or {}).get("user_id",""))==str(event.get("self_id","")): return
    gid,uid=user_key(event); text=clean_text(extract_text(event.get("message")),str(event.get("self_id",""))); owner=is_owner(event)
    if not gid or not uid or not text: return
    targets=[x for x in at_users(event.get("message")) if x!=str(event.get("self_id",""))]
    m=re.search(r"(充值|扣减|清零)\s*(\d+)?\s*积分?",text)
    if m and targets: await send(ws,gid,admin_points(gid,targets[0],m.group(1),int(m.group(2)) if m.group(2) else None) if owner else "只有本群群主可以管理积分。"); return
    if text=="注册": await send(ws,gid,register(gid,uid,nickname(event))); return
    if text in {"介绍", "玩法", "帮助", "介绍/玩法"}: await send(ws,gid,help_text()); return
    if text in {"地图列表", "查看地图", "地图"}: await send(ws,gid,map_list_text()); return
    m_map=re.match(r"(?:选择地图|进入地图)\s*(.+)", text)
    if m_map: await send(ws,gid,enter_map(gid,uid,m_map.group(1))); return
    if text in {"查看个人等级", "我的等级", "查看等级"}:
        with closing(db()) as conn:
            u=user(conn,gid,uid); msg="你还没有注册，请先发送“注册”。" if u is None else level_text(int(u["level"]),int(u["experience"]))
        await send(ws,gid,msg); return
    if text in {"查看排名", "等级排行", "排名"}: await send(ws,gid,player_leaderboard(gid)); return
    if text in {"钓鱼","开始钓鱼"}: await send(ws,gid,"正在垂钓中，请稍候"); await send(ws,gid,fish_once(gid,uid,owner)); return
    if text in {"查看我的鱼塘","我的鱼塘","查看鱼塘"}:
        with closing(db()) as conn: msg="你还没有注册，请先发送“注册”。" if user(conn,gid,uid) is None else inventory_text(gid,uid)
        await send(ws,gid,msg); return
    if text=="鱼塘排行": await send(ws,gid,leaderboard(gid)); return
    if text=="卖鱼":
        with closing(db()) as conn:
            if user(conn,gid,uid) is None: msg="你还没有注册，请先发送“注册”。"
            else: cancel(conn,gid,uid); ts=now_ts(); conn.execute("INSERT INTO sell_orders(order_id,group_id,qq_id,status,created_at,expires_at) VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex,gid,uid,"selecting",ts,ts+SELL_ORDER_TTL)); conn.commit(); msg=inventory_text(gid,uid)+"\n\n请输入如“小鲫鱼×2 彩虹锦鲤×1”，或发送“一键卖出”。"
        await send(ws,gid,msg); return
    with closing(db()) as conn: order=conn.execute("SELECT * FROM sell_orders WHERE group_id=? AND qq_id=? ORDER BY created_at DESC LIMIT 1", (gid,uid)).fetchone(); expired=order and order["expires_at"]<now_ts(); cancel(conn,gid,uid) if expired else None; conn.commit() if expired else None
    if not order or expired: return
    if order["status"]=="pending" and text=="确认": await send(ws,gid,confirm_sale(gid,uid))
    elif order["status"]=="selecting" and text=="一键卖出":
        items={r["fish_name"]:r["quantity"] for r in inventory_rows(gid,uid)}; await send(ws,gid,sale_preview(gid,uid,items) if items else "鱼塘没有可售卖的鱼。")
    elif order["status"]=="selecting":
        items=parse_items(text)
        if items: await send(ws,gid,sale_preview(gid,uid,items))
        else:
            with closing(db()) as conn: cancel(conn,gid,uid); conn.commit()
            await send(ws,gid,"输入无效，卖鱼操作已取消。")
    else:
        with closing(db()) as conn: cancel(conn,gid,uid); conn.commit()
        await send(ws,gid,"卖鱼订单已取消。")

@app.on_event("startup")
async def startup(): init_db()
@app.get("/health")
async def health(): return {"status":"ok","mode":"local_fishing","deepseek_configured":False}
@app.websocket("/ws")
@app.websocket("/onebot/v11/ws")
async def onebot_ws(ws: WebSocket):
    await ws.accept(); log.info("OneBot connected")
    try:
        while True: await handle_event(ws,await ws.receive_json())
    except WebSocketDisconnect: log.info("OneBot disconnected")



