"""
EONIX PYTHON SPAM.py
Copyright (c) 2023 EONIX
Telegram-controlled Playwright Instagram group-renamer bot.
Fixed issues: Removed fake credentials, added error handling, consolidated code, updated selectors.
WARNING: Violates Instagram TOS. Use ethically and at your own risk.
"""

import asyncio
import logging
import os
import random
import sys
import time
from collections import deque
from itertools import count
from typing import List, Optional

# ---- Telegram imports ----
try:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes
except Exception:
    print(
        "\nERROR: Wrong package installed.\n"
        "Fix:\n"
        "  pip uninstall telegram -y\n"
        "  pip install python-telegram-bot==20.4 playwright\n"
        "  python -m playwright install chromium\n"
    )
    raise

# ---- Playwright imports ----
try:
    from playwright.async_api import async_playwright, Page, Locator
except Exception:
    print("ERROR: Playwright missing. Run: pip install playwright && python -m playwright install chromium")
    raise

# ================= CONFIG =================
# REPLACE THESE WITH REAL VALUES!
BOT_TOKEN = os.getenv("BOT_TOKEN", "REPLACE_WITH_REAL_BOT_TOKEN_FROM_BOTFATHER")  # e.g., "123456789:ABCdef..."
ADMIN_ID = int(os.getenv("ADMIN_ID", "REPLACE_WITH_YOUR_TELEGRAM_USER_ID"))  # e.g., 123456789
AUTO_STATS_INTERVAL = 30  # seconds
RESTART_DURATION = 300
RENAME_DELAY = 0.0001
TASK_COUNT = 3

# Default base names + emojis
BASE_NAMES = ["GOATZ-RUDRA-RIS TMKC "]
EMOJIS = ["🌑", "🌘", "🌗", "🌖", "🌕", "🌔", "🌓", "🌒"]
# ===========================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Shared state
counter = count(1)
used_names = set()
success_count = 0
fail_count = 0
lock = asyncio.Lock()
recent_results = deque(maxlen=200)

# Globals controlled via bot
RUNNING_TASK: Optional[asyncio.Task] = None
STATS_TASK: Optional[asyncio.Task] = None
SESSION_ID: Optional[str] = None
DM_URLS: Optional[List[str]] = None
CURRENT_TASK_COUNT = TASK_COUNT
CURRENT_RENAME_DELAY = RENAME_DELAY
APP: Optional[Application] = None
START_TIME: Optional[float] = None

# ================ NAME GENERATOR ================
def gen_name() -> str:
    while True:
        base = random.choice(BASE_NAMES)
        emoji = random.choice(EMOJIS)
        suffix = next(counter)
        name = f"{base}{emoji}{suffix}"
        if name not in used_names:
            used_names.add(name)
            return name

# ================ HELPER: FIND BUTTONS ============
async def find_change_name_button(page: Page) -> Optional[Locator]:
    # Updated selectors based on common Instagram patterns (may need manual tweaks)
    selectors = [
        'div[aria-label="Change group name"][role="button"]',
        'button:has-text("Change group name")',
        'button:has-text("Edit name")',
        'text="Change group name"',
        'text="Edit name"',
        '[data-testid="change-group-name"]',
        'div[role="button"][aria-label*="Change"]',
        'div[role="button"]:has-text("Change")',
        # Fallback: Look for any button with "name" in aria-label
        'button[aria-label*="name"]',
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            await btn.wait_for(timeout=5000)  # Increased timeout
            return btn
        except Exception:
            continue
    return None

# ================ WORKER ========================
async def worker(context, worker_id: int, dm_url: str, duration: int):
    global success_count, fail_count, CURRENT_RENAME_DELAY
    page = await context.new_page()
    start_time = time.time()
    try:
        await page.goto(dm_url, wait_until="domcontentloaded", timeout=60000)
        # Add user-agent to avoid detection
        await page.set_extra_http_headers({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    except Exception as e:
        logging.error(f"[W{worker_id}] goto failed: {e}")
        await page.close()
        return

    # Try to open conversation info
    try:
        info_btn = page.locator('svg[aria-label="Conversation information"]').first
        await info_btn.click()
        await asyncio.sleep(2)  # Increased delay
    except Exception:
        pass

    # Find the "Change group name" button
    change_btn = await find_change_name_button(page)
    if not change_btn:
        logging.warning(f"[W{worker_id}] Could not find 'Change group name' button, skipping worker.")
        await page.close()
        return

    group_input = page.locator('input[aria-label="Group name"]').first
    save_btn = page.locator('div[role="button"]:has-text("Save")').first

    while time.time() - start_time < duration:
        try:
            name = gen_name()
            await change_btn.click()
            await group_input.click(click_count=3)
            await group_input.fill(name)

            # Check if save is disabled (e.g., rate limit)
            if await save_btn.get_attribute("aria-disabled") == "true":
                async with lock:
                    fail_count += 1
                    recent_results.append(False)
                await asyncio.sleep(CURRENT_RENAME_DELAY)
                continue

            await save_btn.click()
            async with lock:
                success_count += 1
                recent_results.append(True)
            await asyncio.sleep(CURRENT_RENAME_DELAY)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.warning(f"[W{worker_id}] failed: {e}")
            async with lock:
                fail_count += 1
                recent_results.append(False)
            await asyncio.sleep(0.01)

    try:
        await page.close()
    except:
        pass
    try:
        await context.close()
    except:
        pass
    logging.info(f"[W{worker_id}] finished cycle.")

# ================ CYCLE RUNNER ===================
async def run_cycle(p, browser, session_id: str, dm_urls: List[str], task_count: int):
    contexts = []
    tasks = []
    try:
        for dm_url in dm_urls:
            for i in range(task_count):
                ctx = await browser.new_context(
                    locale="en-US",
                    # Optional: Add proxy if needed, e.g., proxy={"server": "http://proxy:port"}
                )
                await ctx.add_cookies([{
                    "name": "sessionid",
                    "value": session_id,
                    "domain": ".instagram.com",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "None"
                }])
                contexts.append((ctx, dm_url, i + 1))

        for ctx, dm_url, wid in contexts:
            tasks.append(asyncio.create_task(worker(ctx, wid, dm_url, 999999999)))

        await asyncio.gather(*tasks, return_exceptions=True)  # Handle exceptions
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        for ctx, _, _ in contexts:
            try:
                await ctx.close()
            except:
                pass

async def start_main_loop(session_id: str, dm_urls: List[str], task_count: int):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        await run_cycle(p, browser, session_id, dm_urls, task_count)
        logging.info("Cycle complete.")

# ================ RESET =========================
def reset_counters():
    global success_count, fail_count, used_names, counter
    success_count = 0
    fail_count = 0
    used_names.clear()
    counter = count(1)
    logging.info("Counters reset.")

# ================ TELEGRAM =======================
def is_admin(update: Update) -> bool:
    return update.effective_user.id == ADMIN_ID

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_help(update, context)

async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global SESSION_ID
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /session <session_id>")
        return
    SESSION_ID = context.args[0].strip()
    await update.message.reply_text("✅ Session ID set.")

async def cmd_urls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DM_URLS
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /urls <url1,url2,...>")
        return
    DM_URLS = [u.strip() for u in " ".join(context.args).split(",") if u.strip()]
    await update.message.reply_text(f"✅ DM URLs set: {DM_URLS}")

async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CURRENT_TASK_COUNT
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /tasks <number>")
        return
    try:
        n = int(context.args[0])
        CURRENT_TASK_COUNT = max(1, min(n, 40))
        await update.message.reply_text(f"✅ Tasks set to {CURRENT_TASK_COUNT}")
    except ValueError:
        await update.message.reply_text("⚠️ Invalid number.")

async def cmd_speed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CURRENT_RENAME_DELAY
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /speed <ms>")
        return
    try:
        ms = float(context.args[0])
        CURRENT_RENAME_DELAY = max(0.0, ms / 1000.0)
        await update.message.reply_text(f"✅ Speed set to {ms:.1f} ms per rename")
    except ValueError:
        await update.message.reply_text("⚠️ Invalid number.")

async def cmd_basename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BASE_NAMES
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /basename <name1 | name2 | name3>")
        return
    new_bases = " ".join(context.args).split("|")
    new_bases = [b.strip() for b in new_bases if b.strip()]
    if not new_bases:
        await update.message.reply_text("⚠️ No valid base names.")
        return
    BASE_NAMES = new_bases
    await update.message.reply_text("✅ Base names set:\n- " + "\n- ".join(BASE_NAMES))

async def cmd_start_eonix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global RUNNING_TASK, STATS_TASK, APP, START_TIME
    if not is_admin(update):
        return
    if not SESSION_ID or not DM_URLS:
        await update.message.reply_text("⚠️ Set /session and /urls first.")
        return
    if RUNNING_TASK and not RUNNING_TASK.done():
        await update.message.reply_text("⚠️ Already running.")
        return
    START_TIME = time.time()
    loop = asyncio.get_event_loop()
    RUNNING_TASK = loop.create_task(start_main_loop(SESSION_ID, DM_URLS, CURRENT_TASK_COUNT))
    STATS_TASK = loop.create_task(auto_stats(update.effective_chat.id))
    await update.message.reply_text(
        f"🚀 Started with {CURRENT_TASK_COUNT} tasks, {CURRENT_RENAME_DELAY*1000:.1f} ms speed."
    )

async def cmd_stop_eonix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global RUNNING_TASK, STATS_TASK, START_TIME
    if not is_admin(update):
        return
    if RUNNING_TASK and not RUNNING_TASK.done():
        RUNNING_TASK.cancel()
        RUNNING_TASK = None
        await update.message.reply_text("🛑 Stopping...")
    else:
        await update.message.reply_text("⚠️ Not running.")
    if STATS_TASK and not STATS_TASK.done():
        STATS_TASK.cancel()
        STATS_TASK = None
    reset_counters()
    START_TIME = None
    await update.message.reply_text("✅ Stats cleared.")

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global START_TIME
    if not is_admin(update):
        return
    reset_counters()
    START_TIME = time.time()
    await update.message.reply_text("🔄 Counters and stats reset.")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await send_stats(update.effective_chat.id)

async def send_stats(chat_id: int):
    global success_count, fail_count, used_names, START_TIME
    async with lock:
        total = success_count + fail_count
        uptime = "Not running"
        if START_TIME:
            elapsed = int(time.time() - START_TIME)
            h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
            uptime = f"{h:02d}:{m:02d}:{s:02d}"
        msg = (
            f"📊 Stats:\n"
            f"⏱ Uptime: {uptime}\n"
            f"Total Attempts: {total}\n"
            f"✅ Success: {success_count}\n"
            f"❌ Failed: {fail_count}\n"
            f"🔄 Names Used: {len(used_names)}\n"
            f"⚡ Tasks: {CURRENT_TASK_COUNT}\n"
            f"🚀 Speed: {CURRENT_RENAME_DELAY*1000:.1f} ms\n"
            f"🏷 Base Names: {', '.join(BASE_NAMES)}"
        )
    try:
        await APP.bot.send_message(chat_id=chat_id, text=msg)
    except Exception as e:
        logging.warning(f"send_stats failed: {e}")

async def auto_stats(chat_id: int):
    while True:
        await asyncio.sleep(AUTO_STATS_INTERVAL)
        await send_stats(chat_id)

# --- Admin system ---
ADMINS = {ADMIN_ID}  # Start with the main admin

async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /addadmin <telegram_user_id>")
        return
    try:
        uid = int(context.args[0])
        ADMINS.add(uid)
        await update.message.reply_text(f"✅ Added admin: {uid}")
    except ValueError:
        await update.message.reply_text("Invalid user id")

async def cmd_deladmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /deladmin <telegram_user_id>")
        return
    try:
        uid = int(context.args[0])
        if uid in ADMINS and uid != ADMIN_ID:  # Prevent removing main admin
            ADMINS.remove(uid)
            await update.message.reply_text(f"✅ Removed admin: {uid}")
        else:
            await update.message.reply_text("Cannot remove main admin or invalid ID")
    except ValueError:
        await update.message.reply_text("Invalid user id")

async def cmd_listadmins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    msg = "👑 Current admins:\n" + "\n".join(str(uid) for uid in ADMINS)
    await update.message.reply_text(msg)

# --- Broadcast and Spam ---
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    msg = " ".join(context.args)
    await update.message.reply_text("📨 Broadcast feature not fully implemented (would send to groups)")

async def cmd_spam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /spam <count> <message>")
        return
    try:
        count = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid count")
        return
    msg = " ".join(context.args[1:])
    await update.message.reply_text(f"✅ Spam command would send '{msg}' {count} times")

# --- Emoji/Base Rotation ---
ROTATE_TASKS = {}

async def rotate_task(item_type: str, minutes: int):
    while True:
        await asyncio.sleep(minutes * 60)
        if item_type == "base":
            # rotate base names
            names = BASE_NAMES
            if names:
                names = names[1:] + names[:1]
                BASE_NAMES.clear()
                BASE_NAMES.extend(names)
                logging.info("Rotated base names -> %s", names)
        elif item_type == "emoji":
            emojis = EMOJIS
            if emojis:
                emojis = emojis[1:] + emojis[:1]
                EMOJIS.clear()
                EMOJIS.extend(emojis)
                logging.info("Rotated emojis -> %s", emojis)

async def cmd_rotatebase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /rotatebase <minutes>")
        return
    try:
        minutes = int(context.args[0])
        if "base" in ROTATE_TASKS:
