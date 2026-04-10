"""
Telegram bot for testing WhatsApp bot message generation.

HOW IT WORKS
  /gen calls ElevenLabs ConvAI WebSocket N times and passes dynamic variables:
    {language}, {link}, {promo}
  The agent prompt should generate a unique outreach message each call.

Commands:
  /start         — welcome
  /gen   [N]     — generate N messages from ElevenLabs WebSocket (default 5)
  /raw           — generate one raw outreach message from ElevenLabs
  /typing [N]    — run calc_typing_time N times, show slow/fast/normal distribution
  /full          — gen×5 + typing×30 in one shot

Run:
  pip install "python-telegram-bot>=20.0"
  python telegram_tester.py
"""

import asyncio
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from app.config import settings
from app.services.elevenlabs import generate_outreach_message
from app.services.rate_limiter import calc_typing_time

TOKEN = "8734698086:AAFHYFLDzgbU6_-nNiJX_cg6b5-32QxHDSo"

# Test values — sent as dynamic variables (separate from production link pool)
TEST_LANG = os.getenv("TEST_LANG", "pt-PT")
TEST_PROMO = os.getenv("TEST_PROMO", "BONUS50")
TEST_LINK_PT = os.getenv("TEST_LINK_PT", "https://oro-casino.example/bonus?ref=TEST_PT")
TEST_LINK_ES = os.getenv("TEST_LINK_ES", "https://pampas-casino.example/bonus?ref=TEST_AR")


def _test_link_for_lang(lang: str) -> str:
    return TEST_LINK_ES if lang == "es-AR" else TEST_LINK_PT


def _languages_for_run() -> list[str]:
    # Keep configured language first, always include the second one for cross-language testing.
    primary = TEST_LANG if TEST_LANG in ("pt-PT", "es-AR") else "pt-PT"
    secondary = "es-AR" if primary == "pt-PT" else "pt-PT"
    return [primary, secondary]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash8(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def _uniqueness_bar(variants: list[str]) -> str:
    hashes  = [_hash8(v) for v in variants]
    unique  = len(set(hashes))
    total   = len(hashes)
    pct     = unique / total * 100
    bar     = "█" * unique + "░" * (total - unique)
    verdict = "✅ Колізій немає" if unique == total else f"⚠️ Колізії: {total - unique}"
    return f"Унікальних: {unique}/{total} ({pct:.0f}%)  [{bar}]\n{verdict}"


async def _fetch_template() -> str:
    """Generate one outreach message from ElevenLabs using dynamic variables."""
    if not settings.AGENT_ID:
        return ""
    return await generate_outreach_message(
        agent_id=settings.AGENT_ID,
        chat_key="telegram-tester",
        language=TEST_LANG,
        link_url=_test_link_for_lang(TEST_LANG),
        promo_code=TEST_PROMO,
    )


def _typing_report(n: int) -> str:
    results = [calc_typing_time("Привіт від нас!") for _ in range(n)]
    slow    = [x for x in results if x >= 6_000]
    fast    = [x for x in results if x <= 2_100]
    normal  = [x for x in results if 2_100 < x < 6_000]

    def pct(lst):
        return f"{len(lst)/n*100:.0f}%"

    return "\n".join([
        f"🔢 Запусків: {n}",
        f"🐢 Повільно  ≥6 с:    {len(slow):>3}  ({pct(slow)})   ← очікується ~10%",
        f"⚡ Швидко   ≤2.1 с:  {len(fast):>3}  ({pct(fast)})   ← очікується ~10%",
        f"🙂 Нормально:         {len(normal):>3}  ({pct(normal)})   ← очікується ~80%",
        f"📊 Мін: {min(results)} мс  |  Макс: {max(results)} мс  |  Avg: {sum(results)//n} мс",
    ])


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *WhatsApp Bot — Тестер*\n\n"
        "Команди:\n"
        "• /raw — 1 raw генерація з ElevenLabs WS\n"
        "• /gen `[N]` — N генерацій з ElevenLabs WS (дефолт 5)\n"
        "• /typing `[N]` — розподіл часу друку (дефолт 30)\n"
        "• /full — gen×5 + typing×30\n\n"
        "📌 Переконайся, що промпт агента використовує `{language}`, `{link}`, `{promo}`.",
        parse_mode="Markdown",
    )


async def cmd_raw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show one raw outreach message generated via WebSocket."""
    await update.message.reply_text("⏳ Генерую в ElevenLabs…")

    template = await _fetch_template()
    if not template:
        await update.message.reply_text(
            "❌ ElevenLabs повернув порожню відповідь.\n\n"
            "Перевір, що в агента налаштований промпт, який генерує outreach текст "
            "з `{language}`, `{link}`, `{promo}`.",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        f"📄 *Raw генерація ElevenLabs:*\n\n`{template}`\n\n"
        f"_lang={TEST_LANG} link={_test_link_for_lang(TEST_LANG)} promo={TEST_PROMO}_",
        parse_mode="Markdown",
    )


async def cmd_gen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate N independent outreach messages via ElevenLabs WebSocket."""
    try:
        n = max(1, min(int(context.args[0]), 15)) if context.args else 5
    except (ValueError, IndexError):
        n = 5

    await update.message.reply_text(f"⏳ Генерую через ElevenLabs WS ×{n}…")

    lines = []
    for lang in _languages_for_run():
        variants = []
        for i in range(n):
            v = await generate_outreach_message(
                agent_id=settings.AGENT_ID,
                chat_key=f"telegram-tester-{lang}-{i}",
                language=lang,
                link_url=_test_link_for_lang(lang),
                promo_code=TEST_PROMO,
            )
            variants.append(v if v else "(ElevenLabs повернув пустий рядок)")

        lines.append(f"🤖 *ELEVENLABS GEN ×{n} (lang={lang})*")
        for i, v in enumerate(variants, 1):
            display = v.replace("\u200b", "[·]").replace("\u200c", "[·]")
            lines.append(f"*{i}.* {display}")
        lines.append(_uniqueness_bar(variants))

        zw = sum(1 for v in variants if "\u200b" in v or "\u200c" in v or "\u200d" in v or "\ufeff" in v)
        lines.append(f"🔡 Zero-width char: {zw}/{n} повідомлень")
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_typing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        n = max(10, min(int(context.args[0]), 200)) if context.args else 30
    except (ValueError, IndexError):
        n = 30

    report = _typing_report(n)
    await update.message.reply_text(
        f"⏱ *calc\\_typing\\_time (N={n}):*\n\n{report}",
        parse_mode="Markdown",
    )


async def cmd_full(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Повна перевірка…")

    # 1. ElevenLabs generation ×5 for both languages
    gen_sections = []
    for lang in _languages_for_run():
        variants = []
        for i in range(5):
            v = await generate_outreach_message(
                agent_id=settings.AGENT_ID,
                chat_key=f"telegram-full-{lang}-{i}",
                language=lang,
                link_url=_test_link_for_lang(lang),
                promo_code=TEST_PROMO,
            )
            variants.append(v if v else "(ElevenLabs повернув пустий рядок)")

        if any(v and "пустий рядок" not in v for v in variants):
            zw = sum(1 for v in variants if "\u200b" in v or "\u200c" in v or "\u200d" in v or "\ufeff" in v)
            gen_unique = _uniqueness_bar(variants)
            gen_status = "✅"
        else:
            zw = 0
            gen_unique = "—"
            gen_status = "❌"

        gen_block = "\n".join(f"  {i+1}. {v}" for i, v in enumerate(variants))
        gen_sections.append(
            f"{gen_status} *ELEVENLABS GEN ×5 (lang={lang})*\n"
            f"{gen_block}\n\n"
            f"{gen_unique}\n"
            f"🔡 Zero-width: {zw}/5"
        )

    # 2. Typing ×30
    typing_block = _typing_report(30)

    report = (
        f"{'='*30}\n"
        f"{chr(10).join(gen_sections)}\n\n"
        f"{'='*30}\n"
        f"⏱ *TYPING TIME ×30*\n"
        f"{typing_block}"
    )

    await update.message.reply_text(report, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"Agent ID   : {settings.AGENT_ID or '⚠ NOT SET'}")
    print(f"EL API Key : {'✓ set' if settings.ELEVENLABS_API_KEY else '⚠ NOT SET'}")
    print(f"Test link  : {_test_link_for_lang(TEST_LANG)}")
    print(f"Test promo : {TEST_PROMO}")
    print(f"Test lang  : {TEST_LANG}")
    print("Bot is running. Press Ctrl+C to stop.")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_start))
    app.add_handler(CommandHandler("raw",    cmd_raw))
    app.add_handler(CommandHandler("gen",    cmd_gen))
    app.add_handler(CommandHandler("typing", cmd_typing))
    app.add_handler(CommandHandler("full",   cmd_full))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
