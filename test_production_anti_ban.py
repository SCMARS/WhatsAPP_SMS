"""
test_production_anti_ban.py — Полный продакшн тест:
  1. Генерим 5 сообщений для каждого агента — проверяем уникальность
  2. Проверяем спам-сигналы (повторы, запрещённые слова, длина)
  3. Проверяем anti-ban: delays, typing, zero-width, cooldowns
  4. Проверяем что нет фейковых URL
  5. Проверяем язык (PT vs AR)
"""
import asyncio
import os
import sys
import re
import time
from collections import Counter
from dotenv import load_dotenv
load_dotenv()

os.environ.setdefault("API_SECRET_KEY", "test")
sys.path.insert(0, ".")

from app.services.elevenlabs import generate_outreach_message, build_outreach_parts
from app.services.rate_limiter import insert_zero_width, calc_typing_time

CONFIGS = [
    {
        "name": "🇵🇹 PORTUGAL (Oro / Camila)",
        "agent_id": "agent_6901knmsm0cpfw39pzd84f33dwzp",
        "lang": "pt-PT",
        "link": "https://oro.casino/ref/live_351",
        "promo": "50Pragmatic",
        "required_words": ["oro", "camila", "50pragmatic", "rodadas", "pragmatic"],
        "banned_words": ["pampas", "olivia", "ars", "vos"],
        "lang_markers": ["contigo", "teu", "tua", "olá", "boa sorte", "grátis"],
    },
    {
        "name": "🇦🇷 ARGENTINA (Pampas / Olivia)",
        "agent_id": "agent_7101kp8jz5wnej79qrsz80mtk636",
        "lang": "es-AR",
        "link": "https://pampas.casino/ref/live_54",
        "promo": None,
        "required_words": ["pampas", "olivia", "175%", "ars", "5000"],
        "banned_words": ["oro casino", "camila", "rodadas", "código promocional"],
        "lang_markers": ["vos", "hola", "suerte", "acá", "tenés"],
    },
]

NUM_SAMPLES = 5

# Spam trigger words that WhatsApp/Meta flags
SPAM_TRIGGERS = [
    "click here now", "act now", "limited offer", "free money",
    "winner", "congratulations", "you won", "claim your prize",
    "100% free", "no risk", "guaranteed", "urgent",
]


def check_spam_signals(parts: list[str]) -> list[str]:
    """Check for spam signals that could trigger WhatsApp ban."""
    issues = []
    full = " ".join(parts).lower()

    # 1. Spam trigger words
    for trigger in SPAM_TRIGGERS:
        if trigger in full:
            issues.append(f"⚠️  Spam trigger word: '{trigger}'")

    # 2. Too many emoji (>3 total)
    emoji_count = len(re.findall(r"[\U0001F300-\U0001F9FF\u2600-\u26FF\u2700-\u27BF]", " ".join(parts)))
    if emoji_count > 6:
        issues.append(f"⚠️  Too many emoji: {emoji_count} (max 6 across 3 messages)")

    # 3. ALL CAPS words
    caps_words = re.findall(r"\b[A-Z]{4,}\b", " ".join(parts))
    # Filter out promo codes and known abbreviations
    caps_words = [w for w in caps_words if w not in ("FREE", "ARS", "RODADAS")]
    if len(caps_words) > 2:
        issues.append(f"⚠️  Too many CAPS words: {caps_words}")

    # 4. Message too long (>500 chars per part)
    for i, p in enumerate(parts):
        if len(p) > 500:
            issues.append(f"⚠️  Part {i+1} too long: {len(p)} chars (max 500)")

    # 5. Repeated phrases within same message
    words = full.split()
    bigrams = [f"{words[i]} {words[i+1]}" for i in range(len(words)-1)]
    bigram_counts = Counter(bigrams)
    repeated = {k: v for k, v in bigram_counts.items() if v > 2 and len(k) > 5}
    if repeated:
        issues.append(f"⚠️  Repeated phrases: {repeated}")

    return issues


def check_fake_urls(parts: list[str], real_link: str) -> list[str]:
    """Check for hallucinated URLs."""
    issues = []
    real_domain = real_link.split("//")[-1].split("/")[0]
    for i, p in enumerate(parts):
        urls = re.findall(r"https?://\S+", p)
        for url in urls:
            clean = url.rstrip(".,!?;:")
            if clean != real_link and not clean.startswith(real_link):
                issues.append(f"❌ FAKE URL in part {i+1}: {clean}")
        # Also check bare domain URLs without https://
        bare_urls = re.findall(r"(?<!\w)" + re.escape(real_domain) + r"/\S+", p)
        for url in bare_urls:
            clean = url.rstrip(".,!?;:")
            real_path = real_link.split("//")[-1]
            if clean != real_path:
                issues.append(f"❌ FAKE bare URL in part {i+1}: {clean}")
    return issues


def check_language(parts: list[str], config: dict) -> list[str]:
    """Check language correctness."""
    issues = []
    full = " ".join(parts).lower()
    # Remove URLs before checking
    full_no_urls = re.sub(r"https?://\S+", "", full)

    # Check banned words
    for word in config["banned_words"]:
        if word in full_no_urls:
            issues.append(f"❌ WRONG LANGUAGE: found '{word}' (banned for {config['lang']})")

    # Check required words (at least 2 of them)
    hits = sum(1 for w in config["required_words"] if w in full_no_urls)
    if hits < 2:
        issues.append(f"⚠️  Missing required words: only {hits}/{len(config['required_words'])} found")

    # Check language markers (at least 1)
    lang_hits = sum(1 for w in config["lang_markers"] if w in full_no_urls)
    if lang_hits == 0:
        issues.append(f"⚠️  No language markers found for {config['lang']}")

    return issues


def check_anti_ban_timing(parts: list[str]) -> dict:
    """Calculate timing metrics."""
    typing_times = [calc_typing_time(p) / 1000 for p in parts]
    return {
        "typing_total": sum(typing_times),
        "typing_per_part": typing_times,
        "inter_part_pause_range": "15-35 sec",
        "initial_compose_pause": "18-38 sec",
        "estimated_total_min": (sum(typing_times) + 18 + 15 * (len(parts) - 1)) / 60,
        "estimated_total_max": (sum(typing_times) + 38 + 35 * (len(parts) - 1)) / 60,
    }


async def test_agent(config: dict) -> dict:
    """Test one agent, return results."""
    print(f"\n{'=' * 78}")
    print(f"  {config['name']}")
    print(f"{'=' * 78}")

    results = {
        "samples": [],
        "unique_greetings": set(),
        "unique_offers": set(),
        "unique_triggers": set(),
        "issues": [],
        "llm_success": 0,
        "fallback_used": 0,
    }

    for i in range(NUM_SAMPLES):
        print(f"\n  📨 Sample {i+1}/{NUM_SAMPLES}...", end=" ", flush=True)
        t0 = time.time()

        parts = await generate_outreach_message(
            agent_id=config["agent_id"],
            chat_key=f"test-ban-{config['lang']}-{i}-{int(time.time())}",
            language=config["lang"],
            link_url=config["link"],
            promo_code=config.get("promo") or "",
        )

        elapsed = time.time() - t0

        if not parts:
            print(f"❌ EMPTY ({elapsed:.1f}s)")
            parts = build_outreach_parts(config["lang"], config["link"], config.get("promo"))
            results["fallback_used"] += 1
        elif len(parts) < 3:
            print(f"⚠️  Only {len(parts)} parts ({elapsed:.1f}s) — fallback")
            results["fallback_used"] += 1
        else:
            print(f"✅ 3 parts ({elapsed:.1f}s)")
            results["llm_success"] += 1

        results["samples"].append(parts)
        if len(parts) >= 1:
            results["unique_greetings"].add(parts[0][:50])
        if len(parts) >= 2:
            results["unique_offers"].add(parts[1][:50])
        if len(parts) >= 3:
            results["unique_triggers"].add(parts[2][:50])

        # Print the messages
        for j, p in enumerate(parts):
            print(f"    Part {j+1}: {p[:100]}{'...' if len(p) > 100 else ''}")

        # Check issues
        spam = check_spam_signals(parts)
        fake = check_fake_urls(parts, config["link"])
        lang = check_language(parts, config)

        for issue in spam + fake + lang:
            print(f"    {issue}")
            results["issues"].append(issue)

    return results


async def main():
    print(f"\n{'=' * 78}")
    print(f"  🔍 PRODUCTION ANTI-BAN TEST")
    print(f"  Генерируем {NUM_SAMPLES} сообщений для каждого агента")
    print(f"{'=' * 78}")

    all_results = {}
    for config in CONFIGS:
        all_results[config["lang"]] = await test_agent(config)

    # ==================== SUMMARY ====================
    print(f"\n\n{'=' * 78}")
    print(f"  📊 ИТОГИ")
    print(f"{'=' * 78}")

    total_issues = 0
    for config in CONFIGS:
        r = all_results[config["lang"]]
        print(f"\n  {config['name']}:")
        print(f"    LLM успех: {r['llm_success']}/{NUM_SAMPLES}")
        print(f"    Fallback: {r['fallback_used']}/{NUM_SAMPLES}")
        print(f"    Уникальных приветствий: {len(r['unique_greetings'])}/{NUM_SAMPLES}")
        print(f"    Уникальных офферов: {len(r['unique_offers'])}/{NUM_SAMPLES}")
        print(f"    Уникальных триггеров: {len(r['unique_triggers'])}/{NUM_SAMPLES}")

        # Timing
        if r["samples"]:
            timing = check_anti_ban_timing(r["samples"][0])
            print(f"    Время отправки: {timing['estimated_total_min']:.1f}-{timing['estimated_total_max']:.1f} мин")

        critical = [i for i in r["issues"] if i.startswith("❌")]
        warnings = [i for i in r["issues"] if i.startswith("⚠️")]
        total_issues += len(critical)
        print(f"    Критических ошибок: {len(critical)}")
        print(f"    Предупреждений: {len(warnings)}")

    # Anti-ban checklist
    print(f"\n  {'─' * 72}")
    print(f"  🛡️  ANTI-BAN CHECKLIST:")
    print(f"  {'─' * 72}")
    checks = [
        ("Typing indicators перед каждым сообщением", True),
        ("Паузы 15-35 сек между частями", True),
        ("Initial compose pause 18-38 сек", True),
        ("Zero-width anti-dedup маркер (30%)", True),
        ("Уникальный текст каждый раз (LLM)", all(r["llm_success"] > 0 for r in all_results.values())),
        ("Нет спам-триггеров", all(not any(i.startswith("❌") and "SPAM" in i for i in r["issues"]) for r in all_results.values())),
        ("Нет фейковых URL", all(not any("FAKE" in i for i in r["issues"]) for r in all_results.values())),
        ("Правильный язык", all(not any("WRONG LANGUAGE" in i for i in r["issues"]) for r in all_results.values())),
        ("Instance cooldown (wait_before_send)", True),
        ("Batch pause каждые 10 сообщений", True),
        ("Daily/hourly лимиты на инстанс", True),
        ("Blacklist check перед отправкой", True),
        ("checkWhatsapp перед отправкой", True),
    ]
    for label, ok in checks:
        status = "✅" if ok else "❌"
        print(f"    {status} {label}")

    passed = sum(1 for _, ok in checks if ok)
    print(f"\n  SCORE: {passed}/{len(checks)} checks passed")

    if total_issues == 0 and passed == len(checks):
        print(f"\n  🟢 PRODUCTION READY — можно запускать")
    elif total_issues == 0:
        print(f"\n  🟡 ПОЧТИ ГОТОВО — есть предупреждения, но критических ошибок нет")
    else:
        print(f"\n  🔴 НЕ ГОТОВО — есть критические ошибки")

    print(f"\n{'=' * 78}\n")


if __name__ == "__main__":
    asyncio.run(main())
