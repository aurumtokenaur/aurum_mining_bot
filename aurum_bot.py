# aurum_bot.py

import os
import json
import random
import asyncio
import time
import csv
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

# === CONFIG ===
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")  # Ex: "-1001234567890"
DATA_FILE = "aurum_data.json"

# GAME RULES
RESPONSE_TIME = 30
GRACE_SECONDS = 2
MIN_MSG_LEN = 5

DAILY_THRESHOLDS = [20, 40, 60, 100, 100, 100, 50, 80, 120, 150]

DAILY_MAX_WINNERS = 10
ONE_WIN_PER_USER = True

SUNDAY_SPECIAL = True

LEVELS = [
    (0,   "🟢 Starter"),
    (30,  "🥉 Bronze"),
    (50,  "🥈 Silver"),
    (100, "🥇 Golden"),
    (200, "🛡️ Platinum"),
    (300, "💎 Diamond"),
    (500, "👑 Legendary")
]

RANK_SYMBOLS = ["🥇", "🥈", "🥉", "🎯", "🔥", "⚡", "🌟", "🪙", "🚀", "💎"]

PARIS_TZ = ZoneInfo("Europe/Paris")

# === STATE ===
state = {
    "points": {},
    "names": {},
    "history": [],
    "active_drop": None,
    "message_count": 0,
    "drop_index": 0,
    "current_day": None,
    "daily_total": 0,
    "daily_winners": set()
}
state_lock = asyncio.Lock()

# === HELPERS ===
def today_paris_str() -> str:
    return datetime.now(PARIS_TZ).date().isoformat()

def is_sunday_paris(d: date | None = None) -> bool:
    d = d or datetime.now(PARIS_TZ).date()
    return d.weekday() == 6

def is_special_sunday(d: date | None = None) -> bool:
    return SUNDAY_SPECIAL and is_sunday_paris(d)

def ensure_daily_rollover_unlocked():
    day = today_paris_str()
    if state["current_day"] != day:
        state["current_day"] = day
        state["daily_total"] = 0
        state["daily_winners"] = set()
        state["message_count"] = 0
        state["drop_index"] = 0
        print(f"🔄 New day (Europe/Paris): {day} — counters reset.")

def save_data():
    try:
        with open(DATA_FILE, "w") as f:
            json.dump({
                "points": state["points"],
                "names": state["names"],
                "history": state["history"],
                "current_day": state["current_day"]
            }, f)
    except Exception as e:
        print(f"❌ Error saving {DATA_FILE}: {e}")

def get_level(points: int) -> str:
    for threshold, name in reversed(LEVELS):
        if points >= threshold:
            return name
    return "🟢 Starter"

# === DROP LOGIC ===
async def trigger_drop(context: ContextTypes.DEFAULT_TYPE):
    async with state_lock:
        ensure_daily_rollover_unlocked()

        if state["daily_total"] >= DAILY_MAX_WINNERS:
            print("⚠️ Daily limit reached — no more drops.")
            return

        if state["drop_index"] >= len(DAILY_THRESHOLDS):
            print("⚠️ Threshold list exhausted — no more drops.")
            return

        if state["active_drop"]:
            print("⚠️ Drop ignored: already active.")
            return

        end_ts = time.monotonic() + RESPONSE_TIME
        state["active_drop"] = {
            "timestamp": datetime.utcnow().isoformat(),
            "winner": None,
            "message_id": None,
            "end_ts": end_ts
        }

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🪙 Mine coin", callback_data="mine_now")]
    ])

    text = (
        "💥 *Mining drop is live!*\n"
        f"⏳ *Open for {RESPONSE_TIME} seconds.*\n"
        "Click the button below to mine!"
    )

    drop_msg = await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text=text,
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

    async with state_lock:
        state["active_drop"]["message_id"] = drop_msg.message_id
        print(f"⚡ DROP OPENED | msg_id={drop_msg.message_id} | end_ts={state['active_drop']['end_ts']:.3f}")

    await asyncio.sleep(RESPONSE_TIME)

    async with state_lock:
        drop = state.get("active_drop")
        now = time.monotonic()
        if drop and not drop.get("winner"):
            if now >= drop["end_ts"]:
                try:
                    await context.bot.send_message(
                        chat_id=GROUP_CHAT_ID,
                        text="⏳ Time is up! Nobody mined in time."
                    )
                    print("🕒 DROP TIMEOUT (no winner).")
                except Exception as e:
                    print(f"❌ Error sending timeout: {e}")

    await asyncio.sleep(GRACE_SECONDS)

    async with state_lock:
        drop = state.get("active_drop")
        if drop and not drop.get("winner"):
            state["active_drop"] = None
            print(f"🧹 DROP CLEANED after grace.")
            save_data()

# === BUTTON CALLBACK (unchanged core) ===
async def mine_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = (query.data or "").strip()
    user = query.from_user
    uid = str(user.id)

    if data != "mine_now":
        return

    try:
        await query.answer("⛏️ Mining…", cache_time=0)
    except Exception as e:
        print(f"⚠️ query.answer failed: {e}")

    async with state_lock:
        ensure_daily_rollover_unlocked()
        display_name = user.full_name or user.first_name or user.username or uid
        state["names"][uid] = display_name

        drop = state.get("active_drop")
        now = time.monotonic()
        print(f"🖱️ CLICK | {display_name}({uid}) | active_drop={bool(drop)}")

        if not drop:
            try:
                await query.edit_message_text("⛏️ This drop has expired.")
            except: pass
            return

        end_ts = drop.get("end_ts", 0)
        if now > (end_ts + GRACE_SECONDS):
            try:
                await query.edit_message_text("⏳ This drop already expired.")
            except: pass
            return

        if state["daily_total"] >= DAILY_MAX_WINNERS:
            try:
                await query.edit_message_text("⚠️ Daily limit reached. Come back tomorrow.")
            except: pass
            state["active_drop"] = None
            save_data()
            return

        if ONE_WIN_PER_USER and uid in state["daily_winners"]:
            try:
                await query.edit_message_text("🚫 You already mined today. Come back tomorrow!")
            except: pass
            state["active_drop"] = None
            save_data()
            return

        if drop.get("winner"):
            try:
                await query.edit_message_text("💨 Someone already mined this coin.")
            except: pass
            return

        base_points = 2 if is_special_sunday() else 1
        state["active_drop"]["winner"] = uid
        state["points"][uid] = state["points"].get(uid, 0) + base_points
        pts = state["points"][uid]
        lvl = get_level(pts)

        state["daily_total"] += 1
        state["daily_winners"].add(uid)

        print(f"✅ WINNER | {display_name} ({uid}) | +{base_points} | total={pts} | level={lvl}")

        try:
            extra = " 🎉 Sunday special! (+2 points)" if base_points == 2 else ""
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=f"🏁 {display_name} mined the coin!{extra}\nTotal: {pts} pts • Level: {lvl}"
            )
        except: pass

        try:
            await query.edit_message_text("✅ Coin mined successfully!")
        except: pass

        state["history"].append({
            "user_id": uid,
            "username": user.username,
            "display_name": display_name,
            "delta_points": base_points,
            "points": pts,
            "timestamp": datetime.utcnow().isoformat(),
            "special_sunday": (base_points == 2)
        })

        state["active_drop"] = None
        save_data()

# === COMMANDS ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_daily_rollover_unlocked()
    await update.message.reply_text(
        "👋 Welcome to *Aurum Mining Bot*!\n\n"
        "💬 Talk in the group to trigger drops.\n"
        "🪙 When a drop appears, click the button — first click wins.\n"
        f"⏳ Each drop stays open for *{RESPONSE_TIME} seconds*.\n"
        f"📅 Daily rules:\n• Max {DAILY_MAX_WINNERS} coins/day (unique winners)\n• Each user can win only once/day\n"
        "🏆 Use /points, /ranking, /info, /dashboard, /export",
        parse_mode="Markdown"
    )

async def points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_daily_rollover_unlocked()
    uid = str(update.effective_user.id)
    display_name = update.effective_user.full_name or update.effective_user.first_name or update.effective_user.username or uid
    state["names"][uid] = display_name

    pts = state["points"].get(uid, 0)
    lvl = get_level(pts)
    await update.message.reply_text(f"🔢 Points: {pts}\n⭐ Level: {lvl}")

async def ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_daily_rollover_unlocked()
    sorted_users = sorted(state["points"].items(), key=lambda x: x[1], reverse=True)
    if not sorted_users:
        await update.message.reply_text("🏆 Ranking is empty for now. Join the drops!")
        return

    msg_lines = ["🏆 Aurum Mining Ranking"]
    for i, (uid, pts) in enumerate(sorted_users[:10], 1):
        sym = RANK_SYMBOLS[i-1] if i-1 < len(RANK_SYMBOLS) else "•"
        name = state["names"].get(uid, uid)
        msg_lines.append(f"{i}. {sym} {name} — {pts} pts")
    await update.message.reply_text("\n".join(msg_lines))

async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_daily_rollover_unlocked()
    levels_str = "\n".join([f"{name} — {pts} pts" for pts, name in LEVELS])
    text = (
        "📘 *How Aurum Mining works*\n\n"
        "💬 To unlock drops: talk in the group. A drop appears every X messages.\n"
        "🪙 To win: when a drop appears, click the button. First click wins.\n"
        f"⏳ Each drop stays open for *{RESPONSE_TIME} seconds*.\n\n"
        "📅 *Daily limits*\n"
        f"• Max {DAILY_MAX_WINNERS} coins/day (unique winners)\n"
        "• Each user can win only *once per day*\n\n"
        "🏆 *Top-10 Ranking* with medals by position\n"
        "1. 🥇 2. 🥈 3. 🥉 4. 🎯 5. 🔥 6. ⚡ 7. 🌟 8. 🪙 9. 🚀 10. 💎\n\n"
        "⭐ *Game Levels*\n"
        f"{levels_str}\n\n"
        "🎉 Sunday special: some drops on Sundays may be worth 2 coins."
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# === NEW: DASHBOARD & EXPORT ===
async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_daily_rollover_unlocked()
    today = today_paris_str()
    msg = (
        f"📊 *Aurum Mining — Daily Dashboard*\n"
        f"📅 Date: {today} (Europe/Paris)\n\n"
        f"💬 Messages counted: {state['message_count']}\n"
        f"🪙 Drops triggered today: {state['drop_index']}/{len(DAILY_THRESHOLDS)}\n"
        f"👥 Unique winners today: {len(state['daily_winners'])}/{DAILY_MAX_WINNERS}\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def export_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_daily_rollover_unlocked()
    now = datetime.now(PARIS_TZ)
    week_start = (now - timedelta(days=now.weekday())).date()  # Monday
    week_end = week_start + timedelta(days=6)

    filename = f"aurum_export_{week_start}_{week_end}.csv"
    try:
        with open(filename, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["user_id", "display_name", "points", "delta_points", "timestamp", "special_sunday"])
            for row in state["history"]:
                ts = datetime.fromisoformat(row["timestamp"]).date()
                if week_start <= ts <= week_end:
                    writer.writerow([
                        row.get("user_id"),
                        row.get("display_name"),
                        row.get("points"),
                        row.get("delta_points"),
                        row.get("timestamp"),
                        row.get("special_sunday")
                    ])
        await update.message.reply_document(InputFile(filename))
    except Exception as e:
        await update.message.reply_text(f"❌ Error exporting data: {e}")

# === MONITOR MESSAGES ===
async def monitor_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(GROUP_CHAT_ID):
        return

    text = update.message.text or ""
    if len(text.strip()) < MIN_MSG_LEN:
        return

    async with state_lock:
        ensure_daily_rollover_unlocked()

        uid = str(update.effective_user.id)
        display_name = update.effective_user.full_name or update.effective_user.first_name or update.effective_user.username or uid
        state["names"][uid] = display_name

        if state["daily_total"] >= DAILY_MAX_WINNERS:
            return
        if state["drop_index"] >= len(DAILY_THRESHOLDS):
            return

        state["message_count"] += 1
        current_threshold = DAILY_THRESHOLDS[state["drop_index"]]
        print(f"💬 Count: {state['message_count']}/{current_threshold} (idx {state['drop_index']+1}/10)")

        if state["message_count"] >= current_threshold:
            state["message_count"] = 0
            state["drop_index"] += 1
            print(f"🪙 Triggering DROP | next idx={state['drop_index']+1 if state['drop_index']<10 else '—'}")
            context.application.create_task(trigger_drop(context))

# === MAIN ===
app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("points", points))
app.add_handler(CommandHandler("ranking", ranking))
app.add_handler(CommandHandler("info", info))
app.add_handler(CommandHandler("dashboard", dashboard))
app.add_handler(CommandHandler("export", export_data))

app.add_handler(CallbackQueryHandler(mine_button))
app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), monitor_messages))

if __name__ == "__main__":
    print("🤖 Aurum Bot started!")
    print(f"📌 GROUP_CHAT_ID: {GROUP_CHAT_ID}")
    ensure_daily_rollover_unlocked()
    app.run_polling()
