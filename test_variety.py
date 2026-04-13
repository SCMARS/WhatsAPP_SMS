"""
test_variety.py — проверяет разнообразие outreach-сообщений от ElevenLabs.
НЕ отправляет ничего в WhatsApp.

Запуск:
    python3 test_variety.py
"""
import asyncio
import difflib
import os
import sys
import time

# ── env ──────────────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
os.environ.setdefault("API_SECRET_KEY", "test")
os.environ.setdefault("APP_HOST", "0.0.0.0")
os.environ.setdefault("APP_PORT", "8000")

sys.path.insert(0, ".")

from app.services.elevenlabs import generate_outreach_message  # noqa: E402

# ── параметры теста ───────────────────────────────────────────────────────────
AGENT_ID   = os.environ["AGENT_ID"]
LINK_PT    = "https://oro.casino/ref/testPT"
LINK_AR    = "https://pampas.casino/ref/testAR"
PROMO_PT   = "PROMO50"
PROMO_AR   = None
N_MESSAGES = 8          # сколько сообщений генерировать на язык

YELLOW = "\033[33m"
GREEN  = "\033[32m"
RED    = "\033[31m"
RESET  = "\033[0m"
BOLD   = "\033[1m"


def similarity(a: str, b: str) -> float:
    """0.0 = полностью разные, 1.0 = идентичные."""
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def opening_words(text: str, n: int = 6) -> str:
    import re
    cleaned = re.sub(r"[^\w\s]", "", text.lower())
    return " ".join(cleaned.split()[:n])


def analyze(messages: list[str], lang: str) -> None:
    print(f"\n{'='*65}")
    print(f"{BOLD}  Язык: {lang}  ({len(messages)} сообщений){RESET}")
    print(f"{'='*65}")

    for i, msg in enumerate(messages, 1):
        lines = msg.splitlines()
        preview = lines[0][:80] + ("…" if len(lines[0]) > 80 else "")
        print(f"\n  [{i}] {preview}")
        if len(lines) > 1:
            for l in lines[1:]:
                print(f"       {l[:80]}")

    # Уникальность открывающих фраз
    openings = [opening_words(m) for m in messages]
    unique_openings = len(set(openings))
    print(f"\n{BOLD}── Анализ уникальности ──{RESET}")
    print(f"  Уникальных открывающих фраз : {unique_openings}/{len(messages)}", end="  ")
    if unique_openings == len(messages):
        print(f"{GREEN}✓ ОТЛИЧНО{RESET}")
    elif unique_openings >= len(messages) * 0.7:
        print(f"{YELLOW}⚠ ПРИЕМЛЕМО{RESET}")
    else:
        print(f"{RED}✗ ПЛОХО — повторяются{RESET}")

    # Средняя попарная схожесть
    pairs = [(messages[i], messages[j])
             for i in range(len(messages))
             for j in range(i + 1, len(messages))]
    if pairs:
        avg_sim = sum(similarity(a, b) for a, b in pairs) / len(pairs)
        max_sim = max(similarity(a, b) for a, b in pairs)
        print(f"  Средняя схожесть (0–1)     : {avg_sim:.2f}", end="  ")
        if avg_sim < 0.35:
            print(f"{GREEN}✓ ХОРОШО{RESET}")
        elif avg_sim < 0.55:
            print(f"{YELLOW}⚠ УМЕРЕННО{RESET}")
        else:
            print(f"{RED}✗ СЛИШКОМ ПОХОЖИ{RESET}")

        print(f"  Максимальная схожесть      : {max_sim:.2f}", end="  ")
        if max_sim < 0.60:
            print(f"{GREEN}✓{RESET}")
        else:
            print(f"{RED}✗ есть почти дубликаты{RESET}")

    # Длины
    lengths = [len(m) for m in messages]
    print(f"  Длины сообщений (символов) : min={min(lengths)} max={max(lengths)} avg={sum(lengths)//len(lengths)}")

    # Проверка плейсхолдеров
    leftover = [m for m in messages if "{link}" in m or "{promo}" in m or "{{link}}" in m]
    if leftover:
        print(f"  {RED}✗ ВНИМАНИЕ: {len(leftover)} сообщений содержат незаменённые плейсхолдеры!{RESET}")
    else:
        print(f"  Плейсхолдеры заменены      : {GREEN}✓{RESET}")


async def run_test(lang: str, link: str, promo) -> list[str]:
    messages = []
    print(f"\n{BOLD}Генерирую {N_MESSAGES} сообщений [{lang}]...{RESET}")
    for i in range(1, N_MESSAGES + 1):
        t0 = time.time()
        chat_key = f"test_lead_{i:03d}_{lang}"
        try:
            msg = await generate_outreach_message(
                agent_id=AGENT_ID,
                chat_key=chat_key,
                language=lang,
                link_url=link,
                promo_code=promo,
            )
            elapsed = time.time() - t0
            status = f"{GREEN}OK{RESET}" if msg else f"{RED}EMPTY{RESET}"
            print(f"  [{i}/{N_MESSAGES}] {elapsed:.1f}s  {status}")
            if msg:
                messages.append(msg)
        except Exception as e:
            elapsed = time.time() - t0
            print(f"  [{i}/{N_MESSAGES}] {elapsed:.1f}s  {RED}ERROR: {e}{RESET}")
    return messages


async def main():
    print(f"\n{BOLD}=== ElevenLabs Variety Test ==={RESET}")
    print(f"Agent: {AGENT_ID}")
    print(f"Сообщений на язык: {N_MESSAGES}")
    print("(В WhatsApp ничего не отправляется)")

    # Португальский
    pt_messages = await run_test("pt-PT", LINK_PT, PROMO_PT)
    if pt_messages:
        analyze(pt_messages, "pt-PT (Portugal)")

    # Аргентинский
    ar_messages = await run_test("es-AR", LINK_AR, PROMO_AR)
    if ar_messages:
        analyze(ar_messages, "es-AR (Argentina)")

    # Итоговый вердикт
    all_messages = pt_messages + ar_messages
    print(f"\n{'='*65}")
    print(f"{BOLD}  ИТОГ{RESET}")
    print(f"{'='*65}")
    total = len(all_messages)
    unique = len(set(m[:60] for m in all_messages))
    print(f"  Всего сгенерировано : {total}/{N_MESSAGES*2}")
    print(f"  Уникальных (по началу): {unique}/{total}")

    if total < N_MESSAGES * 2 * 0.8:
        print(f"\n  {RED}✗ ПРОБЛЕМА: агент часто возвращает пустые ответы{RESET}")
    elif unique < total * 0.7:
        print(f"\n  {YELLOW}⚠ ПРЕДУПРЕЖДЕНИЕ: сообщения слишком похожи, риск бана{RESET}")
    else:
        print(f"\n  {GREEN}✓ Разнообразие достаточное для безопасной рассылки{RESET}")


if __name__ == "__main__":
    asyncio.run(main())
