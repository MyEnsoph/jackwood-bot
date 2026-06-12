"""
Jackwood Stats Bot
Бот для ведения статистики игр Jackwood в Telegram-группе.
Хранение данных: Google Sheets через gspread.

Зависимости:
    pip install python-telegram-bot==20.7 gspread google-auth

Запуск:
    python jackwood_bot.py
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMemberUpdated
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    ChatMemberHandler,
    filters,
)

# ─────────────────────────────────────────────
# НАСТРОЙКИ — заполните перед запуском
# ─────────────────────────────────────────────
BOT_TOKEN = "8713385796:AAEQ0w-c1wvz8icqDHsgo7vLzWB_QVwb4iI"          # токен от @BotFather
SPREADSHEET_ID = "15K4hsMnVormFrVCP8q0BT_NK7BuBk10dEgNNMWST2vk"    # ID Google Sheets таблицы
CREDENTIALS_FILE = "credentials.json" # путь к JSON-ключу сервисного аккаунта
# GROUP_CHAT_ID вводить не нужно — бот запомнит группу сам при добавлении
# ─────────────────────────────────────────────

GROUP_ID_FILE = "group_id.txt"  # файл, где хранится ID группы

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Состояния ConversationHandler
SELECT_PLAYERS, SELECT_WINNERS, CONFIRM_GAME = range(3)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


# ═══════════════════════════════════════════════
# Сохранение/загрузка ID группы
# ═══════════════════════════════════════════════

def save_group_id(chat_id: int):
    with open(GROUP_ID_FILE, "w") as f:
        f.write(str(chat_id))
    logger.info(f"Группа сохранена: {chat_id}")


def load_group_id() -> Optional[int]:
    if os.path.exists(GROUP_ID_FILE):
        with open(GROUP_ID_FILE) as f:
            try:
                return int(f.read().strip())
            except ValueError:
                return None
    return None


async def on_bot_added_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Срабатывает когда бота добавляют в группу — запоминает ID группы."""
    result = update.my_chat_member
    if not result:
        return
    chat = result.chat
    new_status = result.new_chat_member.status
    if chat.type in ("group", "supergroup") and new_status in ("member", "administrator"):
        existing = load_group_id()
        if not existing:
            save_group_id(chat.id)
            await context.bot.send_message(
                chat.id,
                f"👋 Привет! Я запомнил эту группу и готов вести статистику Jackwood.\n"
                f"Напишите /start чтобы узнать команды."
            )
        elif existing != chat.id:
            await context.bot.send_message(
                chat.id,
                "⚠️ Я уже привязан к другой группе. Удалите файл group_id.txt и добавьте меня снова."
            )


# ═══════════════════════════════════════════════
# Google Sheets helpers
# ═══════════════════════════════════════════════

def get_sheet():
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)


def ensure_sheets(spreadsheet):
    existing = [ws.title for ws in spreadsheet.worksheets()]
    if "sessions" not in existing:
        ws = spreadsheet.add_worksheet("sessions", rows=1000, cols=5)
        ws.append_row(["session_id", "date", "status", "started_by"])
    if "games" not in existing:
        ws = spreadsheet.add_worksheet("games", rows=5000, cols=10)
        ws.append_row(["game_id", "session_id", "date", "players", "winners", "player_count"])
    return spreadsheet


def get_active_session(spreadsheet) -> Optional[str]:
    ws = spreadsheet.worksheet("sessions")
    for row in ws.get_all_records():
        if row["status"] == "active":
            return str(row["session_id"])
    return None


def open_session(spreadsheet, started_by: str) -> str:
    ws = spreadsheet.worksheet("sessions")
    records = ws.get_all_records()
    session_id = str(len(records) + 1)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ws.append_row([session_id, date_str, "active", started_by])
    return session_id


def close_session(spreadsheet, session_id: str):
    ws = spreadsheet.worksheet("sessions")
    for i, row in enumerate(ws.get_all_records(), start=2):
        if str(row["session_id"]) == session_id:
            ws.update_cell(i, 3, "closed")
            return True
    return False


def save_game(spreadsheet, session_id: str, players: list, winners: list):
    ws = spreadsheet.worksheet("games")
    records = ws.get_all_records()
    game_id = str(len(records) + 1)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    ws.append_row([game_id, session_id, date_str, ",".join(players), ",".join(winners), len(players)])
    return game_id


def get_all_games(spreadsheet) -> list:
    return spreadsheet.worksheet("games").get_all_records()


# ═══════════════════════════════════════════════
# Проверка прав администратора
# ═══════════════════════════════════════════════

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Проверяет права пользователя в сохранённой группе.
    Если бот пишут из лички — проверяем через сохранённый group_id.
    Если из группы — проверяем прямо в том чате.
    """
    try:
        user_id = update.effective_user.id
        chat = update.effective_chat

        # Команда написана прямо в группе
        if chat and chat.type in ("group", "supergroup"):
            member = await context.bot.get_chat_member(chat.id, user_id)
            return member.status in ("administrator", "creator")

        # Команда написана в личке — проверяем через сохранённый ID группы
        group_id = load_group_id()
        if not group_id:
            await update.message.reply_text(
                "⚠️ Бот ещё не добавлен в группу. Добавьте его в вашу группу сначала."
            )
            return False
        member = await context.bot.get_chat_member(group_id, user_id)
        return member.status in ("administrator", "creator")

    except Exception as e:
        logger.error(f"is_admin error: {e}")
        return False


async def admin_only(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Эта команда только для администраторов группы.")
        return False
    return True


# ═══════════════════════════════════════════════
# Команды бота
# ═══════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group_id = load_group_id()
    group_status = f"Группа привязана ✅" if group_id else "Группа не привязана ⚠️ — добавьте бота в группу"
    text = (
        "🎲 *Jackwood Stats Bot*\n\n"
        f"_{group_status}_\n\n"
        "Команды для всех:\n"
        "/stats — статистика за текущий месяц\n"
        "/alltime — статистика за всё время\n"
        "/top — топ игроков\n"
        "/player @ник — статистика игрока\n\n"
        "Команды для администраторов:\n"
        "/newsession — начать игровую сессию\n"
        "/addgame — добавить партию\n"
        "/endsession — завершить сессию\n"
        "/cancelsession — отменить текущую сессию\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_new_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context):
        return
    spreadsheet = ensure_sheets(get_sheet())
    started_by = update.effective_user.username or str(update.effective_user.id)
    if get_active_session(spreadsheet):
        await update.message.reply_text(
            "⚠️ Уже есть активная сессия.\n"
            "Завершите её командой /endsession или отмените /cancelsession."
        )
        return
    session_id = open_session(spreadsheet, started_by)
    date_str = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    await update.message.reply_text(
        f"✅ Сессия #{session_id} открыта — {date_str}\n"
        "Добавляйте партии командой /addgame"
    )


async def cmd_end_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context):
        return
    spreadsheet = ensure_sheets(get_sheet())
    session_id = get_active_session(spreadsheet)
    if not session_id:
        await update.message.reply_text("⚠️ Нет активной сессии.")
        return
    games = [g for g in get_all_games(spreadsheet) if str(g["session_id"]) == session_id]
    close_session(spreadsheet, session_id)
    await update.message.reply_text(
        f"🏁 Сессия #{session_id} завершена.\n"
        f"Сыграно партий: {len(games)}\n\n"
        "Посмотреть итоги: /stats"
    )


async def cmd_cancel_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context):
        return
    spreadsheet = ensure_sheets(get_sheet())
    session_id = get_active_session(spreadsheet)
    if not session_id:
        await update.message.reply_text("⚠️ Нет активной сессии.")
        return
    close_session(spreadsheet, session_id)
    await update.message.reply_text(f"🗑 Сессия #{session_id} отменена.")


# ─── /addgame — диалог ─────────────────────────

async def cmd_add_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context):
        return ConversationHandler.END
    spreadsheet = ensure_sheets(get_sheet())
    session_id = get_active_session(spreadsheet)
    if not session_id:
        await update.message.reply_text("⚠️ Нет активной сессии. Сначала: /newsession")
        return ConversationHandler.END
    context.user_data["session_id"] = session_id
    context.user_data["selected_players"] = []
    await update.message.reply_text(
        "👥 Введите @ники игроков через запятую (4, 5 или 6 игроков):\n\n"
        "Пример: @ivan, @maria, @alex, @oleg"
    )
    return SELECT_PLAYERS


async def select_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    players = [p.strip().lstrip("@") for p in text.split(",") if p.strip()]
    if len(players) not in (4, 5, 6):
        await update.message.reply_text(
            f"⚠️ Нужно 4, 5 или 6 игроков. Вы ввели {len(players)}. Попробуйте снова:"
        )
        return SELECT_PLAYERS
    context.user_data["players"] = players
    keyboard = [
        [InlineKeyboardButton(f"@{p}", callback_data=f"winner_{p}")]
        for p in players
    ]
    keyboard.append([InlineKeyboardButton("✅ Готово", callback_data="winners_done")])
    await update.message.reply_text(
        "🏆 Нажмите на победителя(ей), затем «Готово»:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    context.user_data["selected_winners"] = []
    return SELECT_WINNERS


async def toggle_winner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    players = context.user_data["players"]
    selected = context.user_data.get("selected_winners", [])

    if data == "winners_done":
        if not selected:
            await query.answer("Выберите хотя бы одного победителя!", show_alert=True)
            return SELECT_WINNERS
        players_fmt = ", ".join(f"@{p}" for p in players)
        winners_fmt = ", ".join(f"@{w}" for w in selected)
        keyboard = [[
            InlineKeyboardButton("✅ Сохранить", callback_data="confirm_yes"),
            InlineKeyboardButton("❌ Отмена", callback_data="confirm_no"),
        ]]
        await query.edit_message_text(
            f"📋 Подтвердите партию:\n\n"
            f"👥 Игроки ({len(players)}): {players_fmt}\n"
            f"🏆 Победители: {winners_fmt}",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return CONFIRM_GAME

    username = data.replace("winner_", "")
    if username in selected:
        selected.remove(username)
    else:
        selected.append(username)
    context.user_data["selected_winners"] = selected

    keyboard = []
    for p in players:
        mark = "✅ " if p in selected else ""
        keyboard.append([InlineKeyboardButton(f"{mark}@{p}", callback_data=f"winner_{p}")])
    keyboard.append([InlineKeyboardButton("✅ Готово", callback_data="winners_done")])
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_WINNERS


async def confirm_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "confirm_no":
        await query.edit_message_text("❌ Добавление партии отменено.")
        return ConversationHandler.END
    spreadsheet = ensure_sheets(get_sheet())
    players = context.user_data["players"]
    winners = context.user_data["selected_winners"]
    session_id = context.user_data["session_id"]
    game_id = save_game(spreadsheet, session_id, players, winners)
    winners_fmt = ", ".join(f"@{w}" for w in winners)
    await query.edit_message_text(
        f"✅ Партия #{game_id} сохранена!\n"
        f"🏆 Победитель(и): {winners_fmt}\n\n"
        "Добавить ещё: /addgame"
    )
    return ConversationHandler.END


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


# ─── Статистика ────────────────────────────────

def build_stats(games: list, title: str) -> str:
    if not games:
        return f"📊 *{title}*\n\nИгр пока нет."
    player_games = {}
    player_wins = {}
    for game in games:
        players = [p.strip() for p in str(game["players"]).split(",") if p.strip()]
        winners = [w.strip() for w in str(game["winners"]).split(",") if w.strip()]
        for p in players:
            player_games[p] = player_games.get(p, 0) + 1
        for w in winners:
            player_wins[w] = player_wins.get(w, 0) + 1
    all_players = set(player_games) | set(player_wins)
    sorted_players = sorted(
        all_players,
        key=lambda p: (player_wins.get(p, 0), player_wins.get(p, 0) / max(player_games.get(p, 1), 1)),
        reverse=True,
    )
    total_games = len(games)
    sessions = len(set(str(g["session_id"]) for g in games))
    lines = [f"📊 *{title}*\n", f"Партий: {total_games} | Сессий: {sessions}\n", "─────────────────"]
    medals = ["🥇", "🥈", "🥉"]
    for i, p in enumerate(sorted_players):
        medal = medals[i] if i < 3 else f"{i+1}."
        wins = player_wins.get(p, 0)
        g = player_games.get(p, 0)
        wr = round(wins / g * 100, 1) if g else 0
        lines.append(f"{medal} @{p} — {wins} побед / {g} игр ({wr}%)")
    return "\n".join(lines)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    spreadsheet = ensure_sheets(get_sheet())
    now = datetime.now(timezone.utc)
    all_games = get_all_games(spreadsheet)
    month_games = [g for g in all_games if str(g.get("date", "")).startswith(f"{now.year}-{now.month:02d}")]
    await update.message.reply_text(
        build_stats(month_games, f"Статистика за {now.strftime('%B %Y')}"),
        parse_mode="Markdown"
    )


async def cmd_alltime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    spreadsheet = ensure_sheets(get_sheet())
    await update.message.reply_text(
        build_stats(get_all_games(spreadsheet), "Статистика за всё время"),
        parse_mode="Markdown"
    )


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    spreadsheet = ensure_sheets(get_sheet())
    all_games = get_all_games(spreadsheet)
    player_wins = {}
    player_games = {}
    for game in all_games:
        for p in [x.strip() for x in str(game["players"]).split(",") if x.strip()]:
            player_games[p] = player_games.get(p, 0) + 1
        for w in [x.strip() for x in str(game["winners"]).split(",") if x.strip()]:
            player_wins[w] = player_wins.get(w, 0) + 1
    if not player_wins:
        await update.message.reply_text("Пока нет данных.")
        return
    sorted_players = sorted(player_wins.items(), key=lambda x: x[1], reverse=True)[:5]
    medals = ["🥇", "🥈", "🥉", "4.", "5."]
    lines = ["🏆 *Топ игроков Jackwood (всё время)*\n"]
    for i, (player, wins) in enumerate(sorted_players):
        g = player_games.get(player, 0)
        wr = round(wins / g * 100, 1) if g else 0
        lines.append(f"{medals[i]} @{player} — {wins} побед ({wr}%)")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /player @username")
        return
    username = args[0].lstrip("@").strip()
    spreadsheet = ensure_sheets(get_sheet())
    all_games = get_all_games(spreadsheet)
    played = [g for g in all_games if username in [p.strip() for p in str(g["players"]).split(",")]]
    won = [g for g in played if username in [w.strip() for w in str(g["winners"]).split(",")]]
    if not played:
        await update.message.reply_text(f"@{username} не найден в статистике.")
        return
    wr = round(len(won) / len(played) * 100, 1)
    months = {}
    for g in played:
        month = str(g.get("date", ""))[:7]
        if month not in months:
            months[month] = {"games": 0, "wins": 0}
        months[month]["games"] += 1
        if g in won:
            months[month]["wins"] += 1
    lines = [f"👤 *@{username}*\n", f"Всего игр: {len(played)}", f"Побед: {len(won)} ({wr}%)\n", "📅 По месяцам:"]
    for month in sorted(months.keys(), reverse=True):
        m = months[month]
        mwr = round(m["wins"] / m["games"] * 100, 1)
        lines.append(f"  {month}: {m['wins']}/{m['games']} побед ({mwr}%)")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ═══════════════════════════════════════════════
# Запуск
# ═══════════════════════════════════════════════

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    add_game_conv = ConversationHandler(
        entry_points=[CommandHandler("addgame", cmd_add_game)],
        states={
            SELECT_PLAYERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_players)],
            SELECT_WINNERS: [CallbackQueryHandler(toggle_winner)],
            CONFIRM_GAME: [CallbackQueryHandler(confirm_game)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
    )

    # Отслеживаем добавление бота в группу
    app.add_handler(ChatMemberHandler(on_bot_added_to_chat, ChatMemberHandler.MY_CHAT_MEMBER))

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("newsession", cmd_new_session))
    app.add_handler(CommandHandler("endsession", cmd_end_session))
    app.add_handler(CommandHandler("cancelsession", cmd_cancel_session))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("alltime", cmd_alltime))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("player", cmd_player))
    app.add_handler(add_game_conv)

    logger.info("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
