#!/usr/bin/env python3
"""
🎮 Verifure Game 10.1 — Telegram Gaming Bot
Currency: VRF · 7 Games · Marriages · Bears · Admin Panel
Games: Duel · Cubes · Basketball · Football · Bowling · Darts · Slot
Deploy: Railway.app | Set BOT_TOKEN env var
Admin ID: 6254951831
"""

import asyncio
import logging
import math
import os
import random
import uuid
from datetime import datetime, timedelta
from functools import wraps
from typing import Optional, Tuple

import aiosqlite
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    ReactionTypeEmoji,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

# ══════════════════════════════════════════════════════
#                       CONFIG
# ══════════════════════════════════════════════════════

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
DB_PATH:   str = os.getenv("DB_PATH", "verifure.db")

# ── VRF Economy ───────────────────────────────────────
STARTING_VRF        = 500
DAILY_BONUS_BASE    = 100
DAILY_STREAK_BONUS  = 10    # extra VRF per streak day (max 7)
DAILY_MARRIED_BONUS = 15    # extra VRF when married
GIFT_COST           = 75
GIFT_REWARD         = 100
GIFT_MARRIED_REWARD = 150
GIFT_COOLDOWN_H     = 1
LOVE_REWARD         = 15
LOVE_MARRIED_REWARD = 35
LOVE_COOLDOWN_M     = 30
MAX_BET             = 500
MIN_BET             = 10

# ── XP / Levels ──────────────────────────────────────
XP_PER_MSG_MIN  = 2
XP_PER_MSG_MAX  = 8
XP_MSG_COOLDOWN = 60        # seconds between XP gains from messages
XP_PER_WIN      = 50
XP_PER_GAME     = 20

# ── Game defaults ─────────────────────────────────────
DEFAULT_ROUNDS  = 3
MAX_ROUNDS      = 10
JOIN_TIMEOUT    = 120       # seconds to accept an invite

# ── Admin IDs from env (plus hardcoded) ───────────────
ADMIN_IDS: list[int] = [6254951831] + [
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
]

# ══════════════════════════════════════════════════════
#              CUSTOM EMOJI (Bot API 6.0+)
# ══════════════════════════════════════════════════════

E_ACCEPT = '<tg-emoji emoji-id="5206607081334906820">⭐️</tg-emoji>'
E_DECLINE= '<tg-emoji emoji-id="5210952531676504517">⭐️</tg-emoji>'
E_STARS  = 'E_BEAR'
E_WIN1   = 'E_WIN1'
E_WIN2   = 'E_WIN2'
E_RING   = '<tg-emoji emoji-id="5337080053119336309">⭐️</tg-emoji>'
E_LOVE   = '<tg-emoji emoji-id="5253742260054409879">⭐️</tg-emoji>'
E_ALERT  = '<tg-emoji emoji-id="5458603043203327669">⭐️</tg-emoji>'

# ── Semantic aliases & new premium emoji ──────────────
E_BEAR   = 'E_BEAR'   # Bear collectible
E_WARN   = 'E_WARN'   # Warning / alert
E_BOOM   = 'E_BOOM'   # Mine explosion
E_VRF    = 'E_VRF'   # VRF coin
E_WAIT   = 'E_WAIT'   # Waiting player
E_FIRST  = 'E_FIRST'   # 1st place
E_SECOND = 'E_SECOND'   # 2nd place
E_BONUS  = 'E_BONUS'   # Bonus / daily

# ══════════════════════════════════════════════════════
#             IN-MEMORY GAME STATE
# ══════════════════════════════════════════════════════

duel_challenges: dict = {}   # key: f"{cid}:{c_id}:{o_id}"
cubes_games: dict     = {}   # key: game_id (str)
sports_games: dict    = {}   # key: game_id (str)
slot_games: dict      = {}   # key: game_id (str)
mines_games: dict     = {}   # key: f"{uid}:{cid}"

# ══════════════════════════════════════════════════════
#               LEVEL / RANK SYSTEM
# ══════════════════════════════════════════════════════

def xp_for_level(n: int) -> int:
    return 0 if n <= 1 else 50 * n * (n - 1)

def get_level(xp: int) -> int:
    if xp <= 0:
        return 1
    n = int((1 + math.sqrt(1 + 8 * xp / 50)) / 2)
    return max(1, min(n, 100))

def get_progress(xp: int) -> Tuple[int, int, int, float]:
    lvl  = get_level(xp)
    curr = xp_for_level(lvl)
    nxt  = xp_for_level(lvl + 1) if lvl < 100 else curr + 1
    pct  = (xp - curr) / max(1, nxt - curr)
    return lvl, curr, nxt, min(pct, 1.0)

def xp_bar(xp: int, length: int = 12) -> str:
    _, _, _, pct = get_progress(xp)
    filled = round(pct * length)
    return "█" * filled + "░" * (length - filled)

RANKS = [
    (1,  "🌱 Новичок"),  (5,  "📖 Ученик"),   (10, "⚡ Игрок"),
    (15, "🌟 Про"),      (20, "💎 Знаток"),    (25, "🔥 Ветеран"),
    (30, "👑 Авторитет"),(40, "🏆 Легенда"),   (50, "🌙 Мастер"),
    (75, "🚀 Сенсей"),   (100,"⚜️ Бог игры"),
]
MILESTONES = {10, 20, 30, 50, 75, 100}

def get_rank(level: int) -> str:
    result = RANKS[0][1]
    for lvl, name in RANKS:
        if level >= lvl:
            result = name
    return result

# ══════════════════════════════════════════════════════
#               SLOT MACHINE COMBOS
# ══════════════════════════════════════════════════════

def parse_slot(value: int) -> Tuple[str, int]:
    """Map Telegram 🎰 dice value (1-64) to combo name and multiplier."""
    if value <= 22:  return ("🎰 BAR",         2)
    if value <= 38:  return ("🍋 Лимон",        3)
    if value <= 50:  return ("🍒 Вишня",        5)
    if value <= 57:  return ("7️⃣ Семёрка",     10)
    if value <= 62:  return ("💎 Бриллиант",   20)
    return                  ("⭐ ДЖЕКПОТ",     100)

# ══════════════════════════════════════════════════════
#               SPORTS GAME MAPS
# ══════════════════════════════════════════════════════

# Game type → (emoji, dice_emoji, display_name, score_func)
SPORT_EMOJI = {
    "basket":   "🏀",
    "football": "⚽",
    "bowling":  "🎳",
    "darts":    "🎯",
}
SPORT_NAME = {
    "basket":   "Баскетбол",
    "football": "Футбол",
    "bowling":  "Боулинг",
    "darts":    "Дартс",
}
BOWLING_PINS = {1: 0, 2: 3, 3: 5, 4: 6, 5: 8, 6: 10}
DARTS_SCORES = {1: 1, 2: 2, 3: 5, 4: 10, 5: 25, 6: 50}

def score_throw(game_type: str, value: int) -> Tuple[int, str]:
    """Returns (points, label) for a single throw."""
    if game_type == "basket":
        scored = value in (4, 5)
        return (2 if scored else 0), ("🏀 Гол! +2" if scored else "❌ Мимо")
    if game_type == "football":
        scored = value in (3, 4, 5)
        return (1 if scored else 0), ("⚽ Гол! +1" if scored else "❌ Мимо")
    if game_type == "bowling":
        pts = BOWLING_PINS.get(value, 0)
        label = f"🎳 {'Страйк! ' if pts == 10 else ''}+{pts} кегл."
        return pts, label
    if game_type == "darts":
        pts = DARTS_SCORES.get(value, 1)
        label = f"🎯 {'Булл! ' if pts == 50 else ''}+{pts} очк."
        return pts, label
    return value, str(value)

# ══════════════════════════════════════════════════════
#                     LOGGING
# ══════════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("verifure")



# ══════════════════════════════════════════════════════
#         RICH MESSAGE HELPER  📄
# ══════════════════════════════════════════════════════

async def send_rich(
    bot,
    chat_id: int,
    markdown: str,
    fallback_html: str = "",
    reply_to_id: int = None,
    reply_markup=None,
) -> bool:
    """
    Send a Rich Message (Bot API sendRichMessage).
    Falls back to HTML send_message if the endpoint is unavailable.
    """
    content = {"html": html} if html else {"markdown": markdown}
    fb      = fallback_html or (html or markdown)[:4000]
    kw: dict = {"chat_id": chat_id, "rich_message": content}
    if reply_to_id:
        kw["reply_parameters"] = {"message_id": reply_to_id}
    if reply_markup and hasattr(reply_markup, "to_dict"):
        kw["reply_markup"] = reply_markup.to_dict()
    try:
        await bot.do_api_request("sendRichMessage", api_kwargs=kw)
        return True
    except Exception:
        try:
            await bot.send_message(
                chat_id, fb,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=reply_to_id,
                reply_markup=reply_markup,
            )
        except Exception:
            pass
        return False

# ══════════════════════════════════════════════════════
#                    DATABASE
# ══════════════════════════════════════════════════════

async def db_init() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id      INTEGER,
                chat_id      INTEGER,
                username     TEXT    DEFAULT '',
                first_name   TEXT    DEFAULT '',
                vrf          INTEGER DEFAULT 500,
                experience   INTEGER DEFAULT 0,
                level        INTEGER DEFAULT 1,
                wins         INTEGER DEFAULT 0,
                losses       INTEGER DEFAULT 0,
                draws        INTEGER DEFAULT 0,
                total_games  INTEGER DEFAULT 0,
                win_streak   INTEGER DEFAULT 0,
                max_streak   INTEGER DEFAULT 0,
                bears        INTEGER DEFAULT 0,
                last_xp      TEXT    DEFAULT NULL,
                last_daily   TEXT    DEFAULT NULL,
                daily_streak INTEGER DEFAULT 0,
                last_gift    TEXT    DEFAULT NULL,
                last_love    TEXT    DEFAULT NULL,
                PRIMARY KEY (user_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS marriages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user1_id   INTEGER NOT NULL,
                user2_id   INTEGER NOT NULL,
                chat_id    INTEGER NOT NULL,
                married_at TEXT    NOT NULL,
                UNIQUE (user1_id, chat_id),
                UNIQUE (user2_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS proposals (
                proposer_id INTEGER NOT NULL,
                target_id   INTEGER NOT NULL,
                chat_id     INTEGER NOT NULL,
                created_at  TEXT    NOT NULL,
                PRIMARY KEY (proposer_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS admins (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT    DEFAULT '',
                first_name TEXT    DEFAULT '',
                added_by   INTEGER,
                added_at   TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS shop_boosts (
                user_id    INTEGER NOT NULL,
                chat_id    INTEGER NOT NULL,
                boost_type TEXT    NOT NULL,
                expires_at TEXT    NOT NULL,
                PRIMARY KEY (user_id, chat_id, boost_type)
            );
        """)
        await db.commit()
    log.info("Database initialised at %s", DB_PATH)


# ── Users ──────────────────────────────────────────────

async def db_ensure_user(uid: int, cid: int, username: str, first_name: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO users (user_id, chat_id, username, first_name, vrf)
               VALUES (?,?,?,?,?)
               ON CONFLICT(user_id, chat_id) DO UPDATE SET
                 username=excluded.username, first_name=excluded.first_name""",
            (uid, cid, username or "", first_name or "", STARTING_VRF),
        )
        await db.commit()


async def db_get_user(uid: int, cid: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE user_id=? AND chat_id=?", (uid, cid)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def db_add_vrf(uid: int, cid: int, amount: int) -> int:
    """Add VRF. Returns new balance."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET vrf=vrf+? WHERE user_id=? AND chat_id=?",
            (amount, uid, cid),
        )
        await db.commit()
        async with db.execute(
            "SELECT vrf FROM users WHERE user_id=? AND chat_id=?", (uid, cid)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def db_set_vrf(uid: int, cid: int, amount: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET vrf=? WHERE user_id=? AND chat_id=?",
            (max(0, amount), uid, cid),
        )
        await db.commit()
    return max(0, amount)


async def db_deduct_vrf(uid: int, cid: int, amount: int) -> bool:
    """Deduct VRF only if user has enough. Returns success."""
    u = await db_get_user(uid, cid)
    if not u or u["vrf"] < amount:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET vrf=vrf-? WHERE user_id=? AND chat_id=?",
            (amount, uid, cid),
        )
        await db.commit()
    return True


async def db_add_xp(uid: int, cid: int, amount: int) -> Tuple[int, bool]:
    """Add XP. Returns (new_level, leveled_up)."""
    u = await db_get_user(uid, cid)
    if not u:
        return 1, False
    old_lvl = get_level(u["experience"])
    new_xp   = u["experience"] + amount
    new_lvl  = get_level(new_xp)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET experience=?, level=?, last_xp=? WHERE user_id=? AND chat_id=?",
            (new_xp, new_lvl, _now(), uid, cid),
        )
        await db.commit()
    return new_lvl, new_lvl > old_lvl


async def db_record_game(
    uid: int, cid: int, won: bool, draw: bool = False,
    streak_reset: bool = True
) -> None:
    """Update win/loss/streak counters."""
    u = await db_get_user(uid, cid)
    if not u:
        return
    streak = u["win_streak"]
    max_s  = u["max_streak"]
    if won:
        streak += 1
        max_s   = max(max_s, streak)
    elif not draw and streak_reset:
        streak = 0

    async with aiosqlite.connect(DB_PATH) as db:
        if won:
            await db.execute(
                """UPDATE users SET wins=wins+1, total_games=total_games+1,
                   win_streak=?, max_streak=? WHERE user_id=? AND chat_id=?""",
                (streak, max_s, uid, cid),
            )
        elif draw:
            await db.execute(
                "UPDATE users SET draws=draws+1, total_games=total_games+1 WHERE user_id=? AND chat_id=?",
                (uid, cid),
            )
        else:
            await db.execute(
                """UPDATE users SET losses=losses+1, total_games=total_games+1,
                   win_streak=0 WHERE user_id=? AND chat_id=?""",
                (uid, cid),
            )
        await db.commit()

    # Bears milestone: every 10th win
    u2 = await db_get_user(uid, cid)
    if u2 and won and u2["wins"] % 10 == 0:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET bears=bears+1 WHERE user_id=? AND chat_id=?",
                (uid, cid),
            )
            await db.commit()


async def db_can_earn_xp(uid: int, cid: int) -> bool:
    u = await db_get_user(uid, cid)
    if not u or not u["last_xp"]:
        return True
    return (datetime.now() - datetime.fromisoformat(u["last_xp"])).total_seconds() >= XP_MSG_COOLDOWN


# ── Leaderboard ────────────────────────────────────────

async def db_top(cid: int, sort: str = "vrf", limit: int = 10) -> list:
    col = {"vrf": "vrf", "level": "experience", "wins": "wins"}.get(sort, "vrf")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM users WHERE chat_id=? ORDER BY {col} DESC LIMIT ?",
            (cid, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def db_rank_pos(uid: int, cid: int, col: str = "vrf") -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"""SELECT COUNT(*)+1 FROM users
                WHERE chat_id=? AND {col}>(SELECT {col} FROM users WHERE user_id=? AND chat_id=?)""",
            (cid, uid, cid),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 1


async def db_count_users(cid: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE chat_id=?", (cid,)) as cur:
            return (await cur.fetchone())[0]


async def db_find_user_by_username(username: str, cid: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE LOWER(username)=? AND chat_id=?",
            (username.lower().lstrip("@"), cid),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


# ── Marriages ──────────────────────────────────────────

async def db_get_marriage(uid: int, cid: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM marriages WHERE (user1_id=? OR user2_id=?) AND chat_id=?",
            (uid, uid, cid),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def db_get_proposal_to(target_id: int, cid: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM proposals WHERE target_id=? AND chat_id=?", (target_id, cid)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def db_create_marriage(uid1: int, uid2: int, cid: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO marriages (user1_id,user2_id,chat_id,married_at) VALUES(?,?,?,?)",
            (uid1, uid2, cid, _now()),
        )
        await db.execute(
            "DELETE FROM proposals WHERE chat_id=? AND (proposer_id IN(?,?) OR target_id IN(?,?))",
            (cid, uid1, uid2, uid1, uid2),
        )
        await db.commit()


async def db_delete_marriage(mid: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM marriages WHERE id=?", (mid,))
        await db.commit()


async def db_all_marriages(cid: int) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM marriages WHERE chat_id=? ORDER BY married_at DESC", (cid,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ── Admins ─────────────────────────────────────────────

async def db_add_admin(uid: int, username: str, first_name: str, added_by: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO admins(user_id,username,first_name,added_by,added_at) VALUES(?,?,?,?,?)",
            (uid, username or "", first_name or "", added_by, _now()),
        )
        await db.commit()


async def db_remove_admin(uid: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM admins WHERE user_id=?", (uid,))
        await db.commit()
        return cur.rowcount > 0


async def db_list_admins() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM admins ORDER BY added_at") as cur:
            return [dict(r) for r in await cur.fetchall()]


async def is_bot_admin(uid: int) -> bool:
    if uid in ADMIN_IDS:
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)) as cur:
            return bool(await cur.fetchone())


async def is_group_or_bot_admin(update: Update) -> bool:
    uid = update.effective_user.id
    if await is_bot_admin(uid):
        return True
    try:
        member = await update.effective_chat.get_member(uid)
        return member.status in ("administrator", "creator")
    except TelegramError:
        return False


# ── Shop boosts ────────────────────────────────────────

async def db_has_boost(uid: int, cid: int, boost_type: str) -> bool:
    """Return True if boost is currently active."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT expires_at FROM shop_boosts WHERE user_id=? AND chat_id=? AND boost_type=?",
            (uid, cid, boost_type),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return False
    if datetime.fromisoformat(row[0]) > datetime.now():
        return True
    # Expired — clean up
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM shop_boosts WHERE user_id=? AND chat_id=? AND boost_type=?",
            (uid, cid, boost_type),
        )
        await db.commit()
    return False


async def db_set_boost(uid: int, cid: int, boost_type: str, hours: int = 24) -> None:
    expires = (datetime.now() + timedelta(hours=hours)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO shop_boosts(user_id,chat_id,boost_type,expires_at)
               VALUES(?,?,?,?)""",
            (uid, cid, boost_type, expires),
        )
        await db.commit()


async def db_clear_boost(uid: int, cid: int, boost_type: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM shop_boosts WHERE user_id=? AND chat_id=? AND boost_type=?",
            (uid, cid, boost_type),
        )
        await db.commit()


# ══════════════════════════════════════════════════════
#                    HELPERS
# ══════════════════════════════════════════════════════

def _now() -> str:
    return datetime.now().isoformat()

def mention(uid: int, name: str) -> str:
    safe = str(name).replace("<", "&lt;").replace(">", "&gt;").replace("&", "&amp;")
    return f'<a href="tg://user?id={uid}">{safe}</a>'

def fmt(n: int) -> str:
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 10_000:    return f"{n/1_000:.1f}K"
    return f"{n:,}".replace(",", " ")

def fmt_cd(seconds: int) -> str:
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    if h: return f"{h}ч {m}м"
    if m: return f"{m}м {s}с"
    return f"{s}с"

def days_ago(dt_str: str) -> int:
    return (datetime.now() - datetime.fromisoformat(dt_str)).days

def partner_id(m: dict, uid: int) -> int:
    return m["user2_id"] if m["user1_id"] == uid else m["user1_id"]

def calc_bet(vrf: int, other_vrf: int) -> int:
    """Auto bet: 10% of lowest balance, clamped."""
    return max(MIN_BET, min(MAX_BET, min(vrf, other_vrf) // 10))

MEDALS = [E_FIRST, E_SECOND, "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

def only_groups(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type == "private":
            await update.message.reply_text("❌ Эта команда работает только в групповых чатах.")
            return
        return await func(update, context)
    return wrapper

async def _react(update: Update, emoji: str = "🎉") -> None:
    try:
        await update.message.react([ReactionTypeEmoji(emoji=emoji)])
    except TelegramError:
        pass

async def _resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE, cid: int):
    if update.message.reply_to_message:
        t = update.message.reply_to_message.from_user
        if not t.is_bot:
            return t, None
    if context.args:
        uname = context.args[0].lstrip("@")
        row   = await db_find_user_by_username(uname, cid)
        if row:
            class _FakeUser:
                id = row["user_id"]; first_name = row["first_name"]
                username = row["username"]; is_bot = False
            return _FakeUser(), None
        return None, f"❌ @{uname} не найден в чате."
    return None, "❌ Укажи пользователя: ответь на его сообщение или /команда @username"


# ══════════════════════════════════════════════════════
#                BASE COMMANDS
# ══════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u   = update.effective_user
    cid = update.effective_chat.id

    if update.effective_chat.type == "private":
        md = (
            "# 👋 Привет! Я Verifure Game 10.1\n\n"
            "🎮 Игровой бот с внутренней валютой **VRF**!\n\n"
            "## 🎯 8 игр на VRF\n"
            "⚔️ Дуэль · 🎲 Кубики · 🏀 Баскетбол\n"
            "⚽ Футбол · 🎳 Боулинг · 🎯 Дартс · 🎰 Слот · 💣 Мины\n\n"
            f"💎 Стартовый баланс: **{STARTING_VRF} VRF**\n\n"
            "📌 Добавь меня в группу и напиши /start"
        )
        html = (
            "👋 <b>Привет! Я Verifure Game 10.1</b>\n\n"
            f"💎 Стартовый баланс: <b>{STARTING_VRF} VRF</b>\n\n"
            "⚔️ Дуэль · 🎲 Кубики · 🏀 Баскетбол · ⚽ Футбол\n"
            "🎳 Боулинг · 🎯 Дартс · 🎰 Слот · 💣 Мины\n\n"
            "📌 Добавь меня в группу и напиши /start"
        )
        await send_rich(context.bot, cid, md, fallback_html=html,
                        reply_to_id=update.message.message_id)
        return

    await db_ensure_user(u.id, cid, u.username or "", u.first_name)
    uu = await db_get_user(u.id, cid)
    bal = uu["vrf"] if uu else STARTING_VRF

    md = (
        f"# 👋 Привет, {u.first_name}!\n\n"
        f"💎 На твоём счёте **{fmt(bal)} VRF** для игр!\n\n"
        "## 🎮 Быстрый старт\n"
        "- /duel — ⚔️ вызвать на дуэль *(ответом)*\n"
        "- /cubes — 🎲 кости *(ответом)*\n"
        "- /slot — 🎰 слот PvP *(ответом)*\n"
        "- /mines — 💣 мины *(соло)*\n"
        "- /daily — ⚡ ежедневный бонус\n"
        "- /help — 📖 все команды\n\n"
        "> ⌨️ Клавиатура со всеми командами уже активна внизу экрана!"
    )
    html = (
        f"👋 Привет, {mention(u.id, u.first_name)}!\n\n"
        f"💎 Баланс: <b>{fmt(bal)} VRF</b>\n\n"
        "⚔️ /duel · 🎲 /cubes · 🎰 /slot · 💣 /mines\n"
        "⚡ /daily · 📖 /help\n\n"
        "⌨️ Клавиатура команд активирована!"
    )
    await send_rich(context.bot, cid, md, fallback_html=html,
                    reply_to_id=update.message.message_id,
)




async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cid = update.effective_chat.id
    md = (
        "# 📖 Verifure Game 10.1 — Помощь\n\n"
        "## 👤 Профиль & Активность\n"
        "- /profile — профиль и баланс VRF\n"
        "- /top — топ игроков *(VRF / Уровень / Победы)*\n"
        "- /stats — статистика чата\n"
        "- /daily — ежедневный бонус ⚡\n"
        "- /bonus — статус всех кулдаунов\n\n"
        "## 🎮 Игры *(ответом на сообщение соперника)*\n"
        "- /duel — ⚔️ Дуэль на VRF\n"
        "- /cubes `[раунды] [ставка]` — 🎲 Кубики\n"
        "- /basket — 🏀 Баскетбол (3 раунда)\n"
        "- /football — ⚽ Футбол (3 раунда)\n"
        "- /bowling — 🎳 Боулинг (3 раунда)\n"
        "- /darts — 🎯 Дартс (3 раунда)\n"
        "- /slot — 🎰 Слот-машина PvP\n"
        "- /mines — 💣 Мины *(соло, с кнопками)*\n""- /shop — 🛍️ Магазин *(VRF & Telegram Stars)*\n\n"
        "## 💒 Браки\n"
        "- /marry — предложение руки и сердца\n"
        "- /accept · /reject — ответ на предложение\n"
        "- /divorce — развод\n"
        "- /marriage — карточка вашего брака\n"
        "- /marriages — все пары чата\n\n"
        "## 🎁 Активности\n"
        "- /gift — подарить VRF *(ответом, стоит 75 VRF)*\n"
        "- /love — послать любовь ❤️ *(ответом, +VRF обоим)*\n\n"
        "## 🛡️ Администраторы\n"
        "- /admin — панель управления\n"
        "- /givevrf `<n>` — выдать VRF *(ответом)*\n"
        "- /takevrf `<n>` — забрать VRF *(ответом)*\n"
        "- /givebear — выдать 🐻 *(ответом)*\n"
        "- /addadmin · /removeadmin · /listadmins\n\n"
        "---\n\n"
        "> **Механика**\n"
        f"> - Начальный баланс: **{STARTING_VRF} VRF**\n"
        f"> - Ежедневный бонус: **{DAILY_BONUS_BASE} VRF** + стрик (до +60)\n"
        f"> - 💍 Брак даёт **+{DAILY_MARRIED_BONUS} VRF** к ежедневному\n"
        f"> - Подарок: **{GIFT_COST} VRF** → **{GIFT_REWARD} VRF** ({GIFT_MARRIED_REWARD} партнёру)\n"
        "> - 🐻 Медведь за каждые **10 побед**!"
    )
    html = (
        "📖 <b>Verifure Game 10.1 — Помощь</b>\n\n"
        "<b>👤 Профиль:</b> /profile /top /stats /daily /bonus\n"
        "<b>🎮 Игры:</b> /duel /cubes /basket /football /bowling /darts /slot /mines\n"
        "<b>💒 Браки:</b> /marry /accept /reject /divorce /marriage /marriages\n"
        "<b>🎁 Активности:</b> /gift /love\n"
        "<b>🛡️ Админ:</b> /admin /givevrf /takevrf /givebear /addadmin\n\n"
        f"💎 Старт: <b>{STARTING_VRF} VRF</b> · Бонус: <b>{DAILY_BONUS_BASE} VRF/день</b> · 🐻 за 10 побед!"
    )
    await send_rich(context.bot, cid, md, fallback_html=html,
                    reply_to_id=update.message.message_id,
)


# ══════════════════════════════════════════════════════
#           PROFILE & LEADERBOARD
# ══════════════════════════════════════════════════════

@only_groups
async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message.reply_to_message and not update.message.reply_to_message.from_user.is_bot:
        target = update.message.reply_to_message.from_user
    else:
        target = update.effective_user

    cid = update.effective_chat.id
    await db_ensure_user(target.id, cid, target.username or "", target.first_name)
    u = await db_get_user(target.id, cid)
    if not u:
        return

    lvl, _, _, pct = get_progress(u["experience"])
    bar     = xp_bar(u["experience"])
    rank_nm = get_rank(lvl)
    pos     = await db_rank_pos(target.id, cid)
    wr      = round(u["wins"] / max(1, u["total_games"]) * 100, 1)

    m = await db_get_marriage(target.id, cid)
    if m:
        pid   = partner_id(m, target.id)
        pu    = await db_get_user(pid, cid)
        pname = pu["first_name"] if pu else "Партнёр"
        d     = days_ago(m["married_at"])
        m_line = f"💍 {mention(pid, pname)} · {d} дн."
    else:
        m_line = "💔 Свободен(а)"

    uname = f"@{u['username']}" if u["username"] else u["first_name"]
    bars  = xp_bar(u["experience"], 14)

    # ── Rich markdown card ────────────────────────────
    md = (
        f"# 👤 {uname}\n\n"
        f"| Параметр | Значение |\n"
        f"|:---------|:---------|\n"
        f"| 🏅 Уровень | **{lvl}** — {rank_nm} |\n"
        f"| 📊 Прогресс | `{bars}` {int(pct*100)}% |\n"
        f"| {E_VRF} VRF | **{fmt(u['vrf'])}** |\n"
        f"| 🏆 Место | **#{pos}** |\n"
        f"| 🎮 Всего игр | **{u['total_games']}** |\n"
        f"| ✅ Побед | **{u['wins']}** ({wr}%) |\n"
        f"| ❌ Поражений | **{u['losses']}** |\n"
        f"| 🔥 Серия | **{u['win_streak']}** (макс. {u['max_streak']}) |\n"
        f"| 🐻 Медведей | **{u['bears']}** |\n\n"
        f"---\n\n"
        f"{m_line}"
    )
    html = (
        f"👤 <b>{mention(target.id, u['first_name'])}</b>\n\n"
        f"🏅 Ур. <b>{lvl}</b> — {rank_nm}  📊 [{bars}] {int(pct*100)}%\n"
        f"💎 VRF: <b>{fmt(u['vrf'])}</b>  🏆 <b>#{pos}</b>\n\n"
        f"🎮 Игр: <b>{u['total_games']}</b>  ✅ <b>{u['wins']}</b> ({wr}%)  ❌ <b>{u['losses']}</b>\n"
        f"🔥 Серия: <b>{u['win_streak']}</b> (макс. {u['max_streak']})  🐻 <b>{u['bears']}</b>\n\n"
        f"{m_line}"
    )
    await send_rich(context.bot, update.effective_chat.id, md, fallback_html=html,
                    reply_to_id=update.message.message_id)


@only_groups
async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cid = update.effective_chat.id
    await _show_top(update, context, cid, "vrf")


async def _show_top(update_or_query, context, cid: int, sort: str, edit: bool = False) -> None:
    users  = await db_top(cid, sort, 10)
    titles = {"vrf": "💎 VRF", "level": "⭐ Уровень", "wins": "🏆 Победы"}
    title  = titles.get(sort, "VRF")

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💎 VRF",     callback_data=f"top:vrf:{cid}"),
        InlineKeyboardButton("⭐ Уровень", callback_data=f"top:level:{cid}"),
        InlineKeyboardButton("🏆 Победы", callback_data=f"top:wins:{cid}"),
    ]])

    col_hdr = {"vrf": "VRF", "level": "Уровень / XP", "wins": "Побед"}.get(sort, "VRF")

    md_rows = [f"# 🏆 Топ-10 — {title}\n"]
    md_rows.append(f"| # | Игрок | {col_hdr} |")
    md_rows.append("|:--|:------|-------:|")
    html_rows = [f"🏆 <b>Топ-10 — {title}</b>\n"]

    for i, u in enumerate(users):
        lvl   = get_level(u["experience"])
        medal = MEDALS[i] if i < len(MEDALS) else f"{i+1}."
        name  = u["first_name"]
        uid   = u["user_id"]
        if sort == "wins":
            val_md   = f"{u['wins']}"
            val_html = f"{u['wins']} побед"
        elif sort == "level":
            val_md   = f"Ур.{lvl}"
            val_html = f"Ур.<b>{lvl}</b>"
        else:
            val_md   = f"{fmt(u['vrf'])} VRF"
            val_html = f"{fmt(u['vrf'])} VRF"
        md_rows.append(f"| {medal} | {name} | {val_md} |")
        html_rows.append(f"{medal} {mention(uid, name)} — {val_html}")

    md   = "\n".join(md_rows)
    html = "\n".join(html_rows)

    if edit:
        # callbacks can't use sendRichMessage easily, use HTML
        await update_or_query.edit_message_text(html, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await send_rich(context.bot, cid, md, fallback_html=html,
                        reply_to_id=update_or_query.message.message_id, reply_markup=kb)


@only_groups
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cid   = update.effective_chat.id
    total = await db_count_users(cid)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM marriages WHERE chat_id=?", (cid,)) as cur:
            marriages = (await cur.fetchone())[0]
        async with db.execute("SELECT SUM(total_games) FROM users WHERE chat_id=?", (cid,)) as cur:
            total_games = (await cur.fetchone())[0] or 0
        async with db.execute("SELECT SUM(vrf) FROM users WHERE chat_id=?", (cid,)) as cur:
            total_vrf = (await cur.fetchone())[0] or 0

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE chat_id=? ORDER BY vrf DESC LIMIT 1", (cid,)
        ) as cur:
            richest = await cur.fetchone()
            richest = dict(richest) if richest else None

    rich_line = ""
    if richest:
        rich_line = (
            f"\n\n💰 <b>Богатейший:</b>\n"
            f"{mention(richest['user_id'], richest['first_name'])} — {fmt(richest['vrf'])} VRF"
        )

    chat_title = update.effective_chat.title or "Чат"
    rich_name  = richest["first_name"] if richest else "—"
    rich_vrf   = fmt(richest["vrf"]) if richest else "—"

    md = (
        f"# 📊 Статистика чата\n"
        f"💬 **{chat_title}**\n\n"
        f"| Параметр | Значение |\n"
        f"|:---------|:---------|\n"
        f"| 👥 Игроков | **{total}** |\n"
        f"| 🎮 Сыграно игр | **{fmt(total_games)}** |\n"
        f"| 💎 VRF в обороте | **{fmt(total_vrf)}** |\n"
        f"| 💒 Браков | **{marriages}** |\n"
        f"| 👑 Богатейший | **{rich_name}** — {rich_vrf} VRF |\n"
    )
    html = (
        f"📊 <b>Статистика чата — {chat_title}</b>\n\n"
        f"👥 Игроков: <b>{total}</b>\n"
        f"🎮 Сыграно: <b>{fmt(total_games)}</b>\n"
        f"💎 VRF в обороте: <b>{fmt(total_vrf)}</b>\n"
        f"💒 Браков: <b>{marriages}</b>"
        f"{rich_line}"
    )
    await send_rich(context.bot, update.effective_chat.id, md, fallback_html=html,
                    reply_to_id=update.message.message_id)


# ══════════════════════════════════════════════════════
#          DAILY / GIFT / LOVE / BONUS
# ══════════════════════════════════════════════════════

@only_groups
async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u_obj = update.effective_user
    cid   = update.effective_chat.id
    await db_ensure_user(u_obj.id, cid, u_obj.username or "", u_obj.first_name)
    u     = await db_get_user(u_obj.id, cid)
    now   = datetime.now()
    cd    = 20 * 3600

    if u["last_daily"]:
        elapsed = (now - datetime.fromisoformat(u["last_daily"])).total_seconds()
        if elapsed < cd:
            rem = int(cd - elapsed)
            await update.message.reply_text(
                f"⏰ Следующий бонус через <b>{fmt_cd(rem)}</b>",
                parse_mode=ParseMode.HTML,
            )
            return

    streak = u.get("daily_streak") or 0
    last_streak = u.get("last_daily")
    if last_streak:
        diff   = (now.date() - datetime.fromisoformat(last_streak).date()).days
        streak = streak + 1 if diff == 1 else 1
    else:
        streak = 1

    streak_bonus = min(streak - 1, 6) * DAILY_STREAK_BONUS
    m = await db_get_marriage(u_obj.id, cid)
    marry_bonus  = DAILY_MARRIED_BONUS if m else 0
    total        = DAILY_BONUS_BASE + streak_bonus + marry_bonus

    # Shop double-bonus
    has_boost = await db_has_boost(u_obj.id, cid, "daily_boost")
    if has_boost:
        total *= 2
        await db_clear_boost(u_obj.id, cid, "daily_boost")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET last_daily=?, daily_streak=? WHERE user_id=? AND chat_id=?",
            (_now(), streak, u_obj.id, cid),
        )
        await db.commit()

    new_bal = await db_add_vrf(u_obj.id, cid, total)
    new_lvl, leveled_up = await db_add_xp(u_obj.id, cid, XP_PER_GAME)

    streak_bar = "🔥" * min(streak, 7) + "⬜" * (7 - min(streak, 7))

    md_parts = [
        f"# {E_BONUS} Ежедневный бонус\n\n"
        f"| | |\n|:--|--:|\n"
        f"| 💎 База | **+{DAILY_BONUS_BASE} VRF** |\n"
    ]
    if streak_bonus:
        md_parts.append(f"| 🔥 Стрик {streak} дн. | **+{streak_bonus} VRF** |\n")
    if marry_bonus:
        md_parts.append(f"| 💍 Бонус брака | **+{marry_bonus} VRF** |\n")
    md_parts.append(f"| **Итого** | **+{total} VRF** |\n\n")
    md_parts.append(f"💰 Баланс: **{fmt(new_bal)} VRF**\n")
    md_parts.append(f"📅 Стрик: {streak_bar} {streak}/7 дн.\n")
    if leveled_up:
        md_parts.append(f"\n🎉 **Новый уровень: {new_lvl}!** {get_rank(new_lvl)}")

    html_parts = [f"⚡ <b>Ежедневный бонус!</b>\n\n├ База: +{DAILY_BONUS_BASE} VRF"]
    if streak_bonus:
        html_parts.append(f"\n├ 🔥 Стрик {streak} дн.: +{streak_bonus} VRF")
    if marry_bonus:
        html_parts.append(f"\n├ 💍 Бонус брака: +{marry_bonus} VRF")
    html_parts.append(f"\n└ Итого: <b>+{total} VRF</b>\n\n💎 Баланс: <b>{fmt(new_bal)} VRF</b>")
    if leveled_up:
        html_parts.append(f"\n🎉 Новый уровень: <b>{new_lvl}!</b> {get_rank(new_lvl)}")

    await send_rich(context.bot, update.effective_chat.id,
                    "".join(md_parts), fallback_html="".join(html_parts),
                    reply_to_id=update.message.message_id)


@only_groups
async def cmd_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u_obj = update.effective_user
    cid   = update.effective_chat.id
    await db_ensure_user(u_obj.id, cid, u_obj.username or "", u_obj.first_name)
    u = await db_get_user(u_obj.id, cid)

    daily_txt = "✅ Доступен"
    if u["last_daily"]:
        elapsed = (datetime.now() - datetime.fromisoformat(u["last_daily"])).total_seconds()
        rem = int(20 * 3600 - elapsed)
        if rem > 0:
            daily_txt = f"⏰ {fmt_cd(rem)}"

    def cd_txt(last_field: str, secs: int) -> str:
        last = u.get(last_field)
        if not last:
            return "✅ Доступен"
        rem = int(secs - (datetime.now() - datetime.fromisoformat(last)).total_seconds())
        return f"⏰ {fmt_cd(rem)}" if rem > 0 else "✅ Доступен"

    m = await db_get_marriage(u_obj.id, cid)

    await update.message.reply_text(
        f"🎁 <b>Бонусы: {mention(u_obj.id, u_obj.first_name)}</b>\n\n"
        f"💎 VRF: <b>{fmt(u['vrf'])}</b>\n\n"
        f"📅 Ежедневный: {daily_txt}\n"
        f"🔥 Стрик: {u.get('daily_streak', 0)} дн.\n"
        f"💑 Брак: {'✅ +15 VRF к бонусу' if m else '❌ Нет'}\n"
        f"🎀 Подарок /gift: {cd_txt('last_gift', GIFT_COOLDOWN_H * 3600)}\n"
        f"💕 Любовь /love: {cd_txt('last_love', LOVE_COOLDOWN_M * 60)}\n\n"
        f"{E_BEAR} Медведей: <b>{u['bears']}</b>\n"
        f"🏆 Побед: <b>{u['wins']}</b> · 🎮 Игр: <b>{u['total_games']}</b>",
        parse_mode=ParseMode.HTML,
    )


@only_groups
async def cmd_gift(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sender = update.effective_user
    cid    = update.effective_chat.id

    if not update.message.reply_to_message or update.message.reply_to_message.from_user.is_bot:
        await update.message.reply_text("❌ Ответь на сообщение получателя!")
        return

    target = update.message.reply_to_message.from_user
    if target.id == sender.id:
        await update.message.reply_text("🎁 Нельзя дарить себе!")
        return

    await db_ensure_user(sender.id, cid, sender.username or "", sender.first_name)
    await db_ensure_user(target.id, cid, target.username or "", target.first_name)

    su = await db_get_user(sender.id, cid)
    if su["vrf"] < GIFT_COST:
        await update.message.reply_text(
            f"❌ Нужно {GIFT_COST} VRF · Есть: {su['vrf']} VRF"
        )
        return

    last_gift = su.get("last_gift")
    if last_gift:
        elapsed = (datetime.now() - datetime.fromisoformat(last_gift)).total_seconds()
        if elapsed < GIFT_COOLDOWN_H * 3600:
            rem = int(GIFT_COOLDOWN_H * 3600 - elapsed)
            await update.message.reply_text(f"⏰ Следующий подарок через {fmt_cd(rem)}")
            return

    m       = await db_get_marriage(sender.id, cid)
    reward  = GIFT_MARRIED_REWARD if (m and partner_id(m, sender.id) == target.id) else GIFT_REWARD

    if not await db_deduct_vrf(sender.id, cid, GIFT_COST):
        await update.message.reply_text("❌ Недостаточно VRF")
        return

    new_bal = await db_add_vrf(target.id, cid, reward)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_gift=? WHERE user_id=? AND chat_id=?",
                         (_now(), sender.id, cid))
        await db.commit()

    partner_mark = " 💍 (бонус партнёра)" if reward == GIFT_MARRIED_REWARD else ""
    await update.message.reply_text(
        f"🎁 {mention(sender.id, sender.first_name)} дарит VRF!\n"
        f"→ {mention(target.id, target.first_name)}\n"
        f"💎 +{reward} VRF{partner_mark}\n"
        f"Баланс: {fmt(new_bal)} VRF",
        parse_mode=ParseMode.HTML,
    )


@only_groups
async def cmd_love(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sender = update.effective_user
    cid    = update.effective_chat.id

    if not update.message.reply_to_message or update.message.reply_to_message.from_user.is_bot:
        await update.message.reply_text("❌ Ответь на сообщение получателя!")
        return

    target = update.message.reply_to_message.from_user
    if target.id == sender.id:
        await update.message.reply_text("💘 Начни любить других, а не только себя!")
        return

    await db_ensure_user(sender.id, cid, sender.username or "", sender.first_name)
    await db_ensure_user(target.id, cid, target.username or "", target.first_name)

    su = await db_get_user(sender.id, cid)
    last_love = su.get("last_love")
    if last_love:
        elapsed = (datetime.now() - datetime.fromisoformat(last_love)).total_seconds()
        if elapsed < LOVE_COOLDOWN_M * 60:
            rem = int(LOVE_COOLDOWN_M * 60 - elapsed)
            await update.message.reply_text(f"⏰ Любовь можно слать через {fmt_cd(rem)}")
            return

    m           = await db_get_marriage(sender.id, cid)
    is_partner  = m and partner_id(m, sender.id) == target.id
    s_reward    = LOVE_MARRIED_REWARD if is_partner else LOVE_REWARD
    r_reward    = LOVE_MARRIED_REWARD if is_partner else LOVE_REWARD

    await db_add_vrf(sender.id, cid, s_reward)
    new_bal = await db_add_vrf(target.id, cid, r_reward)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_love=? WHERE user_id=? AND chat_id=?",
                         (_now(), sender.id, cid))
        await db.commit()

    actions = ["шлёт поцелуй 💋", "обнимает 🤗", "дарит цветок 🌸", "признаётся в любви 💌"]
    if is_partner:
        actions = ["целует свою половинку 💋", "обнимает любимого(ую) 🤗", "дарит красную розу 🌹"]

    await update.message.reply_text(
        f"{E_LOVE} {mention(sender.id, sender.first_name)} {random.choice(actions)}\n"
        f"→ {mention(target.id, target.first_name)}\n"
        f"💎 Оба получают +{r_reward} VRF"
        + (" 💍" if is_partner else ""),
        parse_mode=ParseMode.HTML,
    )
    await _react(update, "❤️")


# ══════════════════════════════════════════════════════
#                MARRIAGE COMMANDS
# ══════════════════════════════════════════════════════

@only_groups
async def cmd_marry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    proposer = update.effective_user
    cid      = update.effective_chat.id

    target, err = await _resolve_target(update, context, cid)
    if err:
        await update.message.reply_text(err)
        return
    if not target:
        await update.message.reply_text("❌ Укажи пользователя через ответ или @username")
        return
    if target.id == proposer.id:
        await update.message.reply_text("💘 Жениться на себе нельзя!")
        return
    if await db_get_marriage(proposer.id, cid):
        await update.message.reply_text("💍 Ты уже в браке! Сначала /divorce")
        return
    if await db_get_marriage(target.id, cid):
        await update.message.reply_text(
            f"💔 {mention(target.id, target.first_name)} уже в браке!",
            parse_mode=ParseMode.HTML,
        )
        return

    await db_ensure_user(proposer.id, cid, proposer.username or "", proposer.first_name)
    await db_ensure_user(target.id, cid, getattr(target, "username", "") or "", target.first_name)

    prop = await db_get_proposal_to(proposer.id, cid)
    if prop and prop["proposer_id"] == target.id:
        await db_create_marriage(proposer.id, target.id, cid)
        await update.message.reply_text(
            f"{E_RING} <b>Взаимная любовь — Свадьба!</b>\n\n"
            f"💑 {mention(proposer.id, proposer.first_name)} ❤️ "
            f"{mention(target.id, target.first_name)}\n\n"
            f"🎊 Поздравляем! Бонус к /daily активирован!",
            parse_mode=ParseMode.HTML,
        )
        await _react(update, "🎊")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO proposals(proposer_id,target_id,chat_id,created_at) VALUES(?,?,?,?)",
            (proposer.id, target.id, cid, _now()),
        )
        await db.commit()

    phrase = random.choice(["делает предложение", "встаёт на одно колено перед", "хочет связать жизнь с"])
    await update.message.reply_text(
        f"{E_RING} {mention(proposer.id, proposer.first_name)} {phrase} "
        f"{mention(target.id, target.first_name)}!\n\nПримешь предложение?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("💍 Да!", callback_data=f"ma:{proposer.id}:{target.id}"),
            InlineKeyboardButton("💔 Нет", callback_data=f"mr:{proposer.id}:{target.id}"),
        ]]),
    )


@only_groups
async def cmd_accept(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u   = update.effective_user
    cid = update.effective_chat.id
    prop = await db_get_proposal_to(u.id, cid)
    if not prop:
        await update.message.reply_text("❌ У тебя нет входящих предложений")
        return
    if await db_get_marriage(u.id, cid) or await db_get_marriage(prop["proposer_id"], cid):
        await update.message.reply_text("❌ Один из вас уже в браке!")
        return
    pu    = await db_get_user(prop["proposer_id"], cid)
    pname = pu["first_name"] if pu else "Партнёр"
    await db_create_marriage(prop["proposer_id"], u.id, cid)
    await update.message.reply_text(
        f"💒 <b>Поздравляем с бракосочетанием!</b>\n\n"
        f"💑 {mention(prop['proposer_id'], pname)} ❤️ {mention(u.id, u.first_name)}\n\n"
        f"🎊 Бонус к /daily активирован!",
        parse_mode=ParseMode.HTML,
    )
    await _react(update, "🎊")


@only_groups
async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u   = update.effective_user
    cid = update.effective_chat.id
    prop = await db_get_proposal_to(u.id, cid)
    if not prop:
        await update.message.reply_text("❌ У тебя нет входящих предложений")
        return
    pu    = await db_get_user(prop["proposer_id"], cid)
    pname = pu["first_name"] if pu else "Пользователь"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM proposals WHERE target_id=? AND chat_id=?", (u.id, cid))
        await db.commit()
    await update.message.reply_text(
        f"💔 {mention(u.id, u.first_name)} отклонил(а) предложение от {mention(prop['proposer_id'], pname)}",
        parse_mode=ParseMode.HTML,
    )


@only_groups
async def cmd_divorce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u   = update.effective_user
    cid = update.effective_chat.id
    m   = await db_get_marriage(u.id, cid)
    if not m:
        await update.message.reply_text("💔 Ты не в браке")
        return
    pid   = partner_id(m, u.id)
    pu    = await db_get_user(pid, cid)
    pname = pu["first_name"] if pu else "Партнёр"
    d     = days_ago(m["married_at"])
    await db_delete_marriage(m["id"])
    await update.message.reply_text(
        f"💔 <b>Развод оформлен</b>\n\nПосле {d} дней вместе...\n"
        f"{mention(u.id, u.first_name)} и {mention(pid, pname)} расстались.",
        parse_mode=ParseMode.HTML,
    )


@only_groups
async def cmd_marriage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u   = update.effective_user
    cid = update.effective_chat.id
    await db_ensure_user(u.id, cid, u.username or "", u.first_name)
    m = await db_get_marriage(u.id, cid)
    if not m:
        prop = await db_get_proposal_to(u.id, cid)
        if prop:
            pu    = await db_get_user(prop["proposer_id"], cid)
            pname = pu["first_name"] if pu else "Кто-то"
            await update.message.reply_text(
                f"{E_RING} Предложение от {mention(prop['proposer_id'], pname)}!\n"
                f"💍 /accept — принять · 💔 /reject — отклонить",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text("💔 Ты не в браке.\n\n/marry @username — найди пару!")
        return
    pid   = partner_id(m, u.id)
    pu    = await db_get_user(pid, cid)
    pname = pu["first_name"] if pu else "Партнёр"
    since = datetime.fromisoformat(m["married_at"])
    delta = datetime.now() - since
    await update.message.reply_text(
        f"💑 <b>Ваш брак</b>\n\n"
        f"  {mention(u.id, u.first_name)}\n  ❤️\n  {mention(pid, pname)}\n\n"
        f"⏰ Вместе: <b>{delta.days} дн. {delta.seconds//3600} ч.</b>\n"
        f"📅 С: <b>{since.strftime('%d.%m.%Y')}</b>\n\n"
        f"🎁 Бонус: +{DAILY_MARRIED_BONUS} VRF к /daily",
        parse_mode=ParseMode.HTML,
    )


@only_groups
async def cmd_marriages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cid   = update.effective_chat.id
    all_m = await db_all_marriages(cid)
    if not all_m:
        await update.message.reply_text("💔 В чате пока нет пар.\n\n/marry — найди свою половинку!")
        return
    lines = [f"💑 <b>Пары чата ({len(all_m)})</b>\n"]
    shown = 0
    for m in all_m:
        u1 = await db_get_user(m["user1_id"], cid)
        u2 = await db_get_user(m["user2_id"], cid)
        if not u1 or not u2:
            continue
        shown += 1
        lines.append(
            f"{shown}. {mention(m['user1_id'], u1['first_name'])} ❤️ "
            f"{mention(m['user2_id'], u2['first_name'])} · {days_ago(m['married_at'])} дн."
        )
        if shown >= 15:
            break
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ══════════════════════════════════════════════════════
#                  DUEL GAME ⚔️
# ══════════════════════════════════════════════════════

@only_groups
async def cmd_duel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    challenger = update.effective_user
    cid        = update.effective_chat.id

    if not update.message.reply_to_message or update.message.reply_to_message.from_user.is_bot:
        await update.message.reply_text("⚔️ Ответь на сообщение соперника чтобы вызвать на дуэль!")
        return

    opponent = update.message.reply_to_message.from_user
    if opponent.id == challenger.id:
        await update.message.reply_text("⚔️ Нельзя вызвать самого себя!")
        return

    await db_ensure_user(challenger.id, cid, challenger.username or "", challenger.first_name)
    await db_ensure_user(opponent.id,   cid, opponent.username   or "", opponent.first_name)
    cu = await db_get_user(challenger.id, cid)
    ou = await db_get_user(opponent.id,   cid)

    bet = calc_bet(cu["vrf"], ou["vrf"])
    if cu["vrf"] < bet or ou["vrf"] < MIN_BET:
        await update.message.reply_text(f"❌ Недостаточно VRF для дуэли!\nМинимум: {MIN_BET} VRF")
        return

    key = f"{cid}:{challenger.id}:{opponent.id}"
    duel_challenges[key] = {
        "cid": cid, "c_id": challenger.id, "c_name": challenger.first_name,
        "o_id": opponent.id, "o_name": opponent.first_name, "bet": bet,
    }

    await update.message.reply_text(
        f"⚔️ <b>ВЫЗОВ НА ДУЭЛЬ!</b>\n\n"
        f"{E_ALERT} {mention(challenger.id, challenger.first_name)} вызывает\n"
        f"{mention(opponent.id, opponent.first_name)}!\n\n"
        f"💰 Ставка: <b>{bet} VRF</b>\n"
        f"🎲 Бросок определяется VRF (кости Telegram)\n"
        f"⭐ Бонус уровня добавляется к броску\n\n"
        f"{mention(opponent.id, opponent.first_name)}, принимаешь?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⚔️ Принять",    callback_data=f"da:{challenger.id}:{opponent.id}"),
            InlineKeyboardButton("🏳️ Отклонить", callback_data=f"dd:{challenger.id}:{opponent.id}"),
        ]]),
    )


async def _run_duel(context: ContextTypes.DEFAULT_TYPE, data: dict) -> None:
    cid   = data["cid"]
    c_id, c_name = data["c_id"], data["c_name"]
    o_id, o_name = data["o_id"], data["o_name"]
    bet   = data["bet"]

    await context.bot.send_message(cid, f"⚔️ Дуэль! 🎲 {mention(c_id, c_name)} бросает...",
                                   parse_mode=ParseMode.HTML)
    await asyncio.sleep(1)
    msg_c = await context.bot.send_dice(chat_id=cid, emoji="🎲")
    c_roll = msg_c.dice.value
    await asyncio.sleep(3)

    await context.bot.send_message(cid, f"🎲 {mention(o_id, o_name)} бросает...",
                                   parse_mode=ParseMode.HTML)
    msg_o = await context.bot.send_dice(chat_id=cid, emoji="🎲")
    o_roll = msg_o.dice.value
    await asyncio.sleep(3)

    cu = await db_get_user(c_id, cid)
    ou = await db_get_user(o_id, cid)
    c_total = c_roll + get_level(cu["experience"] if cu else 0)
    o_total = o_roll + get_level(ou["experience"] if ou else 0)

    if c_total == o_total:
        await context.bot.send_message(
            cid,
            f"🤝 <b>НИЧЬЯ!</b>\n\n"
            f"{mention(c_id, c_name)}: {c_roll} (+ур.) = {c_total}\n"
            f"{mention(o_id, o_name)}: {o_roll} (+ур.) = {o_total}\n\n"
            f"Ставка {bet} VRF возвращена!",
            parse_mode=ParseMode.HTML,
        )
        await db_record_game(c_id, cid, won=False, draw=True)
        await db_record_game(o_id, cid, won=False, draw=True)
        return

    if c_total > o_total:
        w_id, w_name = c_id, c_name
        l_id         = o_id
    else:
        w_id, w_name = o_id, o_name
        l_id         = c_id

    await db_deduct_vrf(l_id, cid, bet)
    new_bal = await db_add_vrf(w_id, cid, bet)
    await db_add_xp(w_id, cid, XP_PER_WIN)
    await db_add_xp(l_id, cid, XP_PER_GAME)
    await db_record_game(w_id, cid, won=True)
    await db_record_game(l_id, cid, won=False)

    rich_h = (
        f"<h3>⚔️ Дуэль &mdash; Результат</h3>"
        f"<table bordered>"
        f"<tr><th>Игрок</th><th align=\"center\">🎲</th><th align=\"center\">+Ур.</th><th align=\"right\">Итого</th></tr>"
        f"<tr><td>{'<b>' if c_total > o_total else ''}{c_name}{'</b>' if c_total > o_total else ''}</td>"
        f"<td align=\"center\">{c_roll}</td><td align=\"center\">+{get_level(cu['experience'] if cu else 0)}</td>"
        f"<td align=\"right\">{'<b>' if c_total > o_total else ''}{c_total}{'</b>' if c_total > o_total else ''}</td></tr>"
        f"<tr><td>{'<b>' if o_total > c_total else ''}{o_name}{'</b>' if o_total > c_total else ''}</td>"
        f"<td align=\"center\">{o_roll}</td><td align=\"center\">+{get_level(ou['experience'] if ou else 0)}</td>"
        f"<td align=\"right\">{'<b>' if o_total > c_total else ''}{o_total}{'</b>' if o_total > c_total else ''}</td></tr>"
        f"</table>"
        f"<blockquote>{E_WIN1} Победитель: <b>{w_name}</b><br/>💎 +{fmt(bet)} VRF &rarr; Баланс: {fmt(new_bal)} VRF</blockquote>"
    )
    fb_h = (
        f"{E_WIN1} <b>ПОБЕДИТЕЛЬ!</b>\n\n"
        f"{mention(c_id, c_name)}: {c_roll} + ур. = <b>{c_total}</b>\n"
        f"{mention(o_id, o_name)}: {o_roll} + ур. = <b>{o_total}</b>\n\n"
        f"{E_WIN2} {mention(w_id, w_name)} побеждает!\n"
        f"💎 +{bet} VRF → Баланс: {fmt(new_bal)} VRF"
    )
    await send_rich(context.bot, cid, html=rich_h, fallback_html=fb_h)


# ══════════════════════════════════════════════════════
#               CUBES GAME 🎲
# ══════════════════════════════════════════════════════

@only_groups
async def cmd_cubes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    host = update.effective_user
    cid  = update.effective_chat.id

    if not update.message.reply_to_message or update.message.reply_to_message.from_user.is_bot:
        await update.message.reply_text("🎲 Ответь на сообщение соперника!")
        return
    opponent = update.message.reply_to_message.from_user
    if opponent.id == host.id:
        await update.message.reply_text("❌ Нельзя играть с собой!")
        return

    rounds = DEFAULT_ROUNDS
    bet    = 50
    try:
        if context.args and len(context.args) >= 1:
            rounds = max(1, min(int(context.args[0]), MAX_ROUNDS))
        if context.args and len(context.args) >= 2:
            bet = max(MIN_BET, min(int(context.args[1]), MAX_BET))
    except ValueError:
        await update.message.reply_text("❌ Использование: /cubes [раунды 1-10] [ставка 10-500]")
        return

    await db_ensure_user(host.id,     cid, host.username     or "", host.first_name)
    await db_ensure_user(opponent.id, cid, opponent.username or "", opponent.first_name)
    hu = await db_get_user(host.id, cid)
    if hu["vrf"] < bet:
        await update.message.reply_text(f"❌ Нужно {bet} VRF · Есть: {hu['vrf']} VRF")
        return

    game_id = str(uuid.uuid4())[:8]
    cubes_games[game_id] = {
        "host_id": host.id, "host_name": host.first_name,
        "opp_id": opponent.id, "opp_name": opponent.first_name,
        "cid": cid, "rounds": rounds, "bet": bet, "state": "waiting",
    }

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"🎲 Принять — {bet} VRF", callback_data=f"cj:{game_id}"),
        InlineKeyboardButton("❌ Отказать", callback_data=f"cd:{game_id}"),
    ]])

    msg = await update.message.reply_text(
        f"🎲 <b>Игра в кости!</b>\n\n"
        f"🤺 {mention(host.id, host.first_name)} вызывает\n"
        f"{mention(opponent.id, opponent.first_name)}\n\n"
        f"📊 Раундов: <b>{rounds}</b>\n"
        f"💎 Ставка: <b>{bet} VRF</b> с каждого\n"
        f"🏆 Победитель забирает: <b>{bet*2} VRF</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )

    bot = context.bot
    mid = msg.message_id

    async def auto_cancel():
        await asyncio.sleep(JOIN_TIMEOUT)
        if game_id in cubes_games and cubes_games[game_id]["state"] == "waiting":
            del cubes_games[game_id]
            try:
                await bot.edit_message_reply_markup(cid, mid, reply_markup=None)
                await bot.send_message(cid, "⏰ Игра в кости истекла.")
            except TelegramError:
                pass

    context.application.create_task(auto_cancel())


async def _run_cubes(context: ContextTypes.DEFAULT_TYPE, game: dict) -> None:
    cid    = game["cid"]
    h_id, h_name = game["host_id"], game["host_name"]
    o_id, o_name = game["opp_id"],  game["opp_name"]
    rounds = game["rounds"]
    bet    = game["bet"]
    h_score = o_score = 0

    await context.bot.send_message(
        cid,
        f"🎲 <b>КОСТИ НАЧАЛИСЬ!</b>\n"
        f"{mention(h_id, h_name)} ⚔️ {mention(o_id, o_name)}\n"
        f"Раундов: {rounds} | Ставка: {bet} VRF",
        parse_mode=ParseMode.HTML,
    )
    await asyncio.sleep(2)

    for r in range(1, rounds + 1):
        await context.bot.send_message(cid,
            f"🎲 <b>Раунд {r}/{rounds}</b>\n{mention(h_id, h_name)} бросает...",
            parse_mode=ParseMode.HTML)
        h_val = (await context.bot.send_dice(chat_id=cid, emoji="🎲")).dice.value
        await asyncio.sleep(3)

        await context.bot.send_message(cid,
            f"{mention(o_id, o_name)} бросает...", parse_mode=ParseMode.HTML)
        o_val = (await context.bot.send_dice(chat_id=cid, emoji="🎲")).dice.value
        await asyncio.sleep(3)

        h_score += h_val
        o_score += o_val
        r_res = f"🏅 {mention(h_id, h_name)} берёт раунд!" if h_val > o_val else \
                f"🏅 {mention(o_id, o_name)} берёт раунд!" if o_val > h_val else "🤝 Ничья!"

        await context.bot.send_message(cid,
            f"📊 Раунд {r}: <b>{h_val}</b> vs <b>{o_val}</b>\n"
            f"{r_res}\nСчёт: <b>{h_score} — {o_score}</b>",
            parse_mode=ParseMode.HTML)
        await asyncio.sleep(2)

    if h_score == o_score:
        await db_record_game(h_id, cid, won=False, draw=True)
        await db_record_game(o_id, cid, won=False, draw=True)
        await context.bot.send_message(cid,
            f"🤝 <b>НИЧЬЯ!</b>\nИтог: {h_score} — {o_score}\nСтавки возвращены!",
            parse_mode=ParseMode.HTML)
        return

    w_id, w_name = (h_id, h_name) if h_score > o_score else (o_id, o_name)
    l_id         = o_id if w_id == h_id else h_id

    await db_deduct_vrf(l_id, cid, bet)
    new_bal = await db_add_vrf(w_id, cid, bet)
    await db_add_xp(w_id, cid, XP_PER_WIN)
    await db_add_xp(l_id, cid, XP_PER_GAME)
    await db_record_game(w_id, cid, won=True)
    await db_record_game(l_id, cid, won=False)

    await send_rich(context.bot, cid,
        html=(
            f"<h3>🎲 Кубики &mdash; Итог</h3>"
            f"<table bordered>"
            f"<tr><th>Игрок</th><th align=\"right\">Очки</th></tr>"
            f"<tr><td>{"<b>" if h_score > o_score else ""}{h_name}{"</b>" if h_score > o_score else ""}</td>"
            f"<td align=\"right\">{"<b>" if h_score > o_score else ""}{h_score}{"</b>" if h_score > o_score else ""}</td></tr>"
            f"<tr><td>{"<b>" if o_score > h_score else ""}{o_name}{"</b>" if o_score > h_score else ""}</td>"
            f"<td align=\"right\">{"<b>" if o_score > h_score else ""}{o_score}{"</b>" if o_score > h_score else ""}</td></tr>"
            f"</table>"
            f"<blockquote>{E_WIN1} <b>{w_name}</b> побеждает!<br/>Раундов: {rounds} | 💎 +{fmt(bet)} VRF &rarr; {fmt(new_bal)} VRF</blockquote>"
        ),
        fallback_html=f"{E_WIN1} <b>ПОБЕДИТЕЛЬ!</b>\n🏆 {mention(w_id, w_name)}\n📊 {h_score}:{o_score} | 💎 +{fmt(bet)} VRF")


# ══════════════════════════════════════════════════════
#        SPORTS GAMES 🏀⚽🎳🎯 (shared logic)
# ══════════════════════════════════════════════════════

async def _cmd_sport(update: Update, context: ContextTypes.DEFAULT_TYPE, game_type: str) -> None:
    host = update.effective_user
    cid  = update.effective_chat.id

    if not update.message.reply_to_message or update.message.reply_to_message.from_user.is_bot:
        emoji = SPORT_EMOJI[game_type]
        await update.message.reply_text(f"{emoji} Ответь на сообщение соперника!")
        return

    opponent = update.message.reply_to_message.from_user
    if opponent.id == host.id:
        await update.message.reply_text("❌ Нельзя играть с собой!")
        return

    await db_ensure_user(host.id,     cid, host.username     or "", host.first_name)
    await db_ensure_user(opponent.id, cid, opponent.username or "", opponent.first_name)
    hu = await db_get_user(host.id, cid)
    ou = await db_get_user(opponent.id, cid)
    bet = calc_bet(hu["vrf"], ou["vrf"])

    if hu["vrf"] < bet or ou["vrf"] < bet:
        await update.message.reply_text(f"❌ Нужно {bet} VRF у обоих игроков!")
        return

    game_id = str(uuid.uuid4())[:8]
    sports_games[game_id] = {
        "type": game_type,
        "host_id": host.id, "host_name": host.first_name,
        "opp_id": opponent.id, "opp_name": opponent.first_name,
        "cid": cid, "rounds": DEFAULT_ROUNDS, "bet": bet, "state": "waiting",
    }

    emoji = SPORT_EMOJI[game_type]
    name  = SPORT_NAME[game_type]
    msg   = await update.message.reply_text(
        f"{emoji} <b>{name}!</b>\n\n"
        f"🤺 {mention(host.id, host.first_name)} вызывает\n"
        f"{mention(opponent.id, opponent.first_name)}\n\n"
        f"📊 Раундов: <b>{DEFAULT_ROUNDS}</b>\n"
        f"💎 Ставка: <b>{bet} VRF</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(f"{emoji} Принять", callback_data=f"sj:{game_id}"),
            InlineKeyboardButton("❌ Отказать",       callback_data=f"sd:{game_id}"),
        ]]),
    )

    bot = context.bot
    mid = msg.message_id

    async def auto_cancel():
        await asyncio.sleep(JOIN_TIMEOUT)
        if game_id in sports_games and sports_games[game_id]["state"] == "waiting":
            del sports_games[game_id]
            try:
                await bot.edit_message_reply_markup(cid, mid, reply_markup=None)
                await bot.send_message(cid, f"⏰ Вызов на {name} истёк.")
            except TelegramError:
                pass

    context.application.create_task(auto_cancel())


async def _run_sports(context: ContextTypes.DEFAULT_TYPE, game: dict) -> None:
    cid    = game["cid"]
    gtype  = game["type"]
    h_id, h_name = game["host_id"], game["host_name"]
    o_id, o_name = game["opp_id"],  game["opp_name"]
    rounds = game["rounds"]
    bet    = game["bet"]
    emoji  = SPORT_EMOJI[gtype]
    name   = SPORT_NAME[gtype]

    h_total = o_total = 0

    await context.bot.send_message(
        cid,
        f"{emoji} <b>{name.upper()} НАЧАЛСЯ!</b>\n"
        f"{mention(h_id, h_name)} ⚔️ {mention(o_id, o_name)}",
        parse_mode=ParseMode.HTML,
    )
    await asyncio.sleep(2)

    for r in range(1, rounds + 1):
        await context.bot.send_message(cid,
            f"{emoji} <b>Раунд {r}/{rounds}</b>\n{mention(h_id, h_name)} бросает...",
            parse_mode=ParseMode.HTML)
        h_val = (await context.bot.send_dice(chat_id=cid, emoji=emoji)).dice.value
        h_pts, h_lbl = score_throw(gtype, h_val)
        await asyncio.sleep(3)

        await context.bot.send_message(cid,
            f"{mention(o_id, o_name)} бросает...", parse_mode=ParseMode.HTML)
        o_val = (await context.bot.send_dice(chat_id=cid, emoji=emoji)).dice.value
        o_pts, o_lbl = score_throw(gtype, o_val)
        await asyncio.sleep(3)

        h_total += h_pts
        o_total += o_pts
        r_res = f"🏅 {mention(h_id, h_name)} берёт раунд!" if h_pts > o_pts else \
                f"🏅 {mention(o_id, o_name)} берёт раунд!" if o_pts > h_pts else "🤝 Ничья!"

        await context.bot.send_message(cid,
            f"📊 Раунд {r}: {h_lbl} | {o_lbl}\n"
            f"{r_res}\nСчёт: <b>{h_total} — {o_total}</b>",
            parse_mode=ParseMode.HTML)
        await asyncio.sleep(2)

    if h_total == o_total:
        await db_record_game(h_id, cid, won=False, draw=True)
        await db_record_game(o_id, cid, won=False, draw=True)
        await context.bot.send_message(cid,
            f"🤝 <b>НИЧЬЯ!</b>\nИтог: {h_total} — {o_total}\nСтавки возвращены!",
            parse_mode=ParseMode.HTML)
        return

    w_id, w_name = (h_id, h_name) if h_total > o_total else (o_id, o_name)
    l_id         = o_id if w_id == h_id else h_id

    await db_deduct_vrf(l_id, cid, bet)
    new_bal = await db_add_vrf(w_id, cid, bet)
    await db_add_xp(w_id, cid, XP_PER_WIN)
    await db_add_xp(l_id, cid, XP_PER_GAME)
    await db_record_game(w_id, cid, won=True)
    await db_record_game(l_id, cid, won=False)

    await send_rich(context.bot, cid,
        html=(
            f"<h3>{emoji} {name} &mdash; Итог</h3>"
            f"<table bordered>"
            f"<tr><th>Игрок</th><th align=\"right\">Очки</th></tr>"
            f"<tr><td>{"<b>" if h_total > o_total else ""}{h_name}{"</b>" if h_total > o_total else ""}</td>"
            f"<td align=\"right\">{"<b>" if h_total > o_total else ""}{h_total}{"</b>" if h_total > o_total else ""}</td></tr>"
            f"<tr><td>{"<b>" if o_total > h_total else ""}{o_name}{"</b>" if o_total > h_total else ""}</td>"
            f"<td align=\"right\">{"<b>" if o_total > h_total else ""}{o_total}{"</b>" if o_total > h_total else ""}</td></tr>"
            f"</table>"
            f"<blockquote>{E_WIN1} <b>{w_name}</b> побеждает!<br/>Раундов: {rounds} | 💎 +{fmt(bet)} VRF &rarr; {fmt(new_bal)} VRF</blockquote>"
        ),
        fallback_html=f"{E_WIN1} <b>ПОБЕДИТЕЛЬ!</b>\n🏆 {mention(w_id, w_name)}\n📊 {h_total}:{o_total} | 💎 +{fmt(bet)} VRF")


@only_groups
async def cmd_basket(update, context):   await _cmd_sport(update, context, "basket")
@only_groups
async def cmd_football(update, context): await _cmd_sport(update, context, "football")
@only_groups
async def cmd_bowling(update, context):  await _cmd_sport(update, context, "bowling")
@only_groups
async def cmd_darts(update, context):    await _cmd_sport(update, context, "darts")


# ══════════════════════════════════════════════════════
#              SLOT MACHINE 🎰
# ══════════════════════════════════════════════════════

@only_groups
async def cmd_slot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    host = update.effective_user
    cid  = update.effective_chat.id

    if not update.message.reply_to_message or update.message.reply_to_message.from_user.is_bot:
        await update.message.reply_text("🎰 Ответь на сообщение соперника!")
        return

    opponent = update.message.reply_to_message.from_user
    if opponent.id == host.id:
        await update.message.reply_text("❌ Нельзя играть с собой!")
        return

    await db_ensure_user(host.id,     cid, host.username     or "", host.first_name)
    await db_ensure_user(opponent.id, cid, opponent.username or "", opponent.first_name)
    hu = await db_get_user(host.id, cid)
    ou = await db_get_user(opponent.id, cid)
    bet = calc_bet(hu["vrf"], ou["vrf"])

    if hu["vrf"] < bet or ou["vrf"] < bet:
        await update.message.reply_text(f"❌ Нужно {bet} VRF у обоих игроков!")
        return

    game_id = str(uuid.uuid4())[:8]
    slot_games[game_id] = {
        "host_id": host.id, "host_name": host.first_name,
        "opp_id": opponent.id, "opp_name": opponent.first_name,
        "cid": cid, "bet": bet, "state": "waiting",
        "h_val": None, "o_val": None,
    }

    await update.message.reply_text(
        f"🎰 <b>СЛОТ-МАШИНА PvP!</b>\n\n"
        f"🤺 {mention(host.id, host.first_name)}\n"
        f"⚔️ {mention(opponent.id, opponent.first_name)}\n\n"
        f"💎 Ставка: <b>{bet} VRF</b> с каждого\n"
        f"🏆 Лучшая комбинация побеждает!",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🎰 Принять вызов", callback_data=f"slj:{game_id}"),
            InlineKeyboardButton("❌ Отказать",       callback_data=f"sld:{game_id}"),
        ]]),
    )


# ══════════════════════════════════════════════════════
#              MINES GAME 💣
# ══════════════════════════════════════════════════════

MINES_TOTAL = 25  # 5 × 5 grid


def calc_mines_mult(safe_revealed: int, mines_count: int) -> float:
    """Fair payout multiplier with 3 % house edge."""
    if safe_revealed == 0:
        return 1.0
    safe_total = MINES_TOTAL - mines_count
    prob = 1.0
    for i in range(safe_revealed):
        prob *= (safe_total - i) / (MINES_TOTAL - i)
    return max(1.01, round(0.97 / prob, 2))


def _mines_grid_kb(uid: int, cid: int, game: dict) -> InlineKeyboardMarkup:
    """Render the live 5×5 grid + cashout/quit row."""
    grid, rev = game["grid"], game["revealed"]
    rows = []
    for r in range(5):
        row = []
        for c in range(5):
            i = r * 5 + c
            if rev[i]:
                txt = "💣" if grid[i] else "💎"
                cb  = "mg:noop"
            else:
                txt = "⬜"
                cb  = f"mg:c:{uid}:{cid}:{i}"
            row.append(InlineKeyboardButton(txt, callback_data=cb))
        rows.append(row)
    mult   = calc_mines_mult(game["safe_revealed"], game["mines_count"])
    payout = int(game["bet"] * mult)
    rows.append([
        InlineKeyboardButton(
            f"💸 Забрать {fmt(payout)} VRF  ({mult}×)",
            callback_data=f"mg:co:{uid}:{cid}",
        ),
        InlineKeyboardButton("🏳 Сдаться", callback_data=f"mg:q:{uid}:{cid}"),
    ])
    return InlineKeyboardMarkup(rows)


def _mines_dead_kb(game: dict, boom_idx: int = -1) -> InlineKeyboardMarkup:
    """Non-clickable result grid revealing all mines."""
    grid, rev = game["grid"], game["revealed"]
    rows = []
    for r in range(5):
        row = []
        for c in range(5):
            i = r * 5 + c
            if i == boom_idx:
                txt = E_BOOM
            elif grid[i]:
                txt = "💣"
            elif rev[i]:
                txt = "💎"
            else:
                txt = "⬛"
            row.append(InlineKeyboardButton(txt, callback_data="mg:noop"))
        rows.append(row)
    rows.append([InlineKeyboardButton("🎮 Играть снова", callback_data="mg:new")])
    return InlineKeyboardMarkup(rows)


def _mines_header(game: dict) -> str:
    mult      = calc_mines_mult(game["safe_revealed"], game["mines_count"])
    payout    = int(game["bet"] * mult)
    safe_left = MINES_TOTAL - game["mines_count"] - game["safe_revealed"]
    return (
        f"💣 <b>Мины</b>  ·  Ставка: <b>{fmt(game['bet'])} VRF</b>\n"
        f"💣 Мин на поле: <b>{game['mines_count']}</b>  ·  "
        f"✅ Открыто: <b>{game['safe_revealed']}</b>  ·  "
        f"⚡ Множитель: <b>{mult}×</b>\n"
        f"💰 Забрать прямо сейчас: <b>{fmt(payout)} VRF</b>\n"
        f"🔍 Осталось безопасных: <b>{safe_left}</b>\n\n"
        f"Нажимай ⬜ — ищи 💎, избегай 💣!"
    )


def _mines_bet_kb(uid: int, cid: int) -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(f"💎 {v} VRF",
            callback_data=f"mg:b:{uid}:{cid}:{v}") for v in [10, 25, 50]]
    row2 = [InlineKeyboardButton(f"💎 {v} VRF",
            callback_data=f"mg:b:{uid}:{cid}:{v}") for v in [100, 200, 500]]
    return InlineKeyboardMarkup([row1, row2,
        [InlineKeyboardButton("❌ Отмена", callback_data="mg:cancel")]])


@only_groups
async def cmd_mines(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u   = update.effective_user
    cid = update.effective_chat.id
    key = f"{u.id}:{cid}"

    # Resume existing game if still active
    if key in mines_games and mines_games[key]["state"] == "active":
        g = mines_games[key]
        await update.message.reply_text(
            "♻️ <b>У тебя уже есть активная игра!</b>\n\n" + _mines_header(g),
            parse_mode=ParseMode.HTML,
            reply_markup=_mines_grid_kb(u.id, cid, g),
        )
        return

    await db_ensure_user(u.id, cid, u.username or "", u.first_name)
    uu = await db_get_user(u.id, cid)
    if not uu:
        return

    await update.message.reply_text(
        f"💣 <b>Мины</b>\n\n"
        f"💎 Баланс: <b>{fmt(uu['vrf'])} VRF</b>\n\n"
        f"Открывай клетки — ищи 💎 и избегай 💣\n"
        f"Чем больше клеток откроешь — тем выше множитель!\n"
        f"В любой момент нажми <b>Забрать</b> и забери выигрыш 💸\n\n"
        f"Выбери ставку:",
        parse_mode=ParseMode.HTML,
        reply_markup=_mines_bet_kb(u.id, cid),
    )


# ══════════════════════════════════════════════════════
#                  SHOP 🛍️
# ══════════════════════════════════════════════════════

_E_BEAR   = 'E_BEAR'
_E_BONUS  = 'E_BONUS'
_E_VRF    = 'E_VRF'

SHOP_ITEMS: dict = {
    "bear":  {"label": f"{_E_BEAR} Медведь",       "vrf": 500,  "desc": "+1 медведь в коллекцию"},
    "boost": {"label": f"{_E_BONUS} Мега-бонус",   "vrf": 300,  "desc": "×2 к /daily на 24 часа"},
    "hint":  {"label": "💡 Подсказка в Минах",     "vrf": 50,   "desc": "Открыть 1 безопасную клетку"},
}

STARS_PACKAGES: list = [
    (10,  50,   0),    # (stars, base_vrf, bonus_vrf)
    (50,  250,  10),
    (100, 500,  50),
    (500, 2500, 500),
]


def _shop_text(bal: int, has_boost: bool) -> str:
    """Plain HTML fallback for clients that don't support rich messages."""
    boost_line = f"\n{_E_BONUS} <b>Мега-бонус АКТИВЕН!</b>" if has_boost else ""
    lines = [f"🛍️ <b>Магазин Verifure</b>\n{_E_VRF} Баланс: <b>{fmt(bal)} VRF</b>{boost_line}\n\n<b>За VRF:</b>\n"]
    for item in SHOP_ITEMS.values():
        lines.append(f"  {item['label']} — <b>{item['vrf']} VRF</b>  {item['desc']}\n")
    lines.append("\n<b>⭐ Stars → 💎 VRF:</b>\n")
    for stars, base, bonus in STARS_PACKAGES:
        total = base + bonus
        b = f" <i>+{bonus}</i>" if bonus else ""
        lines.append(f"  {stars}⭐ → <b>{total}</b> VRF{b}\n")
    return "".join(lines)


def _shop_rich_html(bal: int, has_boost: bool) -> str:
    """Rich HTML format for sendRichMessage."""
    boost_bar = f'\n<p><mark>{_E_BONUS} Мега-бонус ×2 к /daily &mdash; активен!</mark></p>' if has_boost else ""
    vrf_rows = ""
    for item in SHOP_ITEMS.values():
        vrf_rows += f'<tr><td>{item["label"]}</td><td align="right"><b>{item["vrf"]}</b></td><td>{item["desc"]}</td></tr>\n'
    stars_rows = ""
    for stars, base, bonus in STARS_PACKAGES:
        total = base + bonus
        b = f"+{bonus} бонус" if bonus else "&mdash;"
        stars_rows += f'<tr><td align="right"><b>{stars}</b> ⭐</td><td align="right"><b>{fmt(total)}</b> 💎</td><td align="center">{b}</td></tr>\n'
    return (
        f"<h2>🛍️ Магазин Verifure</h2>"
        f"<p>{_E_VRF} Твой баланс: <b>{fmt(bal)} VRF</b></p>"
        f"{boost_bar}"
        f"<hr/>"
        f"<h3>🛒 За VRF</h3>"
        f"<table bordered striped>"
        f"<tr><th>Предмет</th><th align=\"right\">VRF</th><th>Описание</th></tr>"
        f"{vrf_rows}"
        f"</table>"
        f"<hr/>"
        f"<h3>⭐ Telegram Stars &rarr; 💎 VRF</h3>"
        f"<table bordered striped>"
        f"<tr><th align=\"right\">Stars ⭐</th><th align=\"right\">VRF 💎</th><th align=\"center\">Бонус</th></tr>"
        f"{stars_rows}"
        f"</table>"
        f"<blockquote>1 ⭐ = 5 💎 VRF — нажми кнопку чтобы купить</blockquote>"
    )


def _shop_kb(uid: int, cid: int) -> InlineKeyboardMarkup:
    rows = []
    for key, item in SHOP_ITEMS.items():
        rows.append([InlineKeyboardButton(
            f"{item['label']} — {item['vrf']} VRF",
            callback_data=f"shop:buy:{key}:{uid}:{cid}",
        )])
    rows.append([InlineKeyboardButton(
        "━━━  Купить VRF за ⭐ Stars  ━━━",
        callback_data="shop:noop",
    )])
    star_row = []
    for stars, base, bonus in STARS_PACKAGES:
        total = base + bonus
        star_row.append(InlineKeyboardButton(
            f"⭐{stars} → 💎{total}",
            callback_data=f"shop:stars:{stars}:{uid}:{cid}",
        ))
        if len(star_row) == 2:
            rows.append(star_row)
            star_row = []
    if star_row:
        rows.append(star_row)
    rows.append([InlineKeyboardButton("❌ Закрыть", callback_data="shop:close")])
    return InlineKeyboardMarkup(rows)


@only_groups
async def cmd_shop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u   = update.effective_user
    cid = update.effective_chat.id
    await db_ensure_user(u.id, cid, u.username or "", u.first_name)
    uu       = await db_get_user(u.id, cid)
    bal      = uu["vrf"] if uu else 0
    has_boost = await db_has_boost(u.id, cid, "daily_boost")
    await send_rich(
        context.bot, cid,
        html=_shop_rich_html(bal, has_boost),
        fallback_html=_shop_text(bal, has_boost),
        reply_to_id=update.message.message_id,
        reply_markup=_shop_kb(u.id, cid),
    )


async def handle_pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.pre_checkout_query
    if q.invoice_payload.startswith("stars_"):
        await q.answer(ok=True)
    else:
        await q.answer(ok=False, error_message="Неверный платёж")


async def handle_stars_payment(message) -> None:
    """Credit VRF after successful Stars payment."""
    payment = message.successful_payment
    parts   = payment.invoice_payload.split("_")   # stars_TOTAL_UID_CID
    try:
        total   = int(parts[1])
        uid     = int(parts[2])
        cid     = int(parts[3])
        new_bal = await db_add_vrf(uid, cid, total)
        stars   = payment.total_amount
        await message.reply_text(
            f"✅ <b>Оплата прошла!</b>\n\n"
            f"⭐ Оплачено: <b>{stars} Stars</b>\n"
            f"{_E_VRF} Зачислено: <b>+{fmt(total)} VRF</b>\n"
            f"💰 Баланс: <b>{fmt(new_bal)} VRF</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.error(f"Stars payment error: {e}")


# ══════════════════════════════════════════════════════
#              ADMIN COMMANDS
# ══════════════════════════════════════════════════════

@only_groups
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_group_or_bot_admin(update):
        await update.message.reply_text("❌ Нет доступа — только для администраторов")
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика",    callback_data="ap:stats"),
         InlineKeyboardButton("🏆 Топ VRF",      callback_data="ap:top")],
        [InlineKeyboardButton("💑 Все браки",     callback_data="ap:marriages"),
         InlineKeyboardButton("👮 Бот-админы",   callback_data="ap:admins")],
        [InlineKeyboardButton("📋 Все команды",   callback_data="ap:cmds"),
         InlineKeyboardButton("ℹ️ Управление",   callback_data="ap:manage")],
        [InlineKeyboardButton("❌ Закрыть",       callback_data="ap:close")],
    ])
    await update.message.reply_text(
        f"🛡️ <b>Verifure Admin Panel</b>\n\n{E_ALERT} Выбери раздел:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


@only_groups
async def cmd_givevrf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_group_or_bot_admin(update):
        await update.message.reply_text("❌ Только для администраторов")
        return
    if not update.message.reply_to_message or not context.args:
        await update.message.reply_text("Использование: /givevrf <сумма> (ответом)")
        return
    try:
        amount = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Укажи сумму: /givevrf 500")
        return
    target  = update.message.reply_to_message.from_user
    cid     = update.effective_chat.id
    await db_ensure_user(target.id, cid, target.username or "", target.first_name)
    new_bal = await db_add_vrf(target.id, cid, amount)
    await update.message.reply_text(
        f"✅ Выдано <b>{fmt(amount)} VRF</b> → {mention(target.id, target.first_name)}\n"
        f"💎 Баланс: {fmt(new_bal)} VRF",
        parse_mode=ParseMode.HTML,
    )


@only_groups
async def cmd_takevrf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_group_or_bot_admin(update):
        await update.message.reply_text("❌ Только для администраторов")
        return
    if not update.message.reply_to_message or not context.args:
        await update.message.reply_text("Использование: /takevrf <сумма> (ответом)")
        return
    try:
        amount = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Укажи сумму")
        return
    target  = update.message.reply_to_message.from_user
    cid     = update.effective_chat.id
    u       = await db_get_user(target.id, cid)
    if not u:
        await update.message.reply_text("❌ Пользователь не найден")
        return
    new_val = max(0, u["vrf"] - amount)
    await db_set_vrf(target.id, cid, new_val)
    await update.message.reply_text(
        f"✅ Списано <b>{fmt(amount)} VRF</b> у {mention(target.id, target.first_name)}\n"
        f"💎 Баланс: {fmt(new_val)} VRF",
        parse_mode=ParseMode.HTML,
    )


@only_groups
async def cmd_givebear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_group_or_bot_admin(update):
        await update.message.reply_text("❌ Только для администраторов")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Ответь на сообщение пользователя")
        return
    target = update.message.reply_to_message.from_user
    cid    = update.effective_chat.id
    await db_ensure_user(target.id, cid, target.username or "", target.first_name)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET bears=bears+1 WHERE user_id=? AND chat_id=?",
                         (target.id, cid))
        await db.commit()
    u = await db_get_user(target.id, cid)
    await update.message.reply_text(
        f"{E_BEAR} {mention(target.id, target.first_name)} получает медведя!\n"
        f"Всего {E_BEAR}: {u['bears']}",
        parse_mode=ParseMode.HTML,
    )


@only_groups
async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_group_or_bot_admin(update):
        await update.message.reply_text("❌ Нет доступа")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Ответь на сообщение пользователя")
        return
    t = update.message.reply_to_message.from_user
    if t.is_bot:
        await update.message.reply_text("❌ Нельзя добавить бота")
        return
    await db_add_admin(t.id, t.username or "", t.first_name or "", update.effective_user.id)
    await update.message.reply_text(
        f"✅ {mention(t.id, t.first_name)} добавлен как бот-администратор!",
        parse_mode=ParseMode.HTML,
    )


@only_groups
async def cmd_removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_group_or_bot_admin(update):
        await update.message.reply_text("❌ Нет доступа")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Ответь на сообщение пользователя")
        return
    t = update.message.reply_to_message.from_user
    if await db_remove_admin(t.id):
        await update.message.reply_text(f"✅ {mention(t.id, t.first_name)} удалён из бот-администраторов",
                                        parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"❌ {mention(t.id, t.first_name)} не является бот-администратором",
                                        parse_mode=ParseMode.HTML)


@only_groups
async def cmd_listadmins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_group_or_bot_admin(update):
        await update.message.reply_text("❌ Нет доступа")
        return
    admins = await db_list_admins()
    lines  = ["👮 <b>Бот-администраторы</b>\n"]
    for a in admins:
        uname = f" @{a['username']}" if a["username"] else ""
        lines.append(f"• {mention(a['user_id'], a['first_name'])}{uname}")
    if ADMIN_IDS:
        lines.append(f"\n🔧 Env ADMIN_IDS: {', '.join(map(str, ADMIN_IDS))}")
    if not admins and not ADMIN_IDS:
        lines.append("Нет бот-администраторов")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ══════════════════════════════════════════════════════
#                CALLBACK HANDLER
# ══════════════════════════════════════════════════════

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query    = update.callback_query
    data     = query.data
    cid      = query.message.chat_id
    who      = query.from_user

    # ── Top tabs ────────────────────────────────────────
    if data.startswith("top:"):
        _, sort, _ = data.split(":")
        await query.answer()
        await _show_top(query, context, cid, sort, edit=True)
        return

    # ── Marriage ────────────────────────────────────────
    if data.startswith("ma:") or data.startswith("mr:"):
        parts  = data.split(":")
        action = parts[0]
        p_id   = int(parts[1])
        t_id   = int(parts[2])

        if who.id != t_id:
            await query.answer("❌ Это предложение не для тебя!", show_alert=True)
            return
        prop = await db_get_proposal_to(t_id, cid)
        if not prop or prop["proposer_id"] != p_id:
            await query.answer("❌ Предложение уже недействительно", show_alert=True)
            await query.edit_message_reply_markup(None)
            return
        pu    = await db_get_user(p_id, cid)
        pname = pu["first_name"] if pu else "Партнёр"

        if action == "ma":
            if await db_get_marriage(p_id, cid) or await db_get_marriage(t_id, cid):
                await query.answer("❌ Один из вас уже в браке!", show_alert=True)
                return
            await db_create_marriage(p_id, t_id, cid)
            await query.answer("💍 Поздравляем!")
            await query.edit_message_text(
                f"💒 <b>СВАДЬБА!</b>\n\n"
                f"💑 {mention(p_id, pname)} ❤️ {mention(t_id, who.first_name)}\n\n"
                f"🎊 Поздравляем! Бонус к /daily активирован!",
                parse_mode=ParseMode.HTML,
            )
        else:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM proposals WHERE target_id=? AND chat_id=?",
                                 (t_id, cid))
                await db.commit()
            await query.answer("💔 Отклонено")
            await query.edit_message_text(
                f"💔 {mention(t_id, who.first_name)} отклонил(а) предложение от {mention(p_id, pname)}",
                parse_mode=ParseMode.HTML,
            )
        return

    # ── Duel ────────────────────────────────────────────
    if data.startswith("da:") or data.startswith("dd:"):
        parts  = data.split(":")
        action = parts[0]
        c_id   = int(parts[1])
        o_id   = int(parts[2])
        key    = f"{cid}:{c_id}:{o_id}"

        if who.id != o_id:
            await query.answer("❌ Вызов не для тебя!", show_alert=True)
            return
        if key not in duel_challenges:
            await query.answer("❌ Вызов уже неактуален", show_alert=True)
            await query.edit_message_reply_markup(None)
            return

        challenge = duel_challenges.pop(key)

        if action == "dd":
            await query.answer("🏳️ Ты отказался")
            await query.edit_message_text(
                f"🏳️ {mention(o_id, who.first_name)} отказался от дуэли!\n"
                f"{mention(c_id, challenge['c_name'])} остаётся непобеждённым.",
                parse_mode=ParseMode.HTML,
            )
            return

        # Check VRF
        cu = await db_get_user(c_id, cid)
        ou = await db_get_user(o_id, cid)
        bet = challenge["bet"]
        if not cu or cu["vrf"] < bet:
            await query.answer("❌ У вызывающего недостаточно VRF!", show_alert=True)
            return
        if not ou or ou["vrf"] < bet:
            await query.answer("❌ У тебя недостаточно VRF!", show_alert=True)
            return

        await query.answer("⚔️ Принято!")
        await query.edit_message_text(
            f"⚔️ <b>ДУЭЛЬ ПРИНЯТА!</b>\n"
            f"{mention(c_id, challenge['c_name'])} ⚔️ {mention(o_id, who.first_name)}\n"
            f"💰 Ставка: {bet} VRF · 🎲 Бросаем...",
            parse_mode=ParseMode.HTML,
        )
        context.application.create_task(_run_duel(context, challenge))
        return

    # ── Cubes join ──────────────────────────────────────
    if data.startswith("cj:") or data.startswith("cd:"):
        game_id = data[3:]
        game    = cubes_games.get(game_id)

        if not game:
            await query.answer("❌ Игра не найдена", show_alert=True)
            return
        if game["state"] != "waiting":
            await query.answer("❌ Игра уже началась", show_alert=True)
            return

        if data.startswith("cd:"):
            if who.id != game["opp_id"]:
                await query.answer("❌ Ты не соперник!", show_alert=True)
                return
            del cubes_games[game_id]
            await query.answer("❌ Отказано")
            await query.edit_message_text(
                f"❌ {mention(who.id, who.first_name)} отказался от игры в кости.",
                parse_mode=ParseMode.HTML,
            )
            return

        if who.id != game["opp_id"]:
            await query.answer("❌ Ты не соперник!", show_alert=True)
            return

        bet = game["bet"]
        hu  = await db_get_user(game["host_id"], cid)
        ou  = await db_get_user(who.id, cid)
        if not hu or hu["vrf"] < bet:
            await query.answer("❌ У хоста недостаточно VRF!", show_alert=True)
            return
        if not ou or ou["vrf"] < bet:
            await query.answer("❌ У тебя недостаточно VRF!", show_alert=True)
            return

        game["state"] = "playing"
        await query.answer("🎲 Поехали!")
        await query.edit_message_text(
            f"🎲 <b>Игра началась!</b>\n"
            f"{mention(game['host_id'], game['host_name'])} ⚔️ {mention(who.id, who.first_name)}\n"
            f"Раундов: {game['rounds']} | Ставка: {bet} VRF",
            parse_mode=ParseMode.HTML,
        )
        context.application.create_task(_run_cubes(context, game))
        return

    # ── Sports join ─────────────────────────────────────
    if data.startswith("sj:") or data.startswith("sd:"):
        game_id = data[3:]
        game    = sports_games.get(game_id)

        if not game:
            await query.answer("❌ Игра не найдена", show_alert=True)
            return
        if game["state"] != "waiting":
            await query.answer("❌ Игра уже началась", show_alert=True)
            return

        if data.startswith("sd:"):
            if who.id != game["opp_id"]:
                await query.answer("❌ Ты не соперник!", show_alert=True)
                return
            del sports_games[game_id]
            await query.answer("❌ Отказано")
            await query.edit_message_text(
                f"❌ {mention(who.id, who.first_name)} отказался от вызова.",
                parse_mode=ParseMode.HTML,
            )
            return

        if who.id != game["opp_id"]:
            await query.answer("❌ Ты не соперник!", show_alert=True)
            return

        bet = game["bet"]
        hu  = await db_get_user(game["host_id"], cid)
        ou  = await db_get_user(who.id, cid)
        if not hu or hu["vrf"] < bet:
            await query.answer("❌ У хоста недостаточно VRF!", show_alert=True)
            return
        if not ou or ou["vrf"] < bet:
            await query.answer("❌ У тебя недостаточно VRF!", show_alert=True)
            return

        game["state"] = "playing"
        emoji = SPORT_EMOJI[game["type"]]
        await query.answer(f"{emoji} Поехали!")
        await query.edit_message_text(
            f"{emoji} <b>Игра началась!</b>\n"
            f"{mention(game['host_id'], game['host_name'])} ⚔️ {mention(who.id, who.first_name)}\n"
            f"Ставка: {bet} VRF",
            parse_mode=ParseMode.HTML,
        )
        context.application.create_task(_run_sports(context, game))
        return

    # ── Slot join ────────────────────────────────────────
    if data.startswith("slj:") or data.startswith("sld:"):
        game_id = data[4:]
        game    = slot_games.get(game_id)

        if not game:
            await query.answer("❌ Игра не найдена", show_alert=True)
            return
        if game["state"] != "waiting":
            await query.answer("❌ Игра уже началась", show_alert=True)
            return

        if data.startswith("sld:"):
            if who.id != game["opp_id"]:
                await query.answer("❌ Ты не соперник!", show_alert=True)
                return
            del slot_games[game_id]
            await query.answer("❌ Отказано")
            await query.edit_message_text("❌ Вызов на слот отклонён.")
            return

        if who.id != game["opp_id"]:
            await query.answer("❌ Ты не соперник!", show_alert=True)
            return

        bet = game["bet"]
        hu  = await db_get_user(game["host_id"], cid)
        ou  = await db_get_user(who.id, cid)
        if not hu or hu["vrf"] < bet:
            await query.answer("❌ У хоста недостаточно VRF!", show_alert=True)
            return
        if not ou or ou["vrf"] < bet:
            await query.answer("❌ У тебя недостаточно VRF!", show_alert=True)
            return

        game["state"] = "active"
        await query.answer("🎰 Принято!")
        await query.edit_message_text(
            f"🎰 <b>Слот-машина!</b>\n\n"
            f"💎 Ставка: {bet} VRF\n\n"
            f"Нажимайте Крутить! (по одному разу каждый)\n\n"
            f"🕹 {mention(game['host_id'], game['host_name'])}: ожидает...\n"
            f"🕹 {mention(game['opp_id'], game['opp_name'])}: ожидает...",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🎰 Крутить!", callback_data=f"slsp:{game_id}"),
            ]]),
        )
        return

    # ── Slot spin ────────────────────────────────────────
    if data.startswith("slsp:"):
        game_id = data[5:]
        game    = slot_games.get(game_id)

        if not game:
            await query.answer("❌ Игра не найдена", show_alert=True)
            return
        if game["state"] not in ("active",):
            await query.answer("❌ Игра завершена", show_alert=True)
            return

        is_host = who.id == game["host_id"]
        is_opp  = who.id == game["opp_id"]
        if not is_host and not is_opp:
            await query.answer("❌ Ты не участник!", show_alert=True)
            return
        if is_host and game["h_val"] is not None:
            await query.answer("✅ Ты уже крутил(а)!", show_alert=True)
            return
        if is_opp and game["o_val"] is not None:
            await query.answer("✅ Ты уже крутил(а)!", show_alert=True)
            return

        await query.answer("🎰 Крутим!")
        # Bot sends the dice
        dice_msg = await context.bot.send_dice(chat_id=cid, emoji="🎰")
        val = dice_msg.dice.value

        if is_host:
            game["h_val"] = val
        else:
            game["o_val"] = val

        # Check if both spun
        if game["h_val"] is not None and game["o_val"] is not None:
            h_combo, h_mult = parse_slot(game["h_val"])
            o_combo, o_mult = parse_slot(game["o_val"])
            bet = game["bet"]
            h_id, h_name = game["host_id"], game["host_name"]
            o_id, o_name = game["opp_id"],  game["opp_name"]

            del slot_games[game_id]

            if h_mult > o_mult:
                w_id, w_name, l_id = h_id, h_name, o_id
            elif o_mult > h_mult:
                w_id, w_name, l_id = o_id, o_name, h_id
            else:
                await db_record_game(h_id, cid, won=False, draw=True)
                await db_record_game(o_id, cid, won=False, draw=True)
                await context.bot.send_message(cid,
                    f"🤝 <b>НИЧЬЯ в слоте!</b>\n\n"
                    f"{mention(h_id, h_name)}: {h_combo} ({h_mult}x)\n"
                    f"{mention(o_id, o_name)}: {o_combo} ({o_mult}x)\n\n"
                    f"Ставки возвращены!",
                    parse_mode=ParseMode.HTML)
                return

            await db_deduct_vrf(l_id, cid, bet)
            new_bal = await db_add_vrf(w_id, cid, bet)
            await db_add_xp(w_id, cid, XP_PER_WIN)
            await db_add_xp(l_id, cid, XP_PER_GAME)
            await db_record_game(w_id, cid, won=True)
            await db_record_game(l_id, cid, won=False)

            await send_rich(context.bot, cid,
                html=(
                    f"<h3>🎰 Слот &mdash; Результат</h3>"
                    f"<table bordered>"
                    f"<tr><th>Игрок</th><th align=\"center\">Комбо</th><th align=\"right\">Множитель</th></tr>"
                    f"<tr><td>{'<b>' if h_mult > o_mult else ''}{h_name}{'</b>' if h_mult > o_mult else ''}</td>"
                    f"<td align=\"center\">{h_combo}</td><td align=\"right\">{h_mult}×</td></tr>"
                    f"<tr><td>{'<b>' if o_mult > h_mult else ''}{o_name}{'</b>' if o_mult > h_mult else ''}</td>"
                    f"<td align=\"center\">{o_combo}</td><td align=\"right\">{o_mult}×</td></tr>"
                    f"</table>"
                    f"<blockquote>{E_WIN1} Победитель: <b>{w_name}</b><br/>💎 +{fmt(bet)} VRF &rarr; {fmt(new_bal)} VRF</blockquote>"
                ),
                fallback_html=f"{E_WIN1} <b>СЛОТ</b>\n{h_name}: {h_combo} ({h_mult}×)\n{o_name}: {o_combo} ({o_mult}×)\n\n🏆 {w_name} +{fmt(bet)} VRF")
        else:
            # One player has spun, update message
            h_status = f"✅ {parse_slot(game['h_val'])[0]}" if game["h_val"] else f"{E_WAIT} ожидает..."
            o_status = f"✅ {parse_slot(game['o_val'])[0]}" if game["o_val"] else f"{E_WAIT} ожидает..."
            try:
                await query.edit_message_text(
                    f"🎰 <b>Слот-машина!</b>\n\n"
                    f"💎 Ставка: {game['bet']} VRF\n\n"
                    f"🕹 {mention(game['host_id'], game['host_name'])}: {h_status}\n"
                    f"🕹 {mention(game['opp_id'], game['opp_name'])}: {o_status}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🎰 Крутить!", callback_data=f"slsp:{game_id}"),
                    ]]),
                )
            except TelegramError:
                pass
        return

    # ── Mines Game ───────────────────────────────────────
    if data.startswith("mg:"):
        parts  = data.split(":")
        action = parts[1]

        if action == "noop":
            await query.answer()
            return

        if action == "cancel":
            await query.answer("Отменено")
            try:
                await query.message.delete()
            except TelegramError:
                pass
            return

        if action == "new":
            # Show bet selection again
            await query.answer()
            uu = await db_get_user(who.id, cid)
            bal = uu["vrf"] if uu else 0
            await query.edit_message_text(
                f"💣 <b>Мины</b>\n\n"
                f"💎 Баланс: <b>{fmt(bal)} VRF</b>\n\n"
                f"Выбери ставку:",
                parse_mode=ParseMode.HTML,
                reply_markup=_mines_bet_kb(who.id, cid),
            )
            return

        if action == "b":  # bet selected → choose mines count
            uid2 = int(parts[2])
            cid2 = int(parts[3])
            bet  = int(parts[4])
            if who.id != uid2:
                await query.answer("❌ Это не твоя кнопка!", show_alert=True)
                return
            uu = await db_get_user(uid2, cid2)
            if not uu or uu["vrf"] < bet:
                await query.answer(f"❌ Нужно {bet} VRF, у тебя {uu['vrf'] if uu else 0}", show_alert=True)
                return
            await query.answer()
            # Build mines count buttons with multiplier hints
            hint_rows = []
            for mc in [3, 5, 10, 15]:
                m1  = calc_mines_mult(1,  mc)
                m5  = calc_mines_mult(5,  mc)
                m10 = calc_mines_mult(10, mc)
                hint_rows.append(
                    f"  💣 <b>{mc} мин</b> → 1-й: {m1}×  5-й: {m5}×  10-й: {m10}×"
                )
            mines_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"💣 {mc} мин", callback_data=f"mg:mc:{uid2}:{cid2}:{mc}:{bet}")
                 for mc in [3, 5]],
                [InlineKeyboardButton(f"💣 {mc} мин", callback_data=f"mg:mc:{uid2}:{cid2}:{mc}:{bet}")
                 for mc in [10, 15]],
                [InlineKeyboardButton("◀️ Назад", callback_data="mg:new")],
            ])
            await query.edit_message_text(
                f"💣 <b>Мины</b>  ·  Ставка: <b>{bet} VRF</b>\n\n"
                f"Выбери количество мин:\n"
                f"(больше мин = выше риск = выше множитель)\n\n"
                + "\n".join(hint_rows),
                parse_mode=ParseMode.HTML,
                reply_markup=mines_kb,
            )
            return

        if action == "mc":  # mines count chosen → start game
            uid2 = int(parts[2])
            cid2 = int(parts[3])
            mc   = int(parts[4])
            bet  = int(parts[5])
            if who.id != uid2:
                await query.answer("❌ Это не твоя кнопка!", show_alert=True)
                return
            key = f"{uid2}:{cid2}"
            if key in mines_games and mines_games[key]["state"] == "active":
                await query.answer("❌ У тебя уже есть активная игра!", show_alert=True)
                return
            if not await db_deduct_vrf(uid2, cid2, bet):
                await query.answer("❌ Недостаточно VRF!", show_alert=True)
                return
            # Generate grid
            mine_pos = set(random.sample(range(MINES_TOTAL), mc))
            mines_games[key] = {
                "user_id": uid2, "cid": cid2, "bet": bet, "mines_count": mc,
                "grid":     [i in mine_pos for i in range(MINES_TOTAL)],
                "revealed": [False] * MINES_TOTAL,
                "safe_revealed": 0, "state": "active",
            }
            await query.answer("🎮 Игра началась!")
            await query.edit_message_text(
                _mines_header(mines_games[key]),
                parse_mode=ParseMode.HTML,
                reply_markup=_mines_grid_kb(uid2, cid2, mines_games[key]),
            )
            return

        if action == "c":  # cell click
            uid2 = int(parts[2])
            cid2 = int(parts[3])
            idx  = int(parts[4])
            if who.id != uid2:
                await query.answer("❌ Это не твоя игра!", show_alert=True)
                return
            key  = f"{uid2}:{cid2}"
            game = mines_games.get(key)
            if not game or game["state"] != "active":
                await query.answer("❌ Игра не найдена или завершена", show_alert=True)
                return
            if game["revealed"][idx]:
                await query.answer("Уже открыто!", show_alert=True)
                return
            game["revealed"][idx] = True

            if game["grid"][idx]:  # 💣 BOMB
                game["state"] = "lost"
                del mines_games[key]
                await db_add_xp(uid2, cid2, XP_PER_GAME)
                await db_record_game(uid2, cid2, won=False)
                await query.answer("💥 БУМ!", show_alert=True)
                await query.edit_message_text(
                    f"<h2>{E_BOOM} БУМ! Мина!</h2>"
                    f"<table bordered>"
                    f"<tr><td>💎 Ставка</td><td align=\"right\"><s>{fmt(game['bet'])} VRF</s></td></tr>"
                    f"<tr><td>✅ Успел открыть</td><td align=\"right\"><b>{game['safe_revealed']}</b> клеток</td></tr>"
                    f"<tr><td>💣 Мин на поле</td><td align=\"right\"><b>{game['mines_count']}</b></td></tr>"
                    f"</table>"
                    f"<blockquote>Ставка <b>{fmt(game['bet'])} VRF</b> потеряна 😢</blockquote>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=_mines_dead_kb(game, boom_idx=idx),
                )
            else:  # 💎 SAFE
                game["safe_revealed"] += 1
                safe_total = MINES_TOTAL - game["mines_count"]
                if game["safe_revealed"] == safe_total:  # All safe cells found!
                    game["state"] = "won"
                    mult   = calc_mines_mult(game["safe_revealed"], game["mines_count"])
                    payout = int(game["bet"] * mult)
                    del mines_games[key]
                    new_bal = await db_add_vrf(uid2, cid2, payout)
                    await db_add_xp(uid2, cid2, XP_PER_WIN)
                    await db_record_game(uid2, cid2, won=True)
                    await query.answer("🏆 Идеальная игра!", show_alert=True)
                    await query.edit_message_text(
                        f"🏆 <b>ИДЕАЛЬНО! Все клетки открыты!</b>\n\n"
                        f"💎 Ставка: <b>{fmt(game['bet'])} VRF</b>\n"
                        f"⚡ Множитель: <b>{mult}×</b>\n"
                        f"🏆 Выигрыш: <b>{fmt(payout)} VRF</b>\n"
                        f"💰 Баланс: <b>{fmt(new_bal)} VRF</b>",
                        parse_mode=ParseMode.HTML,
                        reply_markup=_mines_dead_kb(game),
                    )
                else:
                    mult = calc_mines_mult(game["safe_revealed"], game["mines_count"])
                    await query.answer(f"💎 Безопасно! Множитель: {mult}×")
                    await query.edit_message_text(
                        _mines_header(game),
                        parse_mode=ParseMode.HTML,
                        reply_markup=_mines_grid_kb(uid2, cid2, game),
                    )
            return

        if action == "co":  # cash out
            uid2 = int(parts[2])
            cid2 = int(parts[3])
            if who.id != uid2:
                await query.answer("❌ Это не твоя игра!", show_alert=True)
                return
            key  = f"{uid2}:{cid2}"
            game = mines_games.get(key)
            if not game or game["state"] != "active":
                await query.answer("❌ Игра не найдена или завершена", show_alert=True)
                return
            if game["safe_revealed"] == 0:
                await query.answer("❌ Сначала открой хотя бы одну клетку!", show_alert=True)
                return
            mult   = calc_mines_mult(game["safe_revealed"], game["mines_count"])
            payout = int(game["bet"] * mult)
            profit = payout - game["bet"]
            game["state"] = "won"
            del mines_games[key]
            new_bal = await db_add_vrf(uid2, cid2, payout)
            await db_add_xp(uid2, cid2, XP_PER_WIN)
            await db_record_game(uid2, cid2, won=True)
            await query.answer(f"💸 Забрал {fmt(payout)} VRF!", show_alert=True)
            await query.edit_message_text(
                f"<h3>💸 Выигрыш в Минах!</h3>"
                f"<table bordered striped>"
                f"<tr><td>💎 Ставка</td><td align=\"right\"><b>{fmt(game['bet'])} VRF</b></td></tr>"
                f"<tr><td>✅ Открыто</td><td align=\"right\"><b>{game['safe_revealed']}</b> клеток</td></tr>"
                f"<tr><td>⚡ Множитель</td><td align=\"right\"><b>{mult}×</b></td></tr>"
                f"<tr><td>🏆 Получено</td><td align=\"right\"><b>{fmt(payout)} VRF</b>"
                + (f" <mark>+{fmt(profit)}</mark>" if profit > 0 else "") +
                f"</td></tr>"
                f"<tr><td>💰 Баланс</td><td align=\"right\"><b>{fmt(new_bal)} VRF</b></td></tr>"
                f"</table>",
                parse_mode=ParseMode.HTML,
                reply_markup=_mines_dead_kb(game),
            )
            return

        if action == "q":  # quit → lose bet
            uid2 = int(parts[2])
            cid2 = int(parts[3])
            if who.id != uid2:
                await query.answer("❌ Это не твоя игра!", show_alert=True)
                return
            key  = f"{uid2}:{cid2}"
            game = mines_games.get(key)
            if not game or game["state"] != "active":
                await query.answer("❌ Игра не найдена", show_alert=True)
                return
            game["state"] = "quit"
            del mines_games[key]
            await db_record_game(uid2, cid2, won=False)
            await query.answer("🏳 Сдался")
            await query.edit_message_text(
                f"🏳 <b>Игра прекращена</b>\n\n"
                f"💎 Ставка <b>{fmt(game['bet'])} VRF</b> потеряна\n"
                f"✅ Было открыто: <b>{game['safe_revealed']}</b> клеток",
                parse_mode=ParseMode.HTML,
                reply_markup=_mines_dead_kb(game),
            )
            return

        await query.answer()
        return

    # ── Shop 🛍️ ─────────────────────────────────────────
    if data.startswith("shop:"):
        parts  = data.split(":")
        action = parts[1]

        if action in ("noop", "close"):
            await query.answer()
            if action == "close":
                try:
                    await query.message.delete()
                except TelegramError:
                    pass
            return

        if action == "buy":
            item_key  = parts[2]
            uid2, cid2 = int(parts[3]), int(parts[4])
            if who.id != uid2:
                await query.answer("❌ Это не твой магазин!", show_alert=True)
                return
            item = SHOP_ITEMS.get(item_key)
            if not item:
                await query.answer("❌ Предмет не найден", show_alert=True)
                return
            uu = await db_get_user(uid2, cid2)
            if not uu or uu["vrf"] < item["vrf"]:
                await query.answer(
                    f"❌ Нужно {item['vrf']} VRF, у тебя {uu['vrf'] if uu else 0}",
                    show_alert=True,
                )
                return

            if item_key == "hint":
                key = f"{uid2}:{cid2}"
                game = mines_games.get(key)
                if not game or game["state"] != "active":
                    await query.answer("❌ Нет активной игры в Мины!", show_alert=True)
                    return
                safe_cells = [i for i in range(MINES_TOTAL)
                              if not game["revealed"][i] and not game["grid"][i]]
                if not safe_cells:
                    await query.answer("❌ Нет скрытых безопасных клеток!", show_alert=True)
                    return
                if not await db_deduct_vrf(uid2, cid2, item["vrf"]):
                    await query.answer("❌ Недостаточно VRF!", show_alert=True)
                    return
                cell = random.choice(safe_cells)
                game["revealed"][cell] = True
                game["safe_revealed"] += 1
                await query.answer("💡 Безопасная клетка открыта!", show_alert=True)
                try:
                    await query.edit_message_text(
                        _mines_header(game), parse_mode=ParseMode.HTML,
                        reply_markup=_mines_grid_kb(uid2, cid2, game),
                    )
                except TelegramError:
                    pass
                return

            if item_key == "boost" and await db_has_boost(uid2, cid2, "daily_boost"):
                await query.answer("❌ Мега-бонус уже активен!", show_alert=True)
                return

            if not await db_deduct_vrf(uid2, cid2, item["vrf"]):
                await query.answer("❌ Недостаточно VRF!", show_alert=True)
                return

            if item_key == "bear":
                async with aiosqlite.connect(DB_PATH) as _db:
                    await _db.execute(
                        "UPDATE users SET bears=bears+1 WHERE user_id=? AND chat_id=?",
                        (uid2, cid2),
                    )
                    await _db.commit()
                await query.answer(f"✅ Медведь куплен! {E_BEAR}")
            elif item_key == "boost":
                await db_set_boost(uid2, cid2, "daily_boost", hours=24)
                await query.answer("✅ Мега-бонус ×2 активирован на 24ч! ⚡")

            uu2      = await db_get_user(uid2, cid2)
            hb       = await db_has_boost(uid2, cid2, "daily_boost")
            try:
                await query.message.delete()
            except TelegramError:
                pass
            await send_rich(
                context.bot, cid2,
                html=_shop_rich_html(uu2["vrf"] if uu2 else 0, hb),
                fallback_html=_shop_text(uu2["vrf"] if uu2 else 0, hb),
                reply_markup=_shop_kb(uid2, cid2),
            )
            return

        if action == "stars":
            stars_count = int(parts[2])
            uid2, cid2  = int(parts[3]), int(parts[4])
            if who.id != uid2:
                await query.answer("❌ Это не твой магазин!", show_alert=True)
                return
            pkg = next((p for p in STARS_PACKAGES if p[0] == stars_count), None)
            if not pkg:
                await query.answer("❌ Пакет не найден", show_alert=True)
                return
            s, base, bonus = pkg
            total = base + bonus
            await query.answer(f"⭐ Открываю инвойс на {s} Stars…")
            try:
                await context.bot.send_invoice(
                    chat_id=cid2,          # ← Current chat (group or private)
                    title=f"💎 {total} VRF",
                    description=(
                        f"{s} ⭐ Telegram Stars → {total} 💎 VRF\n"
                        f"для Verifure Game"
                        + (f" (+{bonus} бонус!)" if bonus else "")
                    ),
                    payload=f"stars_{total}_{uid2}_{cid2}",
                    currency="XTR",
                    prices=[LabeledPrice(f"{total} VRF", s)],
                )
            except TelegramError as e:
                await context.bot.send_message(
                    cid2,
                    f"❌ Ошибка инвойса: <code>{e}</code>\n\n"
                    f"Попробуй написать боту в <b>личку</b> и повтори /shop там.",
                    parse_mode=ParseMode.HTML,
                )
            return

        await query.answer()
        return

    # ── Admin panel ──────────────────────────────────────
    if data.startswith("ap:"):
        uid   = who.id
        is_adm = await is_bot_admin(uid)
        if not is_adm:
            try:
                member = await query.message.chat.get_member(uid)
                is_adm = member.status in ("administrator", "creator")
            except TelegramError:
                pass
        if not is_adm:
            await query.answer("❌ Нет доступа", show_alert=True)
            return

        action  = data[3:]
        back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="ap:back")]])

        if action == "back":
            await query.answer()
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 Статистика",   callback_data="ap:stats"),
                 InlineKeyboardButton("🏆 Топ VRF",     callback_data="ap:top")],
                [InlineKeyboardButton("💑 Все браки",    callback_data="ap:marriages"),
                 InlineKeyboardButton("👮 Бот-админы",  callback_data="ap:admins")],
                [InlineKeyboardButton("📋 Все команды",  callback_data="ap:cmds"),
                 InlineKeyboardButton("ℹ️ Управление",  callback_data="ap:manage")],
                [InlineKeyboardButton("❌ Закрыть",      callback_data="ap:close")],
            ])
            await query.edit_message_text(
                f"🛡️ <b>Verifure Admin Panel</b>\n\n{E_ALERT} Выбери раздел:",
                parse_mode=ParseMode.HTML, reply_markup=kb,
            )

        elif action == "close":
            await query.answer("Закрыто")
            await query.message.delete()

        elif action == "stats":
            await query.answer()
            total = await db_count_users(cid)
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT COUNT(*) FROM marriages WHERE chat_id=?", (cid,)) as cur:
                    marriages = (await cur.fetchone())[0]
                async with db.execute("SELECT SUM(total_games),SUM(vrf),SUM(wins) FROM users WHERE chat_id=?", (cid,)) as cur:
                    row = await cur.fetchone()
                    games, vrf, wins = row[0] or 0, row[1] or 0, row[2] or 0
            await query.edit_message_text(
                f"📊 <b>Статистика чата</b>\n\n"
                f"👥 Игроков: <b>{total}</b>\n"
                f"🎮 Сыграно: <b>{fmt(games)}</b>\n"
                f"🏆 Побед: <b>{fmt(wins)}</b>\n"
                f"💎 VRF в обороте: <b>{fmt(vrf)}</b>\n"
                f"💒 Браков: <b>{marriages}</b>",
                parse_mode=ParseMode.HTML, reply_markup=back_kb,
            )

        elif action == "top":
            await query.answer()
            users = await db_top(cid, "vrf", 10)
            lines = ["💎 <b>Топ-10 VRF</b>\n"]
            for i, u in enumerate(users):
                medal = MEDALS[i] if i < len(MEDALS) else f"{i+1}."
                lines.append(
                    f"{medal} {mention(u['user_id'], u['first_name'])} — {fmt(u['vrf'])} VRF"
                    f" · {u['wins']}W/{u['losses']}L"
                )
            await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=back_kb)

        elif action == "marriages":
            await query.answer()
            all_m = await db_all_marriages(cid)
            lines = [f"💑 <b>Все браки ({len(all_m)})</b>\n"]
            for i, m in enumerate(all_m[:10]):
                u1 = await db_get_user(m["user1_id"], cid)
                u2 = await db_get_user(m["user2_id"], cid)
                n1 = u1["first_name"] if u1 else "?"
                n2 = u2["first_name"] if u2 else "?"
                lines.append(f"{i+1}. {n1} ❤️ {n2} — {days_ago(m['married_at'])} дн.")
            await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=back_kb)

        elif action == "admins":
            await query.answer()
            admins = await db_list_admins()
            lines  = ["👮 <b>Бот-администраторы</b>\n"]
            for a in admins:
                uname = f" @{a['username']}" if a["username"] else ""
                lines.append(f"• {a['first_name']}{uname}")
            if ADMIN_IDS:
                lines.append(f"\n🔧 Env: {', '.join(map(str, ADMIN_IDS))}")
            await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=back_kb)

        elif action == "cmds":
            await query.answer()
            await query.edit_message_text(
                "📋 <b>Все команды</b>\n\n"
                "<b>Игроки:</b>\n"
                "/start /help /profile /top /stats /daily /bonus\n"
                "/marry /accept /reject /divorce /marriage /marriages\n"
                "/duel /cubes /basket /football /bowling /darts /slot\n"
                "/gift /love\n\n"
                "<b>Администраторы:</b>\n"
                "/admin /givevrf /takevrf /givebear\n"
                "/addadmin /removeadmin /listadmins",
                parse_mode=ParseMode.HTML, reply_markup=back_kb,
            )

        elif action == "manage":
            await query.answer()
            await query.edit_message_text(
                "ℹ️ <b>Управление игроками</b>\n\n"
                "/givevrf &lt;n&gt; — выдать VRF (ответом)\n"
                "/takevrf &lt;n&gt; — забрать VRF (ответом)\n"
                "/givebear — выдать медведя 🐻 (ответом)\n"
                "/addadmin — сделать бот-админом (ответом)\n"
                "/removeadmin — убрать бот-админа (ответом)",
                parse_mode=ParseMode.HTML, reply_markup=back_kb,
            )
        return

    await query.answer()


# ══════════════════════════════════════════════════════
#           MESSAGE HANDLER (XP from chat)
# ══════════════════════════════════════════════════════

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if update.message and update.message.successful_payment:
        await handle_stars_payment(update.message)
        return

    if update.effective_chat.type == "private":
        return
    u = update.effective_user
    if u.is_bot:
        return

    cid = update.effective_chat.id
    await db_ensure_user(u.id, cid, u.username or "", u.first_name)

    if not await db_can_earn_xp(u.id, cid):
        return

    xp = random.randint(XP_PER_MSG_MIN, XP_PER_MSG_MAX)
    m  = await db_get_marriage(u.id, cid)
    if m:
        xp = int(xp * 1.1)

    new_lvl, leveled_up = await db_add_xp(u.id, cid, xp)

    if leveled_up:
        rank_nm = get_rank(new_lvl)
        if new_lvl in MILESTONES:
            text = (
                f"{E_ALERT} <b>ОСОБЫЙ РУБЕЖ!</b>\n\n"
                f"{mention(u.id, u.first_name)} — <b>{new_lvl} уровень!</b>\n{rank_nm}\n\n"
                f"🏆 Поздравляем!"
            )
        else:
            tpls = [
                f"🎉 {mention(u.id, u.first_name)} — <b>уровень {new_lvl}!</b> {rank_nm}",
                f"⬆️ Новый уровень у {mention(u.id, u.first_name)}: <b>{new_lvl}!</b> {rank_nm}",
            ]
            text = random.choice(tpls)
        try:
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
            await _react(update, "🎉")
        except TelegramError:
            pass


# ══════════════════════════════════════════════════════
#                       MAIN
# ══════════════════════════════════════════════════════

async def on_startup(app: Application) -> None:
    await db_init()
    from telegram import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeDefault
    cmds = [
        BotCommand("start",    "🏠 Старт / Главное меню"),
        BotCommand("profile",  "👤 Мой профиль"),
        BotCommand("top",      "🏆 Топ игроков"),
        BotCommand("stats",    "📊 Статистика чата"),
        BotCommand("daily",    "⚡ Ежедневный бонус"),
        BotCommand("bonus",    "📋 Статус бонусов"),
        BotCommand("gift",     "🎁 Подарить VRF (ответом)"),
        BotCommand("love",     "💝 Любовь (ответом)"),
        BotCommand("duel",     "⚔️ Дуэль (ответом)"),
        BotCommand("cubes",    "🎲 Кубики (ответом)"),
        BotCommand("basket",   "🏀 Баскетбол (ответом)"),
        BotCommand("football", "⚽ Футбол (ответом)"),
        BotCommand("bowling",  "🎳 Боулинг (ответом)"),
        BotCommand("darts",    "🎯 Дартс (ответом)"),
        BotCommand("slot",     "🎰 Слот PvP (ответом)"),
        BotCommand("mines",    "💣 Мины — соло"),
        BotCommand("shop",     "🛍️ Магазин — VRF и Stars"),
        BotCommand("marry",    "💒 Предложение"),
        BotCommand("marriage", "💑 Карточка брака"),
        BotCommand("marriages","👫 Все пары"),
        BotCommand("divorce",  "💔 Развод"),
        BotCommand("keyboard", "⌨️ Показать клавиатуру"),
        BotCommand("help",     "ℹ️ Помощь"),
    ]
    try:
        await app.bot.set_my_commands(cmds, scope=BotCommandScopeDefault())
        await app.bot.set_my_commands(cmds, scope=BotCommandScopeAllGroupChats())
    except Exception:
        pass
    log.info("Verifure Game 10.1 is online!")


def main() -> None:
    if not BOT_TOKEN:
        log.critical("BOT_TOKEN environment variable is not set!")
        raise SystemExit(1)

    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()

    # Core
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))

    # Profile
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("top",     cmd_top))
    app.add_handler(CommandHandler(["leaderboard", "lb"], cmd_top))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("daily",   cmd_daily))
    app.add_handler(CommandHandler("bonus",   cmd_bonus))

    # Marriage
    app.add_handler(CommandHandler("marry",    cmd_marry))
    app.add_handler(CommandHandler("accept",   cmd_accept))
    app.add_handler(CommandHandler("reject",   cmd_reject))
    app.add_handler(CommandHandler("divorce",  cmd_divorce))
    app.add_handler(CommandHandler("marriage", cmd_marriage))
    app.add_handler(CommandHandler("marriages",cmd_marriages))

    # Social
    app.add_handler(CommandHandler("gift",    cmd_gift))
    app.add_handler(CommandHandler("love",    cmd_love))

    # Games
    app.add_handler(CommandHandler("duel",    cmd_duel))
    app.add_handler(CommandHandler("cubes",   cmd_cubes))
    app.add_handler(CommandHandler("basket",  cmd_basket))
    app.add_handler(CommandHandler("football",cmd_football))
    app.add_handler(CommandHandler("bowling", cmd_bowling))
    app.add_handler(CommandHandler("darts",   cmd_darts))
    app.add_handler(CommandHandler("slot",    cmd_slot))
    app.add_handler(CommandHandler("mines",   cmd_mines))
    app.add_handler(CommandHandler("shop",    cmd_shop))
    app.add_handler(PreCheckoutQueryHandler(handle_pre_checkout))

    # Admin
    app.add_handler(CommandHandler("admin",        cmd_admin))
    app.add_handler(CommandHandler("givevrf",      cmd_givevrf))
    app.add_handler(CommandHandler("takevrf",      cmd_takevrf))
    app.add_handler(CommandHandler("givebear",     cmd_givebear))
    app.add_handler(CommandHandler("addadmin",     cmd_addadmin))
    app.add_handler(CommandHandler("removeadmin",  cmd_removeadmin))
    app.add_handler(CommandHandler("listadmins",   cmd_listadmins))

    # Callbacks & messages
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("Starting polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
