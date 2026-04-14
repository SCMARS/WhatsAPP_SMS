"""
test_variety_verbose.py — 5 сообщений на PT + 5 на AR.
Показывает ТОЧНО что отправлялось в ElevenLabs (instruction + dynamic_variables)
и что было бы отправлено в Green API (итоговый текст).
"""
import asyncio
import os
import sys
import random
import time
import uuid

from dotenv import load_dotenv
load_dotenv()
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
os.environ.setdefault("API_SECRET_KEY", "test")
os.environ.setdefault("APP_HOST", "0.0.0.0")
os.environ.setdefault("APP_PORT", "8000")

sys.path.insert(0, ".")

from app.services.elevenlabs import _get_session, WHATSAPP_CONTEXT, STYLE_HINTS, _normalize_dynamic_variables, _open_socket, _fallback_outreach
from app.config import settings

AGENT_ID = os.environ["AGENT_ID"]
LINK_PT  = "https://oro.casino/ref/testPT"
LINK_AR  = "https://pampas.casino/ref/testAR"
PROMO_PT = "PROMO50"
PROMO_AR = None

SEP  = "─" * 70
SEP2 = "═" * 70

OPENERS = [
    "start with a question to the lead",
    "start with an emoji, then the offer",
    "start with the bonus amount first",
    "start with urgency (limited time)",
    "start with the activation instruction",
    "start with a compliment then offer",
    "start with curiosity hook, no greeting",
    "start with the casino name and a bold claim",
]
TONES = [
    "casual and friendly", "energetic and short",
    "formal but warm", "playful with emojis",
]

async def run_one(session, i: int, lang: str, link_url: str, promo_code):
    dynamic_variables = {
        "language": lang,
        "link": link_url,
        "promo": promo_code or "",
        "variant_id": f"v{random.randint(1000, 9999)}",
        "anti_spam_seed": f"{int(time.time() * 1000)}-{random.randint(10000, 99999)}",
    }
    opener = random.choice(OPENERS)
    tone   = random.choice(TONES)
    style_hint = random.choice(STYLE_HINTS)
    uid = str(uuid.uuid4())[:8]

    instruction = (
        f"Write a UNIQUE WhatsApp outreach message. "
        f"Seed={dynamic_variables['anti_spam_seed']} — your reply MUST differ from all previous ones. "
        f"Opening style: {opener}. Tone: {tone}. "
        f"Language={lang}. Include link={{link}} and promo={{promo}} naturally. "
        f"Max 3 sentences. Return ONLY the message text."
    )

    print(f"\n{SEP}")
    print(f"  Итерация #{i} | lang={lang}")
    print(SEP)
    print(f"  ► [ОТПРАВЛЕНО В ELEVENLABS]")
    print(f"    context_text  : «{WHATSAPP_CONTEXT} Style for this message: {style_hint} [uid:{uid}]»")
    print(f"    last_user_text: «{instruction}»")
    print(f"    dynamic_vars  : {dynamic_variables}")

    try:
        reply = await session.ask(
            agent_id=AGENT_ID,
            context_text=f"{WHATSAPP_CONTEXT} Style for this message: {style_hint} [uid:{uid}]",
            prior_turns=[],
            last_user_text=instruction,
            language=None,
            dynamic_variables=dynamic_variables,
        )
        # normalize placeholders
        result = (reply or "").strip()
        result = result.replace("{{link}}", link_url).replace("{link}", link_url)
        result = result.replace("{{promo}}", promo_code or "").replace("{promo}", promo_code or "")
        result = result.replace("{{language}}", lang).replace("{language}", lang)
        result = result.strip()
    except Exception as e:
        print(f"    ERROR: {e}")
        result = ""

    print(f"\n  ► [ОТВЕТ АГЕНТА / ЧТО УШЛО БЫ В GREEN API]")
    if result:
        for line in result.splitlines():
            print(f"    {line}")
    else:
        print("    (пусто — fallback)")
        result = _fallback_outreach(language=lang, link_url=link_url, promo_code=promo_code)
        for line in result.splitlines():
            print(f"    {line}")
    return result


async def run_lang(lang: str, link_url: str, promo_code, n: int = 5):
    print(f"\n{SEP2}")
    print(f"  ЯЗЫК: {lang}  ({n} сообщений)")
    print(SEP2)
    results = []
    for i in range(1, n + 1):
        key = f"outreach:test_{lang}_{i}"
        session = _get_session(key)
        # warm-up (consume initial greeting)
        try:
            await session.ask(
                agent_id=AGENT_ID,
                context_text=WHATSAPP_CONTEXT,
                prior_turns=[],
                last_user_text="Hi",
                language=None,
                dynamic_variables=None,
            )
        except Exception:
            pass
        msg = await run_one(session, i, lang, link_url, promo_code)
        await session.reset()
        results.append(msg)
    return results


async def main():
    print(f"\n{'='*70}")
    print(f"  ElevenLabs Variety Test (verbose) — agent: {AGENT_ID}")
    print(f"  5 × pt-PT  +  5 × es-AR")
    print(f"{'='*70}")

    pt = await run_lang("pt-PT", LINK_PT, PROMO_PT, 5)
    ar = await run_lang("es-AR", LINK_AR, PROMO_AR, 5)

    print(f"\n{SEP2}")
    print("  ИТОГ — уникальность первых 60 символов")
    print(SEP2)
    all_msgs = pt + ar
    unique = len(set(m[:60] for m in all_msgs if m))
    print(f"  PT уникальных: {len(set(m[:60] for m in pt if m))}/5")
    print(f"  AR уникальных: {len(set(m[:60] for m in ar if m))}/5")
    print(f"  Всего уникальных: {unique}/{len(all_msgs)}")


if __name__ == "__main__":
    asyncio.run(main())
