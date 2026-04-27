"""
Psychological Support & Venting Telegram Bot
Features: Memory, Crisis Detection (India helplines), Sentiment Tracking
Stack: python-telegram-bot v20+, openai v1+, SQLite (no external DB needed)
"""

import os
import re
import json
import sqlite3
import asyncio
from datetime import datetime
from openai import OpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# ─────────────────────────────────────────
# 🔑  PASTE YOUR KEYS HERE
# ─────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

# ─────────────────────────────────────────
# 🚨  CRISIS CONFIG
# ─────────────────────────────────────────
CRISIS_KEYWORDS = [
    "suicide", "kill myself", "end my life", "want to die", "no reason to live",
    "can't go on", "overdose", "self harm", "cut myself", "hurt myself",
    "better off dead", "end it all", "no point living", "khud ko khatam",
    "jeena nahi", "mar jaana chahta", "mar jaana chahti",
]

CRISIS_BLOCK = """
🆘 *I hear that you're carrying something very heavy right now.*

I'm an AI — I care, but I'm not equipped to give you what you need in this moment.
Please reach out to a real human *right now*:

📞 *KIRAN* (24x7, free): `1800-599-0019`
📞 *AASRA* (24x7): `+91 98204 66726`
📞 *Tele-MANAS* (Govt): `14416`
📞 *Vandrevala Foundation*: `+91 9999 666 555`
🚨 *Emergency*: `112`

You don't have to explain everything. Just call and say _"I need to talk."_
Someone will listen. 💙

Type /continue when you feel ready to come back.
"""

CRISIS_LOCK_MINUTES = 10  # bot stays in safe-mode for this long after trigger

# ─────────────────────────────────────────
# 🗄️  DATABASE (SQLite — no setup needed)
# ─────────────────────────────────────────
DB_PATH = "bot_memory.db"

def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    # Stores last N messages per user for memory
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            role       TEXT NOT NULL,       -- 'user' or 'assistant'
            content    TEXT NOT NULL,
            sentiment  TEXT DEFAULT NULL,   -- positive / neutral / negative
            ts         DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Tracks crisis-lock state
    cur.execute("""
        CREATE TABLE IF NOT EXISTS crisis_state (
            user_id    INTEGER PRIMARY KEY,
            locked     INTEGER DEFAULT 0,   -- 1 = locked
            locked_at  DATETIME DEFAULT NULL
        )
    """)
    con.commit()
    con.close()

def save_message(user_id: int, role: str, content: str, sentiment: str = None):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO messages (user_id, role, content, sentiment) VALUES (?,?,?,?)",
        (user_id, role, content, sentiment),
    )
    con.commit()
    con.close()

def get_history(user_id: int, limit: int = 12) -> list[dict]:
    """Returns last `limit` messages as OpenAI-style dicts."""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    rows = cur.execute(
        """SELECT role, content FROM messages
           WHERE user_id = ?
           ORDER BY ts DESC LIMIT ?""",
        (user_id, limit),
    ).fetchall()
    con.close()
    # Reverse so oldest first
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

def set_crisis_lock(user_id: int, locked: bool):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """INSERT INTO crisis_state (user_id, locked, locked_at)
           VALUES (?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET locked=excluded.locked, locked_at=excluded.locked_at""",
        (user_id, 1 if locked else 0, datetime.utcnow().isoformat() if locked else None),
    )
    con.commit()
    con.close()

def is_crisis_locked(user_id: int) -> bool:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    row = cur.execute(
        "SELECT locked, locked_at FROM crisis_state WHERE user_id=?", (user_id,)
    ).fetchone()
    con.close()
    if not row or not row[0]:
        return False
    if row[1]:
        locked_at = datetime.fromisoformat(row[1])
        elapsed = (datetime.utcnow() - locked_at).total_seconds() / 60
        if elapsed >= CRISIS_LOCK_MINUTES:
            set_crisis_lock(user_id, False)
            return False
    return True

# ─────────────────────────────────────────
# 🧠  SYSTEM PROMPT
# ─────────────────────────────────────────
SYSTEM_PROMPT = """
You are a warm, psychologically-informed emotional support companion on Telegram.
Your name is Mentra.

Your core principles:
1. LISTEN FIRST — never jump to advice. Validate feelings before offering anything.
2. ANALYTICAL BUT WARM — you think clearly and explain things simply, like a trusted friend who studied psychology.
3. NO TOXIC POSITIVITY — don't say "it'll all be fine." Say "this is hard, and here's one way to look at it."
4. RESPECT ANONYMITY — never ask for name, phone, location. The user's privacy is sacred.
5. NO DEPENDENCY — never say "I miss you" or "I love you". You care, but you're a tool, not a relationship.
6. PROFESSIONAL HANDOFF — in distress, always suggest real humans. You are first-level support, not final treatment.

Conversation style:
- Short paragraphs (3-4 lines max per point)
- Use *bold* for key ideas (Telegram markdown)
- End every response with 1 small actionable question or reflection, never a lecture
- If user is venting, ONLY validate — no advice unless they ask

You remember context from earlier in this conversation. Reference it naturally when relevant.
Example: "Earlier you mentioned exams — is that still weighing on you?"
"""

# ─────────────────────────────────────────
# 🔍  HELPER FUNCTIONS
# ─────────────────────────────────────────
def detect_crisis(text: str) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in CRISIS_KEYWORDS)

def analyze_sentiment(text: str) -> str:
    """
    Lightweight local sentiment — no extra API call.
    Returns: 'negative' | 'neutral' | 'positive'
    """
    negative_words = [
        "sad", "depressed", "anxious", "hopeless", "angry", "scared", "alone",
        "worthless", "exhausted", "crying", "panic", "numb", "empty",
        "udaas", "dara", "ghabra", "akela", "thaka",
    ]
    positive_words = [
        "happy", "good", "better", "hopeful", "grateful", "calm", "motivated",
        "proud", "relieved", "excited", "khush", "theek",
    ]
    text_lower = text.lower()
    neg = sum(1 for w in negative_words if w in text_lower)
    pos = sum(1 for w in positive_words if w in text_lower)
    if neg > pos:
        return "negative"
    if pos > neg:
        return "positive"
    return "neutral"

def mood_emoji(sentiment: str) -> str:
    return {"positive": "🟢", "neutral": "🟡", "negative": "🔴"}.get(sentiment, "⚪")

def get_emergency_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🆘 Emergency Help", callback_data="emergency")],
    ])

# ─────────────────────────────────────────
# 📨  COMMAND HANDLERS
# ─────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = get_emergency_keyboard()
    await update.message.reply_text(
        "👋 *Hi, I'm Mentra* — your anonymous psychological support companion.\n\n"
        "I'm here to *listen, reflect, and help you think clearly* — not to judge.\n\n"
        "⚠️ *Important*: I am an AI, not a therapist or crisis counselor.\n"
        "If you're in immediate danger, use /help or the button below anytime.\n\n"
        "You're completely anonymous here. Nothing you share is linked to your identity.\n\n"
        "What's on your mind? 💙",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        CRISIS_BLOCK,
        parse_mode="Markdown",
    )

async def vent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["vent_mode"] = True
    await update.message.reply_text(
        "💬 *Vent mode on.*\n\n"
        "Go ahead — say everything. I won't give advice, I'll just listen and reflect back what I hear.\n"
        "Type /done when you've said it all.",
        parse_mode="Markdown",
    )

async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["vent_mode"] = False
    await update.message.reply_text(
        "✅ Vent mode off. Thank you for sharing that.\n\n"
        "Would you like me to reflect on anything you shared, or are you just here to be heard?",
    )

async def mood_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    rows = cur.execute(
        """SELECT sentiment, ts FROM messages
           WHERE user_id=? AND role='user' AND sentiment IS NOT NULL
           ORDER BY ts DESC LIMIT 10""",
        (user_id,),
    ).fetchall()
    con.close()

    if not rows:
        await update.message.reply_text("No mood data yet. Keep chatting and I'll track patterns. 📊")
        return

    counts = {"positive": 0, "neutral": 0, "negative": 0}
    for row in rows:
        counts[row[0]] = counts.get(row[0], 0) + 1

    total = sum(counts.values())
    summary = (
        f"📊 *Your recent mood pattern (last {total} messages):*\n\n"
        f"🟢 Positive: {counts['positive']} ({round(counts['positive']/total*100)}%)\n"
        f"🟡 Neutral:  {counts['neutral']} ({round(counts['neutral']/total*100)}%)\n"
        f"🔴 Negative: {counts['negative']} ({round(counts['negative']/total*100)}%)\n\n"
    )
    if counts["negative"] > counts["positive"]:
        summary += "_Looks like things have felt heavy lately. That's valid — you're here, and that takes courage._"
    elif counts["positive"] > counts["negative"]:
        summary += "_There's been some light in your recent messages. Hold onto that. 💙_"
    else:
        summary += "_You're navigating a mixed stretch. One day at a time._"

    await update.message.reply_text(summary, parse_mode="Markdown")

async def continue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    set_crisis_lock(user_id, False)
    await update.message.reply_text(
        "💙 I'm glad you're back.\n\n"
        "You don't have to explain anything. Just tell me — how are you right now, in one word or one sentence?",
    )

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
    cur.execute("DELETE FROM crisis_state WHERE user_id=?", (user_id,))
    con.commit()
    con.close()
    context.user_data.clear()
    await update.message.reply_text(
        "🗑️ Memory cleared. Fresh start.\n\nWhat would you like to talk about?",
    )

# ─────────────────────────────────────────
# 🔘  INLINE BUTTON HANDLER
# ─────────────────────────────────────────
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "emergency":
        await query.message.reply_text(CRISIS_BLOCK, parse_mode="Markdown")

# ─────────────────────────────────────────
# 💬  MAIN MESSAGE HANDLER
# ─────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id   = update.effective_user.id
    user_text = update.message.text.strip()

    # ── 1. Crisis detection (always runs first) ──────────────────────────────
    if detect_crisis(user_text):
        set_crisis_lock(user_id, True)
        save_message(user_id, "user", user_text, sentiment="negative")
        await update.message.reply_text(CRISIS_BLOCK, parse_mode="Markdown")
        return

    # ── 2. Crisis lock (bot stays in safe-mode) ──────────────────────────────
    if is_crisis_locked(user_id):
        await update.message.reply_text(
            "💙 I'm still here.\n\n"
            "The helplines above are available 24x7. If you're in a safer place now, type /continue.",
            parse_mode="Markdown",
        )
        return

    # ── 3. Sentiment analysis ─────────────────────────────────────────────────
    sentiment = analyze_sentiment(user_text)

    # ── 4. Save user message ──────────────────────────────────────────────────
    save_message(user_id, "user", user_text, sentiment=sentiment)

    # ── 5. Build message history (memory) ────────────────────────────────────
    history = get_history(user_id, limit=12)

    # ── 6. Adjust system prompt for vent mode ────────────────────────────────
    system = SYSTEM_PROMPT
    if context.user_data.get("vent_mode"):
        system += (
            "\n\nIMPORTANT: User is in VENT MODE. "
            "Do NOT give advice or solutions. ONLY validate, reflect, and acknowledge feelings. "
            "Mirror back what they said in your own words so they feel heard."
        )

    # ── 7. Call OpenAI ────────────────────────────────────────────────────────
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system}] + history,
            max_tokens=500,
            temperature=0.75,
        )
        reply = response.choices[0].message.content.strip()
    except Exception as e:
        reply = (
            "I'm having a moment of silence 🤫\n"
            "Something went wrong on my end. Please try again in a bit.\n\n"
            f"_(Error: {str(e)[:80]})_"
        )

    # ── 8. Save assistant reply ───────────────────────────────────────────────
    save_message(user_id, "assistant", reply)

    # ── 9. Send reply with Emergency Help button (mood tracked silently) ──────
    await update.message.reply_text(
        reply,
        parse_mode="Markdown",
        reply_markup=get_emergency_keyboard(),
    )

# ─────────────────────────────────────────
# 🚀  MAIN
# ─────────────────────────────────────────
def main():
    init_db()
    print("✅ Database initialised.")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("help",     help_cmd))
    app.add_handler(CommandHandler("vent",     vent_cmd))
    app.add_handler(CommandHandler("done",     done_cmd))
    app.add_handler(CommandHandler("mood",     mood_cmd))
    app.add_handler(CommandHandler("continue", continue_cmd))
    app.add_handler(CommandHandler("reset",    reset_cmd))

    # Inline buttons
    app.add_handler(CallbackQueryHandler(button_handler))

    # All text messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Mentra bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
