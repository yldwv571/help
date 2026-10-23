import asyncio
import logging
import os
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "BU_YERGA_TOKEN")
logging.basicConfig(level=logging.INFO)

router = Router()


def cmd(*names):
    """/ban va .ban ikkalasi ham ishlaydi"""
    return Command(*names, prefix="/.", ignore_case=True)


# Muter roli qaysi buyruqlarni ishlata oladi (faqat mute)
MUTER_CAN = {"mute"}

# ======================= BAZA =======================
db = sqlite3.connect("bot.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS settings(
    chat_id INTEGER PRIMARY KEY,
    antilink INTEGER DEFAULT 1,
    antiforward INTEGER DEFAULT 0,
    antiflood INTEGER DEFAULT 1,
    badwords INTEGER DEFAULT 1,
    delservice INTEGER DEFAULT 1,
    welcome_on INTEGER DEFAULT 1,
    welcome_text TEXT DEFAULT 'Salom, {name}! <b>{group}</b> guruhiga xush kelibsiz 👋',
    channel TEXT DEFAULT NULL,
    maxwarn INTEGER DEFAULT 3
);
CREATE TABLE IF NOT EXISTS warns(
    chat_id INTEGER, user_id INTEGER, count INTEGER DEFAULT 0,
    PRIMARY KEY(chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS muters(
    chat_id INTEGER, user_id INTEGER,
    PRIMARY KEY(chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS words(
    chat_id INTEGER, word TEXT,
    PRIMARY KEY(chat_id, word)
);
"""
)
db.commit()
try:
    db.execute("ALTER TABLE settings ADD COLUMN channel_link TEXT DEFAULT NULL")
    db.commit()
except sqlite3.OperationalError:
    pass

TOGGLES = {
    "antilink": "🔗 Havola filtri",
    "antiforward": "↪️ Forward filtri",
    "antiflood": "💬 Flood (spam) filtri",
    "badwords": "🤬 So'kinish filtri",
    "delservice": "🗑 Xizmat xabarlarini o'chirish",
    "welcome_on": "👋 Xush kelibsiz xabari",
}


def get_settings(chat_id: int) -> sqlite3.Row:
    row = db.execute("SELECT * FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    if not row:
        db.execute("INSERT INTO settings(chat_id) VALUES(?)", (chat_id,))
        db.commit()
        row = db.execute("SELECT * FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    return row


def set_setting(chat_id: int, key: str, value):
    allowed = set(TOGGLES) | {"welcome_text", "channel", "channel_link", "maxwarn"}
    if key not in allowed:
        return
    get_settings(chat_id)
    db.execute(f"UPDATE settings SET {key}=? WHERE chat_id=?", (value, chat_id))
    db.commit()


def get_warns(chat_id: int, user_id: int) -> int:
    r = db.execute(
        "SELECT count FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id)
    ).fetchone()
    return r["count"] if r else 0


def set_warns(chat_id: int, user_id: int, n: int):
    db.execute(
        "INSERT INTO warns(chat_id,user_id,count) VALUES(?,?,?) "
        "ON CONFLICT(chat_id,user_id) DO UPDATE SET count=excluded.count",
        (chat_id, user_id, n),
    )
    db.commit()


def get_words(chat_id: int) -> list[str]:
    return [r["word"] for r in db.execute("SELECT word FROM words WHERE chat_id=?", (chat_id,))]


# ======================= YORDAMCHI =======================
GROUP = {"group", "supergroup"}
URL_RE = re.compile(
    r"(https?://|www\.|t\.me/|telegram\.me/|\b[\w-]+\.(com|ru|uz|org|net|io|me|info|xyz|link)\b)",
    re.IGNORECASE,
)
flood_log: dict[tuple[int, int], list[float]] = defaultdict(list)


def mention(user) -> str:
    return f'<a href="tg://user?id={user.id}">{user.full_name}</a>'


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False


async def msg_from_admin(m: Message, bot: Bot) -> bool:
    # anonim admin yoki oddiy admin
    if m.sender_chat and m.sender_chat.id == m.chat.id:
        return True
    if not m.from_user:
        return False
    return await is_admin(bot, m.chat.id, m.from_user.id)


async def temp(bot: Bot, chat_id: int, text: str, sec: int = 10, **kw):
    try:
        msg = await bot.send_message(chat_id, text, **kw)
    except Exception:
        return
    await asyncio.sleep(sec)
    try:
        await msg.delete()
    except Exception:
        pass


def notify(bot: Bot, chat_id: int, text: str, sec: int = 10, **kw):
    asyncio.create_task(temp(bot, chat_id, text, sec, **kw))


def chan_ref(value: str):
    """-100123... bo'lsa int, @username bo'lsa str"""
    v = str(value)
    return int(v) if re.fullmatch(r"-?\d+", v) else v


def chan_link(s) -> str:
    if s["channel_link"]:
        return s["channel_link"]
    return f"https://t.me/{str(s['channel']).lstrip('@')}"


def is_muter(chat_id: int, user_id: int) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM muters WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        ).fetchone()
        is not None
    )


def parse_duration(text: str) -> int:
    """30 / 30m / 2h / 1d -> daqiqa"""
    mt = re.fullmatch(r"(\d+)([mhd]?)", text.lower())
    if not mt:
        return 0
    n, unit = int(mt.group(1)), mt.group(2)
    return n * {"": 1, "m": 1, "h": 60, "d": 1440}[unit]


async def need_admin(m: Message, bot: Bot, cmdname: str | None = None) -> bool:
    """Admin hamma narsani qila oladi. Muter faqat MUTER_CAN dagi buyruqlarni."""
    if m.chat.type not in GROUP:
        await m.answer("Bu buyruq faqat guruhda ishlaydi.")
        return False
    if await msg_from_admin(m, bot):
        return True
    if (
        cmdname in MUTER_CAN
        and m.from_user
        and is_muter(m.chat.id, m.from_user.id)
    ):
        return True
    notify(bot, m.chat.id, "⛔ Bu buyruq faqat adminlar uchun.", 5)
    return False


async def need_target(m: Message, bot: Bot):
    if not m.reply_to_message or not m.reply_to_message.from_user:
        notify(bot, m.chat.id, "↩️ Buyruqni foydalanuvchi xabariga <b>reply</b> qilib yozing.")
        return None
    t = m.reply_to_message.from_user
    if await is_admin(bot, m.chat.id, t.id):
        notify(bot, m.chat.id, "⛔ Adminga bu amalni qo'llab bo'lmaydi.")
        return None
    if is_muter(m.chat.id, t.id) and not await msg_from_admin(m, bot):
        notify(bot, m.chat.id, "⛔ Muterga bu amalni qo'llab bo'lmaydi.")
        return None
    return t


FULL = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
)
MUTED = ChatPermissions(can_send_messages=False)


def settings_kb(chat_id: int) -> InlineKeyboardMarkup:
    s = get_settings(chat_id)
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'✅' if s[k] else '❌'} {label}", callback_data=f"tg:{k}"
            )
        ]
        for k, label in TOGGLES.items()
    ]
    rows.append([InlineKeyboardButton(text="🔒 Yopish", callback_data="close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ======================= PRIVATE =======================
@router.message(CommandStart(), F.chat.type == "private")
async def start_private(m: Message, bot: Bot):
    me = await bot.get_me()
    url = (
        f"https://t.me/{me.username}?startgroup=true"
        "&admin=delete_messages+restrict_members+invite_users+pin_messages+promote_members"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="➕ Guruhga qo'shish", url=url)]]
    )
    await m.answer(
        "👋 Salom! Men <b>guruh boshqaruv botiman</b>.\n\n"
        "• Reklama/havola, flood, so'kinishlarni o'chiraman\n"
        "• Ban, mute, warn tizimi\n"
        "• Majburiy kanalga obuna\n"
        "• Xush kelibsiz xabari\n\n"
        "Meni guruhga qo'shib, <b>admin</b> qiling. Buyruqlar: /help",
        reply_markup=kb,
    )


@router.message(cmd("help"))
async def help_cmd(m: Message):
    await m.answer(
        "<b>📖 Buyruqlar</b> (<code>/</code> yoki <code>.</code> bilan yozsa bo'ladi)\n\n"
        "<b>Adminlar uchun:</b>\n"
        ".ban, .unban, .kick — (reply)\n"
        ".mute [30 / 30m / 2h / 1d], .unmute — (reply)\n"
        ".warn, .unwarn, .warns — (reply)\n"
        ".admin, .unadmin — adminlik berish/olish (reply)\n"
        ".muter, .unmuter, .muters — muter roli (faqat mute qila oladi)\n"
        ".del — xabarni o'chirish (reply)\n"
        ".settings — sozlamalar paneli\n"
        ".setwelcome matn — salomlashuv ({name}, {group})\n"
        ".setchannel @kanal / .delchannel — majburiy obuna\n"
        ".setwarn son — warn limiti\n"
        ".addword, .delword, .words — taqiqlangan so'zlar\n"
        ".id — ID ko'rish"
    )


@router.message(cmd("id"))
async def id_cmd(m: Message):
    t = m.reply_to_message.from_user if m.reply_to_message else m.from_user
    await m.reply(f"🆔 <code>{t.id}</code>\n💬 Chat: <code>{m.chat.id}</code>")


# ======================= SOZLAMALAR =======================
@router.message(cmd("settings"))
async def settings_cmd(m: Message, bot: Bot):
    if not await need_admin(m, bot):
        return
    await m.answer("⚙️ <b>Guruh sozlamalari</b>", reply_markup=settings_kb(m.chat.id))


@router.callback_query(F.data.startswith("tg:"))
async def toggle_cb(cb: CallbackQuery, bot: Bot):
    chat_id = cb.message.chat.id
    if not await is_admin(bot, chat_id, cb.from_user.id):
        return await cb.answer("Faqat adminlar uchun!", show_alert=True)
    key = cb.data.split(":", 1)[1]
    if key not in TOGGLES:
        return await cb.answer()
    s = get_settings(chat_id)
    set_setting(chat_id, key, 0 if s[key] else 1)
    await cb.message.edit_reply_markup(reply_markup=settings_kb(chat_id))
    await cb.answer("Saqlandi ✅")


@router.callback_query(F.data == "close")
async def close_cb(cb: CallbackQuery, bot: Bot):
    if await is_admin(bot, cb.message.chat.id, cb.from_user.id):
        await cb.message.delete()
    await cb.answer()


@router.message(cmd("setwelcome"))
async def setwelcome(m: Message, bot: Bot):
    if not await need_admin(m, bot):
        return
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await m.reply("Misol: <code>/setwelcome Salom {name}, {group} ga xush kelibsiz!</code>")
    set_setting(m.chat.id, "welcome_text", parts[1])
    await m.reply("✅ Xush kelibsiz matni saqlandi.")


@router.message(cmd("setchannel"))
async def setchannel(m: Message, bot: Bot):
    if not await need_admin(m, bot):
        return
    ch_id, link = None, None
    for p in (m.text or "").split()[1:]:
        if p.startswith("http") or "t.me/" in p:
            link = p
        elif p.startswith("@") or re.fullmatch(r"-?\d+", p):
            ch_id = p

    # kanaldan forward qilingan postga reply qilingan bo'lsa, ID ni o'zi oladi
    if m.reply_to_message and m.reply_to_message.forward_origin:
        fo = m.reply_to_message.forward_origin
        fchat = getattr(fo, "chat", None)
        if fchat and getattr(fo, "type", "") == "channel":
            ch_id = str(fchat.id)
            if fchat.username and not link:
                link = f"https://t.me/{fchat.username}"

    if not ch_id:
        return await m.reply(
            "ℹ️ <b>Yopiq (shaxsiy) kanal uchun:</b>\n"
            "1) Botni kanalga <b>admin</b> qiling\n"
            "2) Kanaldan istalgan postni shu guruhga forward qiling\n"
            "3) O'sha postga reply qilib yozing:\n"
            "<code>.setchannel https://t.me/+64utWJu1Ti1hNDFi</code>\n\n"
            "Yoki ID bilan: <code>.setchannel -1001234567890 https://t.me/+havola</code>\n"
            "Ochiq kanal uchun: <code>.setchannel @kanal</code>"
        )
    if ch_id.startswith("@") and not link:
        link = f"https://t.me/{ch_id.lstrip('@')}"
    if not link:
        return await m.reply("⚠️ Kanal havolasini ham yozing: <code>.setchannel -100... https://t.me/+...</code>")

    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chan_ref(ch_id), me.id)
        if member.status not in ("administrator", "creator"):
            return await m.reply("⚠️ Avval botni kanalga <b>admin</b> qiling.")
    except Exception:
        return await m.reply(
            "⚠️ Kanal topilmadi yoki bot u yerda yo'q. Botni kanalga admin qiling va ID to'g'ri ekanini tekshiring."
        )
    set_setting(m.chat.id, "channel", ch_id)
    set_setting(m.chat.id, "channel_link", link)
    await m.reply(f"✅ Majburiy obuna yoqildi.\nKanal: {link}")


@router.message(cmd("delchannel"))
async def delchannel(m: Message, bot: Bot):
    if not await need_admin(m, bot):
        return
    set_setting(m.chat.id, "channel", None)
    set_setting(m.chat.id, "channel_link", None)
    await m.reply("✅ Majburiy obuna o'chirildi.")


@router.message(cmd("setwarn"))
async def setwarn(m: Message, bot: Bot):
    if not await need_admin(m, bot):
        return
    parts = (m.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit() or not (1 <= int(parts[1]) <= 20):
        return await m.reply("Misol: <code>/setwarn 3</code> (1-20)")
    set_setting(m.chat.id, "maxwarn", int(parts[1]))
    await m.reply(f"✅ Warn limiti: {parts[1]}")


@router.message(cmd("addword"))
async def addword(m: Message, bot: Bot):
    if not await need_admin(m, bot):
        return
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await m.reply("Misol: <code>/addword so'z</code>")
    db.execute(
        "INSERT OR IGNORE INTO words(chat_id,word) VALUES(?,?)",
        (m.chat.id, parts[1].strip().lower()),
    )
    db.commit()
    await m.reply("✅ Qo'shildi.")


@router.message(cmd("delword"))
async def delword(m: Message, bot: Bot):
    if not await need_admin(m, bot):
        return
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await m.reply("Misol: <code>/delword so'z</code>")
    db.execute(
        "DELETE FROM words WHERE chat_id=? AND word=?", (m.chat.id, parts[1].strip().lower())
    )
    db.commit()
    await m.reply("✅ O'chirildi.")


@router.message(cmd("words"))
async def words_cmd(m: Message, bot: Bot):
    if not await need_admin(m, bot):
        return
    w = get_words(m.chat.id)
    await m.reply("🤬 Taqiqlangan so'zlar:\n" + (", ".join(w) if w else "— bo'sh —"))


# ======================= MODERATSIYA =======================
@router.message(cmd("ban"))
async def ban(m: Message, bot: Bot):
    if not await need_admin(m, bot):
        return
    t = await need_target(m, bot)
    if not t:
        return
    try:
        await bot.ban_chat_member(m.chat.id, t.id)
        await m.answer(f"🚫 {mention(t)} guruhdan <b>ban</b> qilindi.")
    except Exception as e:
        await m.reply(f"Xatolik: {e}")


@router.message(cmd("unban"))
async def unban(m: Message, bot: Bot):
    if not await need_admin(m, bot):
        return
    if not m.reply_to_message or not m.reply_to_message.from_user:
        return await m.reply("↩️ Reply qiling.")
    t = m.reply_to_message.from_user
    try:
        await bot.unban_chat_member(m.chat.id, t.id, only_if_banned=True)
        await m.answer(f"✅ {mention(t)} bandan chiqarildi.")
    except Exception as e:
        await m.reply(f"Xatolik: {e}")


@router.message(cmd("kick"))
async def kick(m: Message, bot: Bot):
    if not await need_admin(m, bot):
        return
    t = await need_target(m, bot)
    if not t:
        return
    try:
        await bot.ban_chat_member(m.chat.id, t.id)
        await bot.unban_chat_member(m.chat.id, t.id)
        await m.answer(f"👢 {mention(t)} guruhdan <b>chiqarib yuborildi</b>.")
    except Exception as e:
        await m.reply(f"Xatolik: {e}")


@router.message(cmd("mute"))
async def mute(m: Message, bot: Bot):
    if not await need_admin(m, bot, "mute"):
        return
    t = await need_target(m, bot)
    if not t:
        return
    parts = (m.text or "").split()
    minutes = parse_duration(parts[1]) if len(parts) > 1 else 0
    until = datetime.now() + timedelta(minutes=minutes) if minutes else None
    try:
        await bot.restrict_chat_member(m.chat.id, t.id, permissions=MUTED, until_date=until)
        dur = f"{minutes} daqiqaga" if minutes else "muddatsiz"
        await m.answer(f"🔇 {mention(t)} {dur} <b>mute</b> qilindi.")
    except Exception as e:
        await m.reply(f"Xatolik: {e}")


@router.message(cmd("unmute"))
async def unmute(m: Message, bot: Bot):
    if not await need_admin(m, bot, "unmute"):
        return
    if not m.reply_to_message or not m.reply_to_message.from_user:
        return await m.reply("↩️ Reply qiling.")
    t = m.reply_to_message.from_user
    try:
        await bot.restrict_chat_member(m.chat.id, t.id, permissions=FULL)
        await m.answer(f"🔊 {mention(t)} uchun mute olib tashlandi.")
    except Exception as e:
        await m.reply(f"Xatolik: {e}")


async def punish_warn(bot: Bot, chat_id: int, user, reason: str):
    s = get_settings(chat_id)
    n = get_warns(chat_id, user.id) + 1
    limit = s["maxwarn"]
    if n >= limit:
        set_warns(chat_id, user.id, 0)
        try:
            await bot.ban_chat_member(chat_id, user.id)
            await bot.send_message(
                chat_id, f"🚫 {mention(user)} {limit} ta ogohlantirish olgani uchun <b>ban</b> qilindi."
            )
        except Exception:
            pass
    else:
        set_warns(chat_id, user.id, n)
        await bot.send_message(
            chat_id, f"⚠️ {mention(user)} ogohlantirildi ({n}/{limit})\nSabab: {reason}"
        )


@router.message(cmd("warn"))
async def warn(m: Message, bot: Bot):
    if not await need_admin(m, bot):
        return
    t = await need_target(m, bot)
    if not t:
        return
    parts = (m.text or "").split(maxsplit=1)
    await punish_warn(bot, m.chat.id, t, parts[1] if len(parts) > 1 else "admin qarori")


@router.message(cmd("unwarn"))
async def unwarn(m: Message, bot: Bot):
    if not await need_admin(m, bot):
        return
    if not m.reply_to_message or not m.reply_to_message.from_user:
        return await m.reply("↩️ Reply qiling.")
    t = m.reply_to_message.from_user
    n = max(0, get_warns(m.chat.id, t.id) - 1)
    set_warns(m.chat.id, t.id, n)
    await m.answer(f"✅ {mention(t)} ogohlantirishlari: {n}")


@router.message(cmd("warns"))
async def warns_cmd(m: Message, bot: Bot):
    t = m.reply_to_message.from_user if m.reply_to_message else m.from_user
    s = get_settings(m.chat.id)
    await m.reply(f"⚠️ {mention(t)}: {get_warns(m.chat.id, t.id)}/{s['maxwarn']}")


@router.message(cmd("del"))
async def del_cmd(m: Message, bot: Bot):
    if not await need_admin(m, bot):
        return
    try:
        if m.reply_to_message:
            await m.reply_to_message.delete()
        await m.delete()
    except Exception:
        pass


# ======================= ADMIN / MUTER ROLLARI =======================
async def reply_target(m: Message):
    if not m.reply_to_message or not m.reply_to_message.from_user:
        await m.reply("↩️ Buyruqni foydalanuvchi xabariga <b>reply</b> qilib yozing.")
        return None
    return m.reply_to_message.from_user


@router.message(cmd("admin"))
async def make_admin(m: Message, bot: Bot):
    if not await need_admin(m, bot):
        return
    t = await reply_target(m)
    if not t:
        return
    try:
        await bot.promote_chat_member(
            m.chat.id,
            t.id,
            can_manage_chat=True,
            can_delete_messages=True,
            can_restrict_members=True,
            can_invite_users=True,
            can_pin_messages=True,
            can_change_info=False,
            can_promote_members=False,
        )
        db.execute("DELETE FROM muters WHERE chat_id=? AND user_id=?", (m.chat.id, t.id))
        db.commit()
        await m.answer(f"👮 {mention(t)} <b>admin</b> qilindi.")
    except Exception:
        await m.reply("⚠️ Xatolik. Botda <b>«Adminlarni qo'shish»</b> huquqi bo'lishi kerak.")


@router.message(cmd("unadmin"))
async def remove_admin(m: Message, bot: Bot):
    if not await need_admin(m, bot):
        return
    t = await reply_target(m)
    if not t:
        return
    try:
        await bot.promote_chat_member(
            m.chat.id,
            t.id,
            can_manage_chat=False,
            can_delete_messages=False,
            can_restrict_members=False,
            can_invite_users=False,
            can_pin_messages=False,
            can_change_info=False,
            can_promote_members=False,
            can_post_messages=False,
            can_edit_messages=False,
            can_manage_video_chats=False,
        )
        await m.answer(f"✅ {mention(t)} adminlikdan olindi.")
    except Exception:
        await m.reply(
            "⚠️ Adminlikdan olib bo'lmadi. Bot faqat o'zi tayinlagan adminlarni "
            "olib tashlay oladi (guruh egasini hech kim olib tashlay olmaydi)."
        )


@router.message(cmd("muter"))
async def make_muter(m: Message, bot: Bot):
    if not await need_admin(m, bot):
        return
    t = await reply_target(m)
    if not t:
        return
    if await is_admin(bot, m.chat.id, t.id):
        return await m.reply("ℹ️ Bu odam allaqachon admin.")
    db.execute(
        "INSERT OR IGNORE INTO muters(chat_id,user_id) VALUES(?,?)", (m.chat.id, t.id)
    )
    db.commit()
    await m.answer(f"🔇 {mention(t)} <b>muter</b> qilindi (faqat mute qila oladi).")


@router.message(cmd("unmuter"))
async def remove_muter(m: Message, bot: Bot):
    if not await need_admin(m, bot):
        return
    t = await reply_target(m)
    if not t:
        return
    db.execute("DELETE FROM muters WHERE chat_id=? AND user_id=?", (m.chat.id, t.id))
    db.commit()
    await m.answer(f"✅ {mention(t)} muterlikdan olindi.")


@router.message(cmd("muters"))
async def list_muters(m: Message, bot: Bot):
    if not await need_admin(m, bot):
        return
    rows = db.execute("SELECT user_id FROM muters WHERE chat_id=?", (m.chat.id,)).fetchall()
    if not rows:
        return await m.reply("Muterlar yo'q.")
    text = "🔇 <b>Muterlar:</b>\n" + "\n".join(
        f'• <a href="tg://user?id={r["user_id"]}">{r["user_id"]}</a>' for r in rows
    )
    await m.reply(text)


# ======================= XIZMAT XABARLARI / SALOM =======================
@router.message(F.new_chat_members, F.chat.type.in_(GROUP))
async def on_join(m: Message, bot: Bot):
    s = get_settings(m.chat.id)
    if s["delservice"]:
        try:
            await m.delete()
        except Exception:
            pass
    if s["welcome_on"]:
        for u in m.new_chat_members:
            if u.is_bot:
                continue
            text = s["welcome_text"].replace("{name}", mention(u)).replace(
                "{group}", m.chat.title or ""
            )
            notify(bot, m.chat.id, text, 60)


@router.message(F.left_chat_member, F.chat.type.in_(GROUP))
async def on_left(m: Message):
    if get_settings(m.chat.id)["delservice"]:
        try:
            await m.delete()
        except Exception:
            pass


# ======================= ASOSIY FILTR =======================
def has_link(m: Message) -> bool:
    for ent in (m.entities or []) + (m.caption_entities or []):
        if ent.type in ("url", "text_link"):
            return True
    txt = m.text or m.caption or ""
    return bool(URL_RE.search(txt))


@router.message(F.chat.type.in_(GROUP))
async def guard(m: Message, bot: Bot):
    if not m.from_user or m.from_user.is_bot:
        return
    if await msg_from_admin(m, bot):
        return

    chat_id = m.chat.id
    user = m.from_user
    s = get_settings(chat_id)

    # 1) Majburiy obuna
    if s["channel"]:
        try:
            cm = await bot.get_chat_member(chan_ref(s["channel"]), user.id)
            subscribed = cm.status not in ("left", "kicked")
            if cm.status == "restricted" and not getattr(cm, "is_member", True):
                subscribed = False
        except Exception:
            subscribed = True  # kanalni tekshirib bo'lmasa to'smaymiz
        if not subscribed:
            try:
                await m.delete()
            except Exception:
                pass
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="📢 Kanalga obuna bo'lish",
                            url=chan_link(s),
                        )
                    ]
                ]
            )
            notify(
                bot,
                chat_id,
                f"❗ {mention(user)}, guruhda yozish uchun avval kanalga obuna bo'ling (pastdagi tugma).",
                15,
                reply_markup=kb,
            )
            return

    # 2) Flood
    if s["antiflood"]:
        now = time.time()
        key = (chat_id, user.id)
        flood_log[key] = [t for t in flood_log[key] if now - t < 5] + [now]
        if len(flood_log[key]) >= 6:
            flood_log[key] = []
            try:
                await bot.restrict_chat_member(
                    chat_id,
                    user.id,
                    permissions=MUTED,
                    until_date=datetime.now() + timedelta(minutes=5),
                )
                notify(bot, chat_id, f"🔇 {mention(user)} flood uchun 5 daqiqaga mute qilindi.")
            except Exception:
                pass
            return

    # 3) Havola
    if s["antilink"] and has_link(m):
        try:
            await m.delete()
        except Exception:
            pass
        notify(bot, chat_id, f"🔗 {mention(user)}, guruhda havola yuborish taqiqlangan!")
        return

    # 4) Forward
    if s["antiforward"] and m.forward_origin:
        try:
            await m.delete()
        except Exception:
            pass
        notify(bot, chat_id, f"↪️ {mention(user)}, forward xabar yuborish taqiqlangan!")
        return

    # 5) So'kinish / taqiqlangan so'zlar
    if s["badwords"]:
        txt = (m.text or m.caption or "").lower()
        if txt and any(w in txt for w in get_words(chat_id)):
            try:
                await m.delete()
            except Exception:
                pass
            await punish_warn(bot, chat_id, user, "taqiqlangan so'z")


# ======================= ISHGA TUSHIRISH =======================
async def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())