import os
import re
import asyncio
import logging
import subprocess
import posixpath
import time
from concurrent.futures import ThreadPoolExecutor
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import database as db

BOT_TOKEN      = os.environ.get("BOT_TOKEN", "8500309733:AAF4z6vwme3CEPBhLdrznvuOltV3SAS34CI")
OWNER_ID       = int(os.environ.get("OWNER_ID", "5142642877"))
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "@HIT_Sir")

CMD_TIMEOUT  = 60
BATCH_SIZE   = 50
MAX_FILES    = 10000

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

user_sessions = {}

# 10 files ek saath rename hongi → ~8-10x speed improvement
WORKERS   = 10
_executor = ThreadPoolExecutor(max_workers=WORKERS)

def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID

async def check_auth(user_id: int) -> bool:
    return is_owner(user_id) or await db.is_authorised(user_id)

async def send_unauthorized(update: Update):
    user = update.effective_user
    await update.message.reply_text(
        f"🚫 *Access Denied!*\n\n"
        f"Aap is bot ko use karne ke liye authorized nahi hain.\n\n"
        f"👤 Aapka ID: `{user.id}`\n"
        f"📛 Naam: {user.first_name}\n\n"
        f"✅ Access ke liye owner se contact karein:\n"
        f"➡️ {OWNER_USERNAME}",
        parse_mode="Markdown"
    )

def run_cmd(args, timeout=CMD_TIMEOUT, extra_env=None):
    try:
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", f"Command timed out after {timeout}s", 1
    except Exception as e:
        return "", str(e), 1


QUOTA_ENV = {
    "MEGA_IGNORE_UPLOAD_QUOTA":    "1",
    "MEGA_FORCE_FULL_ACCOUNT_CACHE": "1"
}

def mega_login(email: str, password: str) -> dict:
    """Returns {"success": bool, "over_quota": bool, "error": str}"""

    # Attempt 1: quota-ignore env
    out, err, code = run_cmd(["mega-login", email, password], extra_env=QUOTA_ENV)
    already_in = "Already logged in" in out or "Already logged in" in err

    if code == 0 or already_in:
        q_out, _, _ = run_cmd(["mega-quota"], extra_env=QUOTA_ENV)
        over_quota = any(x in q_out.lower() for x in ["exceeded", "overquota", "over quota", "full"])
        return {"success": True, "over_quota": over_quota}

    # Attempt 2: --no-ask-for-confirmation
    out2, err2, code2 = run_cmd(
        ["mega-login", "--no-ask-for-confirmation", email, password],
        extra_env=QUOTA_ENV
    )
    already_in2 = "Already logged in" in out2 or "Already logged in" in err2

    if code2 == 0 or already_in2:
        return {"success": True, "over_quota": False}

    # Both failed
    combined = (err or out or err2 or out2).strip()
    is_quota_err = any(x in combined.lower() for x in ["quota", "overquota", "storage", "full"])
    return {"success": False, "over_quota": is_quota_err, "error": combined}


def mega_logout():
    run_cmd(["mega-logout", "--keep-session"])


def mega_get_all_files() -> list:
    out, err, code = run_cmd(["mega-find", "/", "--type=f"], timeout=CMD_TIMEOUT * 3)
    if code == 0:
        files = [line.strip() for line in out.strip().split('\n') if line.strip()]
        return files[:MAX_FILES]
    raise Exception(err or out)


def mega_rename_file(old_path: str, new_path: str):
    """Pure metadata op — works even on over-quota accounts."""
    out, err, code = run_cmd(["mega-mv", old_path, new_path])
    if code != 0:
        raise Exception(err or out)


def mega_check_link(link: str) -> dict:
    out, err, code = run_cmd(["mega-ls", link], timeout=CMD_TIMEOUT)
    if code != 0:
        raise Exception(err or out or "Invalid or expired link")
    info_out, _, _ = run_cmd(["mega-ls", "-l", link], timeout=CMD_TIMEOUT)
    lines = [l.strip() for l in info_out.strip().split('\n') if l.strip()]
    file_count = len([l for l in lines if not l.startswith('/')])
    return {
        "name":       lines[0] if lines else "Unknown",
        "file_count": file_count,
        "raw_output": info_out[:500] if info_out else "No details"
    }


def mega_account_info() -> dict:
    quota_out, _, _ = run_cmd(["mega-quota"], timeout=CMD_TIMEOUT)
    files_out, _, fcode = run_cmd(["mega-find", "/", "--type=f"], timeout=CMD_TIMEOUT * 2)
    dirs_out,  _, dcode = run_cmd(["mega-find", "/", "--type=d"], timeout=CMD_TIMEOUT * 2)
    file_list   = [l for l in files_out.strip().split('\n') if l.strip()] if fcode == 0 else []
    folder_list = [l for l in dirs_out.strip().split('\n')  if l.strip()] if dcode == 0 else []
    return {
        "quota_raw":    quota_out.strip(),
        "file_count":   len(file_list),
        "folder_count": len(folder_list),
    }

def build_new_name(old_name: str, pattern: str, replacement: str, index: int) -> str:
    """
    Patterns:
      prefix  → replacement + old_name
      suffix  → name + replacement + ext
      replace → old_name.replace(old|new)
      regex   → re.sub(pat|repl, old_name)
      number  → 00001.ext
      channel → @channelname (1).ext  ← NEW
    """
    name, ext = posixpath.splitext(old_name)

    if pattern == "prefix":
        return f"{replacement}{old_name}"
    elif pattern == "suffix":
        return f"{name}{replacement}{ext}"
    elif pattern == "replace":
        parts = replacement.split("|", 1)
        if len(parts) == 2:
            return old_name.replace(parts[0], parts[1])
    elif pattern == "regex":
        parts = replacement.split("|", 1)
        if len(parts) == 2:
            try:
                return re.sub(parts[0], parts[1], old_name)
            except re.error:
                pass
    elif pattern == "template":
        return replacement.replace("{n}", name).replace("{i}", str(index)).replace("{ext}", ext)
    elif pattern == "number":
        return f"{str(index).zfill(5)}{ext}"
    elif pattern == "channel":
        channel = replacement.strip()
        if not channel.startswith("@"):
            channel = f"@{channel}"
        return f"{channel} ({index}){ext}"

    return old_name

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await db.add_user(uid)

    if not await check_auth(uid):
        await send_unauthorized(update)
        return

    await update.message.reply_text(
        "🚀 *MEGA.NZ PRO RENAMER BOT*\n\n"
        "Advanced & Crash-Free Engine. Database Connected! 💾\n\n"
        "📌 *Commands:*\n"
        "  `/login email password` — Mega.nz login\n"
        "  `/logout` — Mega.nz logout\n"
        "  `/renameall` — Start Rename Process\n"
        "  `/renameall @channelname` — Channel style rename\n"
        "  `/check <link>` — Check MEGA link details\n"
        "  `/megainfo` — Account storage & file stats\n"
        "  `/stats` — Database Stats & Quota\n"
        "  `/premium` — Check premium status\n"
        "  `/lang` — Change language\n"
        "  `/help` — Help & support\n\n"
        "👑 *Owner Commands:*\n"
        "  `/auth user_id` — Authorize user\n"
        "  `/unauth user_id` — Remove authorization\n"
        "  `/authlist` — View authorized users\n"
        "  `/broadcast message` — Message all users",
        parse_mode="Markdown"
    )

async def login_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await db.add_user(uid)

    if not await check_auth(uid):
        await send_unauthorized(update)
        return

    if len(ctx.args) < 2:
        await update.message.reply_text("❌ Usage: `/login email pass`", parse_mode="Markdown")
        return

    email, password = ctx.args[0], ctx.args[1]
    msg = await update.message.reply_text("🔄 MegaCMD Engine se login ho raha hai...")

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(mega_login, email, password),
            timeout=CMD_TIMEOUT
        )

        if result["success"]:
            user_sessions[uid] = {"email": email}
            await db.save_session(uid, email)

            if result.get("over_quota"):
                await msg.edit_text(
                    f"⚠️ *Login Successful — Over-Quota Account*\n\n"
                    f"📧 `{email}`\n\n"
                    f"🔴 *Storage quota exceed ho chuki hai.*\n\n"
                    f"✅ *Rename kaam karega* — `mega-mv` sirf metadata change karta hai, "
                    f"koi upload nahi hoti, isliye quota affect nahi hota.\n"
                    f"❌ Naya upload tab tak nahi hoga jab tak storage free na ho.\n\n"
                    f"Ab `/renameall` use karein. ✨",
                    parse_mode="Markdown"
                )
            else:
                await msg.edit_text(
                    f"✅ *Login Successful!*\n📧 `{email}`\n\nAb `/renameall` use karein.",
                    parse_mode="Markdown"
                )
        else:
            err = result.get("error", "Unknown error")
            if result.get("over_quota"):
                await msg.edit_text(
                    f"❌ *Login Failed — Storage Over-Quota*\n\n"
                    f"📧 `{email}`\n"
                    f"⚠️ Error: `{err}`\n\n"
                    f"💡 *Kya karein:*\n"
                    f"1️⃣ MEGA website/app se kuch files delete karein\n"
                    f"2️⃣ Phir dobara `/login` try karein\n"
                    f"3️⃣ Ya MEGA premium plan lein\n\n"
                    f"📩 Help: {OWNER_USERNAME}",
                    parse_mode="Markdown"
                )
            else:
                await msg.edit_text(f"❌ *Login Failed!*\nError: `{err}`", parse_mode="Markdown")

    except asyncio.TimeoutError:
        await msg.edit_text("⏱️ Login timed out. Please try again.")
    except Exception as e:
        await msg.edit_text(f"❌ Error: `{e}`", parse_mode="Markdown")

async def logout_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if not await check_auth(uid):
        await send_unauthorized(update)
        return

    session = user_sessions.get(uid) or await db.get_session(uid)
    if not session:
        await update.message.reply_text("❌ Aap abhi logged in nahi hain.")
        return

    msg = await update.message.reply_text("🔄 Logout ho raha hai...")
    try:
        await asyncio.wait_for(asyncio.to_thread(mega_logout), timeout=CMD_TIMEOUT)
        await db.delete_session(uid)
        user_sessions.pop(uid, None)
        await msg.edit_text(
            f"✅ *Logout Successful!*\n\n"
            f"📧 `{session.get('email', 'N/A')}` se logout ho gaye.\n"
            f"Dobara login ke liye `/login` use karein.",
            parse_mode="Markdown"
        )
    except asyncio.TimeoutError:
        await msg.edit_text("⏱️ Logout timed out.")
    except Exception as e:
        await msg.edit_text(f"❌ Error: `{e}`", parse_mode="Markdown")

async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await check_auth(uid):
        await send_unauthorized(update)
        return

    user_data = await db.get_user(uid)
    if not user_data:
        await update.message.reply_text("Pehle /start type karke DB account banayein.")
        return

    session = await db.get_session(uid)
    email = session.get("email", "Not logged in") if session else "Not logged in"

    await update.message.reply_text(
        f"📊 *User Database Stats*\n\n"
        f"👤 UID: `{user_data['_id']}`\n"
        f"📧 Email: `{email}`\n"
        f"🔄 Lifetime Renamed: `{user_data['lifetime_renamed']}`\n"
        f"🔗 Links Checked: `{user_data.get('links_checked', 0)}`\n"
        f"⚡ Daily Limit Remaining: `{user_data['daily_limit']}`\n"
        f"👑 Premium: `{'Yes ✨' if user_data['is_premium'] else 'No'}`",
        parse_mode="Markdown"
    )

async def check_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await check_auth(uid):
        await send_unauthorized(update)
        return
    if not ctx.args:
        await update.message.reply_text("Usage: `/check <mega_link>`", parse_mode="Markdown")
        return

    msg = await update.message.reply_text("🔍 MEGA link check ho raha hai...")
    try:
        info = await asyncio.wait_for(
            asyncio.to_thread(mega_check_link, ctx.args[0]),
            timeout=CMD_TIMEOUT
        )
        await msg.edit_text(
            f"✅ *MEGA Link Details*\n\n"
            f"📄 *Contents:*\n`{info['name']}`\n\n"
            f"🗂️ *Items Found:* `{info['file_count']}`\n\n"
            f"📋 *Raw Info:*\n```\n{info['raw_output'][:400]}\n```",
            parse_mode="Markdown"
        )
        await db.increment_links_checked(uid)
    except asyncio.TimeoutError:
        await msg.edit_text("⏱️ Request timed out.")
    except Exception as e:
        await msg.edit_text(f"❌ Error: `{e}`", parse_mode="Markdown")

async def megainfo_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await check_auth(uid):
        await send_unauthorized(update)
        return
    session = user_sessions.get(uid) or await db.get_session(uid)
    if not session:
        await update.message.reply_text("❌ Pehle `/login` karein.", parse_mode="Markdown")
        return

    msg = await update.message.reply_text("📊 MEGA account info fetch ho rahi hai...")
    try:
        info = await asyncio.wait_for(
            asyncio.to_thread(mega_account_info),
            timeout=CMD_TIMEOUT * 3
        )
        await msg.edit_text(
            f"☁️ *MEGA Account Info*\n\n"
            f"📧 *Email:* `{session.get('email', 'N/A')}`\n\n"
            f"📄 *Total Files:*   `{info['file_count']:,}`\n"
            f"📁 *Total Folders:* `{info['folder_count']:,}`\n\n"
            f"💾 *Storage Quota:*\n```\n{info['quota_raw'][:400] or 'N/A'}\n```",
            parse_mode="Markdown"
        )
    except asyncio.TimeoutError:
        await msg.edit_text("⏱️ Request timed out.")
    except Exception as e:
        await msg.edit_text(f"❌ Error: `{e}`", parse_mode="Markdown")

async def renameall_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await check_auth(uid):
        await send_unauthorized(update)
        return
    session = user_sessions.get(uid) or await db.get_session(uid)
    if not session:
        await update.message.reply_text("❌ Pehle `/login` karein.", parse_mode="Markdown")
        return
    user_sessions[uid] = session

    # Direct channel rename: /renameall @channelname
    if ctx.args:
        arg = ctx.args[0].strip()
        if arg.startswith("@") or re.match(r'^[a-zA-Z0-9_]{3,}$', arg):
            channel = arg if arg.startswith("@") else f"@{arg}"
            ctx.user_data["rename_pattern"]     = "channel"
            ctx.user_data["rename_replacement"] = channel
            ctx.user_data["awaiting_input"]     = False

            example  = build_new_name("Example_File.mp4", "channel", channel, 1)
            example2 = build_new_name("Another.mkv",      "channel", channel, 2)
            keyboard = [[InlineKeyboardButton("✅ Start Renaming!", callback_data="confirm_rename")]]
            await update.message.reply_text(
                f"📢 *Channel Rename Mode*\n\n"
                f"Channel: `{channel}`\n\n"
                f"👁 *Preview:*\n"
                f"• `{example}`\n"
                f"• `{example2}`\n"
                f"• `{channel} (3).ext` ...\n\n"
                f"Confirm karein?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return

    # Normal pattern menu
    keyboard = [
        [InlineKeyboardButton("🔤 Add Prefix",          callback_data="pattern_prefix")],
        [InlineKeyboardButton("🔡 Add Suffix",          callback_data="pattern_suffix")],
        [InlineKeyboardButton("🔄 Replace Text",        callback_data="pattern_replace")],
        [InlineKeyboardButton("🔢 Sequential Number",   callback_data="pattern_number")],
        [InlineKeyboardButton("📢 Channel Name Rename", callback_data="pattern_channel")],
    ]
    await update.message.reply_text(
        "🎯 *Rename Pattern Select Karein:*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def premium_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await check_auth(uid):
        await send_unauthorized(update)
        return
    user_data = await db.get_user(uid)
    if not user_data:
        await update.message.reply_text("Pehle /start karein.")
        return
    is_prem = user_data.get("is_premium", False)
    await update.message.reply_text(
        f"⭐ *Premium Status*\n\n"
        f"{'✅ Aapke paas Premium access hai!' if is_prem else '❌ Aapke paas Premium nahi hai.'}\n\n"
        f"{'🎉 Unlimited daily renames enjoy karein!' if is_prem else f'Premium ke liye contact karein: {OWNER_USERNAME}'}",
        parse_mode="Markdown"
    )

async def lang_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await check_auth(uid):
        await send_unauthorized(update)
        return
    keyboard = [
        [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
         InlineKeyboardButton("🇮🇳 Hindi",   callback_data="lang_hi")],
        [InlineKeyboardButton("🇸🇦 Arabic",  callback_data="lang_ar"),
         InlineKeyboardButton("🇪🇸 Spanish", callback_data="lang_es")],
    ]
    await update.message.reply_text("🌐 Apni language choose karein:", reply_markup=InlineKeyboardMarkup(keyboard))

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await check_auth(uid):
        await send_unauthorized(update)
        return
    await update.message.reply_text(
        f"❓ *Help & Support*\n\n"
        f"1️⃣ `/login email password` — MEGA login\n"
        f"2️⃣ `/logout` — MEGA logout\n"
        f"3️⃣ `/renameall` — pattern menu\n"
        f"4️⃣ `/renameall @channelname` — channel style rename\n"
        f"5️⃣ `/check <link>` — MEGA link inspect\n"
        f"6️⃣ `/megainfo` — storage & file stats\n\n"
        f"💡 *Over-quota accounts mein bhi rename kaam karta hai!*\n"
        f"_(rename = metadata change, koi upload nahi)_\n\n"
        f"⚡ Max *{MAX_FILES:,}* files per session!\n\n"
        f"📩 Help: {OWNER_USERNAME}",
        parse_mode="Markdown"
    )

async def auth_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("❌ Sirf owner.")
        return
    if not ctx.args:
        await update.message.reply_text("Usage: `/auth user_id`", parse_mode="Markdown")
        return
    try:
        uid = int(ctx.args[0])
        await db.add_user(uid)
        await db.add_auth(uid)
        await update.message.reply_text(f"✅ User `{uid}` authorize kar diya!", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")

async def unauth_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("❌ Sirf owner.")
        return
    if not ctx.args:
        await update.message.reply_text("Usage: `/unauth user_id`", parse_mode="Markdown")
        return
    try:
        uid = int(ctx.args[0])
        await db.remove_auth(uid)
        await update.message.reply_text(f"✅ User `{uid}` unauthorize kar diya.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")

async def authlist_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("❌ Sirf owner.")
        return
    users = await db.get_auth_list()
    if not users:
        await update.message.reply_text("📋 Koi authorized user nahi hai.")
        return
    await update.message.reply_text(
        "📋 *Authorized Users:*\n\n" + "\n".join(f"• `{u}`" for u in users),
        parse_mode="Markdown"
    )

async def broadcast_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("❌ Sirf owner.")
        return
    if not ctx.args:
        await update.message.reply_text("Usage: `/broadcast message`", parse_mode="Markdown")
        return
    msg_text  = " ".join(ctx.args)
    all_users = await db.get_all_users()
    status    = await update.message.reply_text(f"📢 Broadcasting to {len(all_users)} users...")
    sent = failed = 0
    for u in all_users:
        try:
            await ctx.bot.send_message(u, f"📢 *Broadcast:*\n\n{msg_text}", parse_mode="Markdown")
            sent += 1
        except Exception:
            failed += 1
    await status.edit_text(f"✅ Done! Sent: {sent} | Failed: {failed}")

async def setpremium_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("❌ Sirf owner.")
        return
    if not ctx.args:
        await update.message.reply_text("Usage: `/setpremium user_id`", parse_mode="Markdown")
        return
    try:
        uid = int(ctx.args[0])
        await db.set_premium(uid, True)
        await db.reset_daily_limit(uid, 99999)
        await update.message.reply_text(f"⭐ User `{uid}` ko Premium de diya!", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")

async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid  = query.from_user.id

    if not await check_auth(uid):
        await query.edit_message_text(f"🚫 Access Denied! Owner se contact karein: {OWNER_USERNAME}")
        return

    if data.startswith("pattern_"):
        pattern = data.replace("pattern_", "")
        ctx.user_data["rename_pattern"] = pattern

        if pattern == "number":
            ctx.user_data["rename_replacement"] = ""
            keyboard = [[InlineKeyboardButton("✅ Start", callback_data="confirm_rename")]]
            await query.edit_message_text(
                "🔢 Sabhi files `00001.ext` pattern me rename hongi.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        elif pattern == "channel":
            ctx.user_data["awaiting_input"] = True
            await query.edit_message_text(
                "📢 *Channel Rename Mode*\n\n"
                "Channel username bhejein:\n"
                "_(e.g. `@mychannel` ya sirf `mychannel`)_\n\n"
                "Files rename hongi: `@mychannel (1).ext`, `@mychannel (2).ext` ...",
                parse_mode="Markdown"
            )
        else:
            hint = {
                "prefix":  "prefix text (har file se pehle lagega)",
                "suffix":  "suffix text (har file ke baad lagega)",
                "replace": "format: `purana_text|naya_text`",
            }.get(pattern, "text")
            await query.edit_message_text(f"✏️ Ab apna {hint} type karke bhejein:")
            ctx.user_data["awaiting_input"] = True

    elif data == "confirm_rename":
        await query.edit_message_text("🚀 Rename Processing Started...")
        asyncio.create_task(do_bulk_rename(query.message, uid, ctx))

    elif data.startswith("lang_"):
        await db.set_language(uid, data.split("_")[1])
        await query.edit_message_text(f"✅ Language set kar di!", parse_mode="Markdown")

async def message_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await check_auth(uid):
        await send_unauthorized(update)
        return
    if not ctx.user_data.get("awaiting_input"):
        return

    text    = update.message.text.strip()
    pattern = ctx.user_data.get("rename_pattern", "")

    if pattern == "channel":
        if not text.startswith("@"):
            text = f"@{text}"

    ctx.user_data["rename_replacement"] = text
    ctx.user_data["awaiting_input"]     = False

    example  = build_new_name("Example_File.mp4", pattern, text, 1)
    example2 = build_new_name("Another_Video.mkv", pattern, text, 2)
    keyboard = [[InlineKeyboardButton("✅ Start Renaming!", callback_data="confirm_rename")]]

    preview = (
        f"👁 *Preview:*\n\n"
        f"📄 `{example}`\n"
        f"📄 `{example2}`\n\n"
        f"Confirm?"
    )
    await update.message.reply_text(preview, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

def _rename_one_sync(file_path: str, pattern: str, replacement: str, idx: int):
    """
    Runs in thread pool. Returns True=renamed, False=skipped, raises on error.
    mega-mv is purely metadata — safe even on over-quota accounts.
    """
    old_name = posixpath.basename(file_path)
    new_name = build_new_name(old_name, pattern, replacement, idx)
    if new_name == old_name:
        return False   # nothing to rename
    parent   = posixpath.dirname(file_path)
    new_path = f"{parent}/{new_name}" if parent not in ["", "/"] else f"/{new_name}"
    mega_rename_file(file_path, new_path)
    return True


async def do_bulk_rename(message, uid: int, ctx: ContextTypes.DEFAULT_TYPE):
    pattern     = ctx.user_data.get("rename_pattern", "prefix")
    replacement = ctx.user_data.get("rename_replacement", "")

    user_data = await db.get_user(uid)
    if not user_data:
        await message.reply_text("❌ Pehle /start karein.")
        return

    if user_data['daily_limit'] <= 0 and not user_data.get('is_premium'):
        await message.reply_text(
            f"❌ Aapki daily limit khatam ho chuki hai.\n"
            f"Premium ke liye contact karein: {OWNER_USERNAME}"
        )
        return

    try:
        # ── 1. Fetch file list ────────────────────────────────────────
        fetch_msg = await message.reply_text("⏳ Files fetch ho rahi hain...")
        try:
            files = await asyncio.wait_for(
                asyncio.to_thread(mega_get_all_files),
                timeout=CMD_TIMEOUT * 3
            )
        except asyncio.TimeoutError:
            await fetch_msg.edit_text("⏱️ Files fetch timed out.")
            return

        total = len(files)
        if total == 0:
            await fetch_msg.edit_text("📂 Koi file nahi mili.")
            return

        max_allowed = user_data['daily_limit'] if not user_data.get('is_premium') else MAX_FILES
        files = files[:max_allowed]
        total = len(files)

        # ── 2. Initial status message ─────────────────────────────────
        status_msg = await fetch_msg.edit_text(
            f"🔄 *Renaming...*\n\n"
            f"`░░░░░░░░░░░░░░░░░░░░` 0%\n\n"
            f"📊 Total: `{total:,}`\n"
            f"✅ Done: `0`\n"
            f"❌ Failed: `0`\n"
            f"⚡ Speed: `—`\n"
            f"⏱ ETA: `—`",
            parse_mode="Markdown"
        )

        # ── 3. Shared counters (updated from coroutines) ──────────────
        done    = 0
        failed  = 0
        loop    = asyncio.get_event_loop()
        sem     = asyncio.Semaphore(WORKERS)   # max 10 parallel renames
        start_t = time.time()

        # ── 4. Single-file async wrapper ──────────────────────────────
        async def rename_one(fp: str, idx: int):
            nonlocal done, failed
            async with sem:
                try:
                    result = await asyncio.wait_for(
                        loop.run_in_executor(_executor, _rename_one_sync, fp, pattern, replacement, idx),
                        timeout=CMD_TIMEOUT
                    )
                    if result:
                        done += 1
                    # result=False → skipped (same name), count neither
                except Exception as e:
                    failed += 1
                    logger.warning(f"Rename failed [{idx}] {fp}: {e}")

        # ── 5. Background progress ticker (edits same message every 5s) ─
        async def progress_ticker():
            while True:
                await asyncio.sleep(5)
                elapsed  = max(time.time() - start_t, 0.1)
                processed = done + failed
                speed    = processed / elapsed
                pct      = int(processed / total * 100) if total else 0
                filled   = pct // 5
                bar      = "█" * filled + "░" * (20 - filled)
                eta      = int((total - processed) / speed) if speed > 0 and processed < total else 0
                eta_str  = f"⏱ ETA: `{eta}s`" if eta > 0 else "⏱ ETA: `calculating...`"
                try:
                    await status_msg.edit_text(
                        f"🔄 *Renaming...*\n\n"
                        f"`{bar}` {pct}%\n\n"
                        f"📊 Total: `{total:,}`\n"
                        f"✅ Done: `{done:,}`\n"
                        f"❌ Failed: `{failed:,}`\n"
                        f"⚡ Speed: `{speed:.1f}` files/sec\n"
                        f"{eta_str}",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass   # "message not modified" — ignore

        # ── 6. Launch all rename tasks + ticker together ──────────────
        ticker   = asyncio.create_task(progress_ticker())
        all_jobs = [rename_one(fp, idx + 1) for idx, fp in enumerate(files)]
        await asyncio.gather(*all_jobs)
        ticker.cancel()

        # ── 7. Final summary (edit same status message) ───────────────
        await db.update_rename_stats(uid, done)
        elapsed  = max(time.time() - start_t, 0.1)
        avg_spd  = (done + failed) / elapsed
        skipped  = total - done - failed

        await status_msg.edit_text(
            f"🎉 *Rename Complete!*\n\n"
            f"`{'█' * 20}` 100%\n\n"
            f"📊 Total:    `{total:,}`\n"
            f"✅ Renamed:  `{done:,}`\n"
            f"❌ Failed:   `{failed:,}`\n"
            f"⏭️ Skipped:  `{skipped:,}`\n\n"
            f"⚡ Avg Speed: `{avg_spd:.1f}` files/sec\n"
            f"🕐 Time: `{int(elapsed)}s`\n\n"
            f"💾 Database updated! `/stats` check karein.",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"do_bulk_rename uid={uid}: {e}")
        await message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")
        
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.getenv("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

if __name__ == "__main__":
    threading.Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("login",      login_cmd))
    app.add_handler(CommandHandler("logout",     logout_cmd))
    app.add_handler(CommandHandler("stats",      stats_cmd))
    app.add_handler(CommandHandler("renameall",  renameall_cmd))
    app.add_handler(CommandHandler("check",      check_cmd))
    app.add_handler(CommandHandler("megainfo",   megainfo_cmd))
    app.add_handler(CommandHandler("premium",    premium_cmd))
    app.add_handler(CommandHandler("lang",       lang_cmd))
    app.add_handler(CommandHandler("help",       help_cmd))
    app.add_handler(CommandHandler("auth",       auth_cmd))
    app.add_handler(CommandHandler("unauth",     unauth_cmd))
    app.add_handler(CommandHandler("authlist",   authlist_cmd))
    app.add_handler(CommandHandler("broadcast",  broadcast_cmd))
    app.add_handler(CommandHandler("setpremium", setpremium_cmd))

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    print("🤖 Pro Mega Bot Backend is Running with Health Checks...")
    app.run_polling(drop_pending_updates=True)
