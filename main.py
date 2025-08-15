# -*- coding: utf-8 -*-
# Discord Event Organizer Bot — 本機版（SQLite）
# 功能：建立/加入/退出/顯示/列表/備註/提醒/指派或隨機負責人/刪除
# 用法：先準備 DISCORD_TOKEN（環境變數或 DISCORD_TOKEN.txt 第一行），然後 python main.py

import os
import sqlite3
from datetime import datetime, timedelta, timezone
import random

import discord
from discord.ext import commands, tasks

# =========================
# Token 取得：環境變數優先，否則讀同層 DISCORD_TOKEN.txt 第一行
# =========================
def load_token() -> str | None:
    token = os.getenv("DISCORD_TOKEN")
    if token:
        return token.strip()
    try:
        with open("DISCORD_TOKEN.txt", "r", encoding="utf-8") as f:
            line = f.readline().strip()
            return line or None
    except FileNotFoundError:
        return None

TOKEN = load_token()

# =========================
# Config / Constants
# =========================
TAIPEI = timezone(timedelta(hours=8))          # 台北時區
DB_PATH = os.getenv("DB_PATH", "events.db")    # 本機預設放在同層

intents = discord.Intents.default()
intents.message_content = True                 # 你使用前綴指令需要此 Intent（Portal 要開啟）
bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# Database Setup
# =========================
def ensure_dirs():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER,
    title TEXT,
    proposer TEXT,
    manager TEXT,
    participants TEXT,      -- 以逗號分隔的名稱清單（顯示名稱）
    note TEXT,
    location TEXT,
    event_time_utc TEXT,    -- ISO8601(UTC)
    remind_minutes INTEGER DEFAULT 30,
    reminded INTEGER DEFAULT 0
);
"""

def ensure_column(conn: sqlite3.Connection, col: str, ddl: str) -> None:
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT {col} FROM events LIMIT 1")
    except sqlite3.OperationalError:
        cur.execute(ddl)
        conn.commit()

def init_db() -> tuple[sqlite3.Connection, sqlite3.Cursor]:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.executescript(SCHEMA_SQL)
    conn.commit()
    # 舊版兼容（多次呼叫安全）
    ensure_column(conn, "channel_id",     "ALTER TABLE events ADD COLUMN channel_id INTEGER")
    ensure_column(conn, "location",       "ALTER TABLE events ADD COLUMN location TEXT")
    ensure_column(conn, "event_time_utc", "ALTER TABLE events ADD COLUMN event_time_utc TEXT")
    ensure_column(conn, "remind_minutes", "ALTER TABLE events ADD COLUMN remind_minutes INTEGER DEFAULT 30")
    ensure_column(conn, "reminded",       "ALTER TABLE events ADD COLUMN reminded INTEGER DEFAULT 0")
    return conn, cur

conn, c = init_db()

# =========================
# Utilities
# =========================
def parse_local_time(s: str) -> datetime:
    """'YYYY-MM-DD HH:MM' 視為台北時間，回傳 aware datetime。"""
    dt = datetime.strptime(s.strip(), "%Y-%m-%d %H:%M")
    return dt.replace(tzinfo=TAIPEI)

def names_to_list(s: str | None) -> list[str]:
    if not s:
        return []
    return [x.strip() for x in s.split(",") if x.strip()]

def list_to_names(lst: list[str]) -> str:
    return ", ".join(lst)

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def fmt_dt_local(iso_utc: str | None) -> str:
    if not iso_utc:
        return "未設定"
    return datetime.fromisoformat(iso_utc).astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M")

def is_messageable(ch: object) -> bool:
    return isinstance(ch, (discord.TextChannel, discord.Thread, discord.DMChannel))

# =========================
# Lifecycle & Background Task
# =========================
@bot.event
async def on_ready():
    print(f"Bot 已登入為 {bot.user}")
    if not reminder_loop.is_running():
        reminder_loop.start()

@tasks.loop(seconds=30)
async def reminder_loop():
    now_utc = utc_now()
    rows = c.execute("""
        SELECT * FROM events
        WHERE event_time_utc IS NOT NULL
          AND reminded = 0
    """).fetchall()

    for row in rows:
        if not row["event_time_utc"]:
            continue
        event_dt_utc = datetime.fromisoformat(row["event_time_utc"])
        remind_at = event_dt_utc - timedelta(minutes=(row["remind_minutes"] or 30))
        if now_utc >= remind_at:
            chan = bot.get_channel(row["channel_id"]) if row["channel_id"] else None
            if is_messageable(chan):
                parts = names_to_list(row["participants"])
                await chan.send(
                    f"⏰ **活動提醒**（{row['title']}）\n"
                    f"時間：{fmt_dt_local(row['event_time_utc'])}（台北）\n"
                    f"地點：{row['location'] or '未設定'}\n"
                    f"提議者：{row['proposer']}　負責人：{row['manager'] or '未指定'}\n"
                    f"參加（{len(parts)}）：{list_to_names(parts) or '（目前無）'}\n"
                    f"備註：{row['note'] or '（無）'}"
                )
            else:
                print(f"[reminder] channel {row['channel_id']} not messageable; skipped.")
            c.execute("UPDATE events SET reminded = 1 WHERE id = ?", (row["id"],))
            conn.commit()

# =========================
# Commands
# =========================
@bot.command()
async def ping(ctx):
    await ctx.send("Pong! 🏓")

@bot.command(help="建立：!create_event 標題 | 2025-08-20 19:30 | 地點 | （可選）備註")
async def create_event(ctx, *, payload: str | None = None):
    if not is_messageable(ctx.channel):
        return await ctx.reply("請在文字頻道或討論串內建立活動。")
    if not payload:
        return await ctx.send("用法：`!create_event 標題 | 2025-08-20 19:30 | 地點 | （可選）備註`")

    parts = [p.strip() for p in payload.split("|")]
    if len(parts) < 3:
        return await ctx.send("用法：`!create_event 標題 | 2025-08-20 19:30 | 地點 | （可選）備註`")

    title, time_str, location = parts[0], parts[1], parts[2]
    note = parts[3] if len(parts) >= 4 else ""

    try:
        dt_local = parse_local_time(time_str)
    except ValueError:
        return await ctx.send("時間格式錯誤，請用 `YYYY-MM-DD HH:MM`（例：2025-08-20 19:30）")

    dt_utc = dt_local.astimezone(timezone.utc)
    proposer = ctx.author.display_name
    manager = proposer
    participants = [proposer]

    c.execute("""
        INSERT INTO events (channel_id, title, proposer, manager, participants, note, location, event_time_utc, remind_minutes, reminded)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
    """, (
        ctx.channel.id, title, proposer, manager,
        list_to_names(participants), note, location,
        dt_utc.isoformat(), 30
    ))
    conn.commit()
    event_id = c.lastrowid

    await ctx.send(
        f"✅ 已建立活動 **#{event_id} — {title}**\n"
        f"時間：{dt_local.strftime('%Y-%m-%d %H:%M')}（台北）\n"
        f"地點：{location}\n"
        f"提議者：{proposer}（預設為負責人並加入名單）\n"
        f"備註：{note or '（無）'}\n"
        f"指令：`!join_event {event_id}` `!leave_event {event_id}` `!show_event {event_id}`"
    )

@bot.command(help="加入活動：!join_event <活動ID>")
async def join_event(ctx, event_id: int):
    member_name = ctx.author.display_name
    row = c.execute("SELECT title, participants FROM events WHERE id = ?", (event_id,)).fetchone()
    if not row:
        return await ctx.send("❌ 找不到這個活動。")

    event_title = row["title"]
    parts = names_to_list(row["participants"])
    if member_name in parts:
        return await ctx.send(f"你已經在活動 #{event_id} 《{event_title}》名單中了！")

    parts.append(member_name)
    c.execute("UPDATE events SET participants = ? WHERE id = ?", (list_to_names(parts), event_id))
    conn.commit()
    await ctx.send(f"🙋 {member_name} 已加入活動 #{event_id} 《{event_title}》")

@bot.command(help="退出活動：!leave_event <活動ID>")
async def leave_event(ctx, event_id: int):
    row = c.execute("SELECT participants, manager FROM events WHERE id = ?", (event_id,)).fetchone()
    if not row:
        return await ctx.send("❌ 找不到這個活動。")

    parts = names_to_list(row["participants"])
    name = ctx.author.display_name
    if name not in parts:
        return await ctx.send("你不在此活動名單中。")

    parts = [p for p in parts if p != name]
    new_manager = row["manager"]
    if name == row["manager"]:
        new_manager = None

    c.execute("UPDATE events SET participants = ?, manager = ? WHERE id = ?",
              (list_to_names(parts), new_manager, event_id))
    conn.commit()

    msg = f"🙆 {name} 已退出活動 #{event_id}"
    if new_manager is None:
        msg += "（原負責人離隊，負責人已清空）"
    await ctx.send(msg)

@bot.command(help="指定負責人（需在參加名單中）：!set_manager <活動ID> <成員名稱>")
async def set_manager(ctx, event_id: int, *, who: str):
    row = c.execute("SELECT participants FROM events WHERE id = ?", (event_id,)).fetchone()
    if not row:
        return await ctx.send("❌ 找不到這個活動。")

    parts = names_to_list(row["participants"])
    if who not in parts:
        return await ctx.send("此人不在參加名單中（請先 `!join_event`）。")

    c.execute("UPDATE events SET manager = ? WHERE id = ?", (who, event_id))
    conn.commit()
    await ctx.send(f"🧭 已將活動 #{event_id} 的負責人設定為：{who}")

@bot.command(help="隨機抽負責人（從參加者）：!random_manager <活動ID>")
async def random_manager(ctx, event_id: int = None):
    if event_id is None:
        return await ctx.send("請輸入活動 ID，例如：`!random_manager 3`")
    row = c.execute("SELECT title, participants FROM events WHERE id = ?", (event_id,)).fetchone()
    if not row:
        return await ctx.send("❌ 找不到這個活動。")

    parts = names_to_list(row["participants"])
    if not parts:
        return await ctx.send("目前沒有參加者可供抽選。")

    who = random.choice(parts)
    c.execute("UPDATE events SET manager = ? WHERE id = ?", (who, event_id))
    conn.commit()
    await ctx.send(f"🎲 活動《{row['title']}》隨機抽中的負責人：**{who}**")

@bot.command(help="新增/覆寫備註：!add_note <活動ID> <備註>")
async def add_note(ctx, event_id: int, *, note: str):
    row = c.execute("SELECT id FROM events WHERE id = ?", (event_id,)).fetchone()
    if not row:
        return await ctx.send("❌ 找不到這個活動。")
    c.execute("UPDATE events SET note = ? WHERE id = ?", (note, event_id))
    conn.commit()
    await ctx.send(f"📝 已更新活動 #{event_id} 的備註。")

@bot.command(help="設定幾分鐘前提醒：!set_reminder <活動ID> <分鐘(0~1440)>")
async def set_reminder(ctx, event_id: int, minutes: int):
    if minutes < 0 or minutes > 24 * 60:
        return await ctx.send("提醒分鐘必須在 0~1440。")
    row = c.execute("SELECT id FROM events WHERE id = ?", (event_id,)).fetchone()
    if not row:
        return await ctx.send("❌ 找不到這個活動。")
    c.execute("UPDATE events SET remind_minutes = ?, reminded = 0 WHERE id = ?",
              (minutes, event_id))
    conn.commit()
    await ctx.send(f"⏱ 已將活動 #{event_id} 的提醒設定為 **{minutes} 分鐘前**。")

@bot.command(help="顯示活動摘要：!show_event <活動ID>")
async def show_event(ctx, event_id: int):
    row = c.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not row:
        return await ctx.send("❌ 找不到這個活動。")
    parts = names_to_list(row["participants"])
    await ctx.send(
        f"🎬 活動 #{row['id']} — {row['title']}\n"
        f"時間：{fmt_dt_local(row['event_time_utc'])}（台北）\n"
        f"地點：{row['location'] or '未設定'}\n"
        f"提議者：{row['proposer']}　負責人：{row['manager'] or '未指定'}\n"
        f"參加者（{len(parts)}）：{list_to_names(parts) or '（目前無）'}\n"
        f"備註：{row['note'] or '（無）'}\n"
        f"提醒：{row['remind_minutes'] or 30} 分鐘前"
    )

@bot.command(help="列出最近 10 筆活動：!list_events")
async def list_events(ctx):
    rows = c.execute("SELECT id, title, event_time_utc FROM events ORDER BY id DESC LIMIT 10").fetchall()
    if not rows:
        return await ctx.send("目前沒有活動。")
    lines = [f"#{r['id']}  {r['title']}  /  {fmt_dt_local(r['event_time_utc'])}" for r in rows]
    await ctx.send("📅 近期活動：\n" + "\n".join(lines))

@bot.command(help="刪除活動（提議者/負責人或管理員）：!delete_event <活動ID> confirm")
async def delete_event(ctx, event_id: int, confirm: str | None = None):
    row = c.execute("SELECT proposer, manager FROM events WHERE id = ?", (event_id,)).fetchone()
    if not row:
        return await ctx.send("❌ 找不到這個活動。")

    display = ctx.author.display_name
    is_owner = display == row["proposer"] or display == row["manager"]
    perms = getattr(ctx.author.guild_permissions, "manage_guild", False) or getattr(ctx.author.guild_permissions, "administrator", False)
    if not (is_owner or perms):
        return await ctx.send("此操作僅限提議者、負責人，或具有伺服器管理權限的成員執行。")

    if confirm != "confirm":
        return await ctx.send(f"⚠️ 這會永久刪除活動 #{event_id}。若確定，請輸入：\n`!delete_event {event_id} confirm`")

    c.execute("DELETE FROM events WHERE id = ?", (event_id,))
    conn.commit()
    await ctx.send(f"🗑️ 已刪除活動 #{event_id}")

# =========================
# Entrypoint
# =========================
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("請設定環境變數 DISCORD_TOKEN，或在同層放 DISCORD_TOKEN.txt（第一行為 token）")
    bot.run(TOKEN)
