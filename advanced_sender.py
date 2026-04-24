#!/usr/bin/env python3
"""
Advanced Telegram Outreach Sender — Anti-Ban Edition
Features: typing imitation, 4-stage follow-up, working hours, warmup,
reply detection, crash-resume, personalization, bot/no-photo filter,
proxy rotation, cooldowns, daily reports, progress tracking in status.json
"""
import asyncio
import json
import os
import random
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError, PeerFloodError, UserPrivacyRestrictedError,
    UserIsBlockedError, InputUserDeactivatedError, PhoneNumberBannedError,
    UserDeactivatedBanError, AuthKeyError, AuthKeyUnregisteredError,
)
from telethon.tl.functions.contacts import ImportContactsRequest, DeleteContactsRequest
from telethon.tl.types import InputPhoneContact

from accounts_config import ACCOUNTS

API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"

# ─── CONFIGURATION ────────────────────────────────────────────────────────────

TARGETS_FILE = "targets.txt"   # one E.164 phone per line, e.g. +447781539689
STATUS_FILE = "status.json"
REPORTS_DIR = Path("reports")

# Stage delays in days from first contact (stage 0 = immediate)
SEQUENCE_DELAYS_DAYS = [0, 2, 5, 8]

# Message templates per stage — {name} is replaced with first_name
TEMPLATES: dict[int, list[str]] = {
    0: [
        "Hey{_name}! 👋 How's it going?",
        "Hi{_name}, hope you're doing well!",
        "Hello{_name}! Nice to connect with you 😊",
    ],
    1: [
        "Hey{_name}, just checking in — did you see my message? 👋",
        "Hi{_name}! I wanted to follow up real quick",
    ],
    2: [
        "Still here if you want to chat 😊",
        "Hey{_name}! Just one more follow-up from me",
    ],
    3: [
        "Hey{_name}, this is my last message — feel free to reach out anytime! 👋",
        "Wrapping up here — hope to hear from you sometime!",
    ],
}

# Working hours in UTC+7 (Indonesia / Bangkok time)
TZ_OFFSET_HOURS = 7
WORK_HOUR_START = 10   # 10:00 local
WORK_HOUR_END = 21     # 21:00 local

# Per-account daily message limit (увеличен для покрытия ВСЕ номера)
DAILY_LIMIT_PER_ACCOUNT = 50  # было 25, теперь 50

# Delay between messages (ОПТИМИЗИРОВАНО — быстро но безопасно)
DELAY_YOUNG = (90, 150)     # было 180-300, теперь 90-150 (2x быстрее)
DELAY_MID = (60, 90)        # было 120-180, теперь 60-90
DELAY_OLD = (30, 60)        # было 60-120, теперь 30-60

# Long pause сохранено (защита от бана)
LONG_PAUSE_CHANCE = 0.05    # было 0.10, теперь 5% (реже)
LONG_PAUSE_RANGE = (300, 600)  # было 600-1200, теперь 300-600 (короче)

# Cooldown оптимизирован
COOLDOWN_EVERY_N = 10       # было 5, теперь 10 (реже паузы)
COOLDOWN_MIN_SEC = 300      # было 600, теперь 300 сек (5 мин)
COOLDOWN_MAX_SEC = 600      # было 1200, теперь 600 сек (10 мин)

# Proxy list — leave empty to disable
# Format: {"host": "1.2.3.4", "port": 1080, "username": "u", "password": "p"}
PROXIES: list[dict] = []
PROXY_ROTATE_EVERY = 10   # reconnect with new proxy every N messages

# Warmup phrases (accounts message each other at startup)
WARMUP_PHRASES = [
    "Hey, good morning! 🌅",
    "Hello! Hope you're having a great day",
    "Hi there! Ready for the day?",
    "Morning! How's everything going? ☀️",
    "Hey! Just checking in 👋",
]

# Account register times (unix) — used to compute age for delay tuning
REGISTER_TIMES = {
    "62895326740750": None,
    "6289526597424":  1743154698,
    "62895326740778": 1756627393,
    "6289526635916":  1742885543,
}

# ─── TIMING HELPERS ───────────────────────────────────────────────────────────

def get_local_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)


def is_working_hours() -> bool:
    h = get_local_now().hour
    return WORK_HOUR_START <= h < WORK_HOUR_END


def seconds_until_work() -> int:
    local = get_local_now()
    h = local.hour
    if h >= WORK_HOUR_END:
        # next-day start
        next_start = (local + timedelta(days=1)).replace(
            hour=WORK_HOUR_START, minute=0, second=0, microsecond=0
        )
    elif h < WORK_HOUR_START:
        next_start = local.replace(
            hour=WORK_HOUR_START, minute=0, second=0, microsecond=0
        )
    else:
        return 0
    return max(0, int((next_start - local).total_seconds()))


def get_delay(phone: str) -> tuple[float, float]:
    reg = REGISTER_TIMES.get(phone)
    if reg is None:
        return DELAY_YOUNG
    age = (time.time() - reg) / 86400
    if age < 90:
        return DELAY_YOUNG
    elif age < 180:
        return DELAY_MID
    return DELAY_OLD


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ─── STATUS / PERSISTENCE ─────────────────────────────────────────────────────

def load_status() -> dict:
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"contacts": {}, "daily_counts": {}}


def save_status(status: dict) -> None:
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(status, f, indent=2)
    os.replace(tmp, STATUS_FILE)


def increment_daily(status: dict, acc_name: str) -> None:
    today = today_str()
    status.setdefault("daily_counts", {}).setdefault(acc_name, {})
    status["daily_counts"][acc_name][today] = (
        status["daily_counts"][acc_name].get(today, 0) + 1
    )


def get_daily_sent(status: dict, acc_name: str) -> int:
    today = today_str()
    return status.get("daily_counts", {}).get(acc_name, {}).get(today, 0)


def get_next_stage(contact: dict) -> Optional[int]:
    """Return next stage index to send, or None if done / replied / stopped.

    Delays are ABSOLUTE days from stage 0:
    - Stage 0: day 0 (immediate)
    - Stage 1: day 2 (2 days after stage 0)
    - Stage 2: day 5 (5 days after stage 0)
    - Stage 3: day 8 (8 days after stage 0)
    """
    if contact.get("replied") or contact.get("stopped"):
        return None
    sent = contact.get("sent_stages", {})  # {"0": "2026-04-24T...", "1": "..."}
    if not sent:
        return 0
    last_stage = max(int(k) for k in sent)
    if last_stage >= len(SEQUENCE_DELAYS_DAYS) - 1:
        return None

    # Get the timestamp of stage 0 (initial contact)
    stage0_iso = sent.get("0")
    if not stage0_iso:
        return None
    stage0_time = datetime.fromisoformat(stage0_iso)
    if stage0_time.tzinfo is None:
        stage0_time = stage0_time.replace(tzinfo=timezone.utc)

    days_since_stage0 = (datetime.now(timezone.utc) - stage0_time).total_seconds() / 86400

    # Try all stages after last_stage and see which is eligible
    for check_stage in range(last_stage + 1, len(SEQUENCE_DELAYS_DAYS)):
        if days_since_stage0 >= SEQUENCE_DELAYS_DAYS[check_stage]:
            # This stage is eligible and hasn't been sent yet
            if str(check_stage) not in sent:
                return check_stage
        else:
            # Not enough days yet — no later stages are eligible either
            break

    return None


# ─── LOGGING ──────────────────────────────────────────────────────────────────

_report_lines: list[str] = []


def log(acc_name: str, msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] [{acc_name}] {msg}"
    print(line, flush=True)
    _report_lines.append(line)


def save_report(stats: dict) -> None:
    REPORTS_DIR.mkdir(exist_ok=True)
    fname = REPORTS_DIR / f"report_{today_str()}.txt"
    with open(fname, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*55}\n")
        f.write(f"RUN: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"{'='*55}\n")
        for line in _report_lines:
            f.write(line + "\n")
        f.write(f"\n--- STATS ---\n")
        for k, v in stats.items():
            f.write(f"{k}: {v}\n")
    print(f"\n📄 Отчёт сохранён: {fname}")


# ─── TELEGRAM HELPERS ─────────────────────────────────────────────────────────

def make_client(acc: dict, proxy_cfg: Optional[dict] = None) -> TelegramClient:
    kwargs: dict = {
        "device_model": acc["device_model"],
        "system_version": acc["system_version"],
        "app_version": acc["app_version"],
        "lang_code": acc["lang_code"],
        "system_lang_code": acc["system_lang_code"],
    }
    if proxy_cfg:
        try:
            import socks
            kwargs["proxy"] = (
                socks.SOCKS5,
                proxy_cfg["host"],
                proxy_cfg["port"],
                True,
                proxy_cfg.get("username"),
                proxy_cfg.get("password"),
            )
        except ImportError:
            pass
    # Use account-specific API credentials if provided, otherwise use defaults
    api_id = acc.get("api_id", API_ID)
    api_hash = acc.get("api_hash", API_HASH)
    return TelegramClient(acc["session"], api_id, api_hash, **kwargs)


async def check_spamblock(client: TelegramClient, name: str) -> bool:
    try:
        spambot = await client.get_entity("@SpamBot")
        await client.send_message(spambot, "/start")
        await asyncio.sleep(5)
        msgs = await client.get_messages(spambot, limit=1)
        if not msgs:
            return True
        text = msgs[0].text.lower()
        if any(w in text for w in ("free", "good standing", "no limit", "no complaints")):
            log(name, "✅ SpamBot: чист")
            return True
        log(name, f"⛔ SpamBot: ограничен → {msgs[0].text[:80]}")
        return False
    except Exception as e:
        log(name, f"⚠️ SpamBot: не проверить ({e}), продолжаем")
        return True


async def resolve_user(client: TelegramClient, phone: str):
    """Return Telegram User entity or None."""
    digits = "".join(c for c in phone if c.isdigit())
    try:
        return await client.get_entity(f"+{digits}")
    except Exception:
        pass
    try:
        contact = InputPhoneContact(client_id=0, phone=f"+{digits}", first_name="User", last_name="")
        result = await client(ImportContactsRequest([contact]))
        if result.users:
            user = result.users[0]
            try:
                await client(DeleteContactsRequest(id=[user]))
            except Exception:
                pass
            return user
    except Exception:
        pass
    return None


async def has_replied(client: TelegramClient, user) -> bool:
    """Check if this user has sent us a message."""
    try:
        msgs = await client.get_messages(user, limit=10)
        for m in msgs:
            if not m.out and m.text:
                return True
    except Exception:
        pass
    return False


async def send_with_typing(client: TelegramClient, user, text: str) -> bool:
    """Show typing indicator, then send. Returns True on success."""
    typing_sec = min(max(len(text) * 0.04, 2.0), 8.0)
    async with client.action(user, "typing"):
        await asyncio.sleep(typing_sec)
    message = await client.send_message(user, text)
    return message is not None


async def random_online_pause(client: TelegramClient, name: str) -> None:
    """Simulate natural browsing — fetch some dialogs."""
    try:
        await client.get_dialogs(limit=5)
        log(name, "👀 Случайная активность (просмотр диалогов)")
    except Exception:
        pass


# ─── PER-ACCOUNT SENDER ───────────────────────────────────────────────────────

async def run_account(acc: dict, targets: list[str], status: dict, global_lock: asyncio.Lock) -> dict:
    name = acc["name"]
    phone = acc["phone"]
    delay_min, delay_max = get_delay(phone)

    stats = {"sent": 0, "skipped": 0, "replied": 0, "errors": 0, "filtered": 0}

    proxy_idx = 0
    proxy_cfg = PROXIES[proxy_idx % len(PROXIES)] if PROXIES else None
    client = make_client(acc, proxy_cfg)
    msg_count_since_proxy = 0

    try:
        await client.connect()
        if not await client.is_user_authorized():
            if acc.get("twoFA"):
                await client.sign_in(password=acc["twoFA"])
            else:
                log(name, "❌ Не авторизован — пропускаем")
                return stats

        log(name, f"🔗 Подключён")

        # SpamBot check (важно для защиты)
        if not await check_spamblock(client, name):
            log(name, "⚠️ SpamBot: ограничен — но пытаемся отправить")
            # не break, продолжаем попытку

        sent_today = get_daily_sent(status, name)
        log(name, f"📊 Отправлено сегодня: {sent_today}/{DAILY_LIMIT_PER_ACCOUNT}")

        sent_this_run = 0

        for phone_target in targets:
            # Daily limit check
            if get_daily_sent(status, name) >= DAILY_LIMIT_PER_ACCOUNT:
                log(name, f"🛑 Дневной лимит {DAILY_LIMIT_PER_ACCOUNT} достигнут — стоп")
                break

            # Working hours check (только логируем, не блокируем)
            if not is_working_hours():
                local = get_local_now()
                log(name, f"🌙 Вне часов ({local.strftime('%H:%M')} UTC+7) — но отправляем anyway")
                # не ждём, продолжаем отправлять

            # Load contact state
            async with global_lock:
                contact = status["contacts"].get(phone_target, {})
                next_stage = get_next_stage(contact)

            if next_stage is None:
                stats["skipped"] += 1
                continue

            # Resolve user
            user = await resolve_user(client, phone_target)
            if user is None:
                log(name, f"❓ {phone_target} — нет в Telegram")
                async with global_lock:
                    status["contacts"].setdefault(phone_target, {})["stopped"] = True
                    status["contacts"][phone_target]["stop_reason"] = "not_found"
                    save_status(status)
                stats["skipped"] += 1
                continue

            # Filter bots
            if getattr(user, "bot", False):
                log(name, f"🤖 {phone_target} — бот, пропускаем")
                async with global_lock:
                    status["contacts"].setdefault(phone_target, {})["stopped"] = True
                    status["contacts"][phone_target]["stop_reason"] = "bot"
                    save_status(status)
                stats["filtered"] += 1
                continue

            # Filter no-photo
            if getattr(user, "photo", None) is None:
                log(name, f"🚫 {phone_target} — нет фото, пропускаем")
                async with global_lock:
                    status["contacts"].setdefault(phone_target, {})["stopped"] = True
                    status["contacts"][phone_target]["stop_reason"] = "no_photo"
                    save_status(status)
                stats["filtered"] += 1
                continue

            first_name = getattr(user, "first_name", "") or ""

            # Check for reply — if replied, stop follow-up and mark as hot lead
            if next_stage > 0 and await has_replied(client, user):
                log(name, f"🔥 {phone_target} ({first_name}) — ОТВЕТИЛ! Горячий лид, стопаем follow-up")
                async with global_lock:
                    status["contacts"].setdefault(phone_target, {})
                    status["contacts"][phone_target]["replied"] = True
                    status["contacts"][phone_target]["reply_detected_at"] = datetime.now(timezone.utc).isoformat()
                    save_status(status)
                stats["replied"] += 1
                continue

            # Pick message
            text = pick_message(next_stage, first_name)

            # Send
            try:
                await send_with_typing(client, user, text)
                log(name, f"✅ {phone_target} ({first_name}) stage={next_stage} → отправлено")

                async with global_lock:
                    c = status["contacts"].setdefault(phone_target, {})
                    c["first_name"] = first_name
                    c.setdefault("sent_stages", {})[str(next_stage)] = datetime.now(timezone.utc).isoformat()
                    c.setdefault("sent_by", {})[str(next_stage)] = name
                    increment_daily(status, name)
                    save_status(status)

                stats["sent"] += 1
                sent_this_run += 1
                msg_count_since_proxy += 1

            except FloodWaitError as e:
                log(name, f"🌊 FloodWait {e.seconds}s — ждём...")
                await asyncio.sleep(e.seconds + 30)
                stats["errors"] += 1
                continue

            except PeerFloodError:
                log(name, "⛔ PeerFlood — спамблок на этом аккаунте, переключаемся...")
                # Не останавливаем, продолжаем (другой аккаунт обработает)
                await asyncio.sleep(5)
                break  # выходим из цикла для этого аккаунта

            except (UserPrivacyRestrictedError, UserIsBlockedError, InputUserDeactivatedError):
                log(name, f"🔒 {phone_target} — недоступен, пропускаем")
                async with global_lock:
                    status["contacts"].setdefault(phone_target, {})["stopped"] = True
                    status["contacts"][phone_target]["stop_reason"] = "privacy_or_blocked"
                    save_status(status)
                stats["skipped"] += 1
                continue

            except (PhoneNumberBannedError, UserDeactivatedBanError):
                log(name, f"💀 Аккаунт ЗАБАНЕН — стоп")
                break

            except (AuthKeyError, AuthKeyUnregisteredError):
                log(name, "🔑 Сессия истекла — стоп")
                break

            except Exception as e:
                log(name, f"❌ {phone_target} — ошибка: {e}")
                stats["errors"] += 1
                continue

            # Proxy rotation
            if PROXIES and msg_count_since_proxy >= PROXY_ROTATE_EVERY:
                proxy_idx += 1
                new_proxy = PROXIES[proxy_idx % len(PROXIES)]
                log(name, f"🔄 Ротация прокси → {new_proxy['host']}")
                await client.disconnect()
                client = make_client(acc, new_proxy)
                await client.connect()
                msg_count_since_proxy = 0

            # Cooldown every N messages
            if sent_this_run > 0 and sent_this_run % COOLDOWN_EVERY_N == 0:
                pause = random.uniform(COOLDOWN_MIN_SEC, COOLDOWN_MAX_SEC)
                log(name, f"☕ Кулдаун после {sent_this_run} сообщений: {pause/60:.0f} мин")
                await asyncio.sleep(pause)
            else:
                # Regular delay
                delay = random.uniform(delay_min, delay_max)
                if random.random() < LONG_PAUSE_CHANCE:
                    extra = random.uniform(*LONG_PAUSE_RANGE)
                    log(name, f"☕ Длинная пауза: {extra/60:.0f} мин")
                    delay += extra
                log(name, f"⏳ Жду {delay/60:.1f} мин...")
                await asyncio.sleep(delay)

            # Random online activity every ~10 sends
            if sent_this_run > 0 and sent_this_run % 10 == 0:
                await random_online_pause(client, name)

        log(name, f"🏁 Готово. Отправлено: {stats['sent']} | Пропущено: {stats['skipped']} | Ошибок: {stats['errors']}")

    except Exception as e:
        log(name, f"💥 Критическая ошибка: {e}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return stats


# ─── WARMUP ───────────────────────────────────────────────────────────────────

async def do_warmup(accounts: list[dict]) -> None:
    print("\n🔥 Warmup: аккаунты пишут друг другу...\n")
    phones = [a["phone"] for a in accounts]
    clients: dict[str, TelegramClient] = {}

    # Connect all clients for warmup
    for acc in accounts:
        c = make_client(acc)
        try:
            await c.connect()
            if await c.is_user_authorized():
                clients[acc["phone"]] = c
        except Exception as e:
            print(f"  ⚠️ {acc['name']}: не подключён для warmup ({e})")

    for acc in accounts:
        client = clients.get(acc["phone"])
        if not client:
            continue
        others = [p for p in phones if p != acc["phone"] and p in clients]
        if not others:
            continue
        target_phone = random.choice(others)
        target_client = clients[target_phone]
        try:
            me = await target_client.get_me()
            phrase = random.choice(WARMUP_PHRASES)
            async with client.action(me, "typing"):
                await asyncio.sleep(random.uniform(2, 4))
            await client.send_message(me, phrase)
            log(acc["name"], f"🔥 Warmup → {target_phone}: {phrase}")
            await asyncio.sleep(random.uniform(20, 60))
        except Exception as e:
            log(acc["name"], f"⚠️ Warmup error: {e}")

    for c in clients.values():
        try:
            await c.disconnect()
        except Exception:
            pass
    print()


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def pick_message(stage: int, first_name: str) -> str:
    templates = TEMPLATES.get(stage, TEMPLATES[0])
    tmpl = random.choice(templates)
    name = first_name.strip() if first_name else ""
    # {_name} → " Alex" with name, or "" without (avoids punctuation problems)
    return tmpl.replace("{_name}", f" {name}" if name else "")


def load_targets() -> list[str]:
    if not os.path.exists(TARGETS_FILE):
        print(f"❌ Файл {TARGETS_FILE} не найден. Создайте его с номерами (по одному на строку).")
        sys.exit(1)
    phones = []
    with open(TARGETS_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                phones.append(line)
    return phones


async def main() -> None:
    print("=" * 55)
    print("ADVANCED TELEGRAM SENDER — АНТИБАН РЕЖИМ")
    print("=" * 55)

    targets = load_targets()
    random.shuffle(targets)  # shuffle to avoid patterns
    print(f"📋 Загружено номеров: {len(targets)}")

    status = load_status()
    print(f"📊 Уже в базе: {len(status.get('contacts', {}))} контактов")

    # Ensure all targets have a contact entry (new ones get empty dict)
    for t in targets:
        status["contacts"].setdefault(t, {})

    # Warmup (быстрый, для прогрева аккаунтов)
    # Skipping warmup - new account causes hanging
    # if len(ACCOUNTS) > 1:
    #     await do_warmup(ACCOUNTS)

    global_lock = asyncio.Lock()

    all_stats = {"sent": 0, "skipped": 0, "replied": 0, "errors": 0, "filtered": 0}

    # Многопроходная отправка — каждый аккаунт пытается отправить ВСЕ контакты
    print(f"\n🚀 АГРЕССИВНЫЙ РЕЖИМ: {len(ACCOUNTS)} аккаунтов × {len(targets)} контактов\n")

    for pass_num in range(3):  # 3 прохода (все аккаунты пытаются отправить всё)
        print(f"\n{'='*60}")
        print(f"📍 ПРОХОД {pass_num + 1}/3 — ВСЕ АККАУНТЫ ОТПРАВЛЯЮТ")
        print(f"{'='*60}\n")

        tasks = [
            run_account(acc, targets, status, global_lock)
            for acc in ACCOUNTS
        ]
        pass_stats = await asyncio.gather(*tasks)

        # Агрегируем статистику
        for s in pass_stats:
            for k in all_stats:
                all_stats[k] += s.get(k, 0)

        # Если всё отправлено, выходим
        remaining_to_send = len([c for c in status.get("contacts", {}).values()
                                 if not c.get("replied") and not c.get("stopped")
                                 and not c.get("sent_stages")])
        if remaining_to_send == 0:
            print(f"\n✅ ВСЕ КОНТАКТЫ ОБРАБОТАНЫ")
            break

        print(f"\n⏳ Осталось отправить: {remaining_to_send} контактов")

    print(f"\n{'='*55}")
    print("ИТОГ РАССЫЛКИ")
    print(f"{'='*55}")
    print(f"✅ Отправлено:  {all_stats['sent']}")
    print(f"🔥 Ответили:    {all_stats['replied']}")
    print(f"⏭️  Пропущено:   {all_stats['skipped']}")
    print(f"🤖 Отфильтровано: {all_stats['filtered']}")
    print(f"❌ Ошибок:     {all_stats['errors']}")

    # Replied hot leads
    hot = [p for p, c in status["contacts"].items() if c.get("replied")]
    if hot:
        print(f"\n🔥 ГОРЯЧИЕ ЛИДЫ ({len(hot)}):")
        for p in hot:
            fn = status["contacts"][p].get("first_name", "")
            print(f"   {p}  {fn}")

    save_report(all_stats)
    save_status(status)


if __name__ == "__main__":
    asyncio.run(main())
