"""
test_reply_monitor.py — проверяет reply rate monitoring без WhatsApp и без реального PostgreSQL.
Использует SQLite in-memory — работает везде, ничего не оставляет в БД.

Режимы запуска:
  python3 test_reply_monitor.py          # запускает все сценарии на SQLite in-memory
  python3 test_reply_monitor.py --live   # читает РЕАЛЬНЫЕ данные из вашей PostgreSQL
"""

import asyncio
import sys
import os

# ── Заглушки до импорта приложения ───────────────────────────────────────────
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
os.environ.setdefault("API_SECRET_KEY", "test")
os.environ.setdefault("APP_HOST",       "0.0.0.0")
os.environ.setdefault("APP_PORT",       "8000")

from dotenv import load_dotenv
load_dotenv(override=False)   # не перезаписывать заглушки выше

sys.path.insert(0, ".")

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Base, Campaign, Conversation, WhatsAppInstance, WhatsAppMessage
from app.services.reply_monitor import (
    REPLY_RATE_DANGER,
    REPLY_RATE_LOOKBACK_DAYS,
    REPLY_RATE_WARNING,
    classify_reply_rate,
    get_all_reply_rates,
    get_reply_rate,
)

# ── ANSI colours ─────────────────────────────────────────────────────────────
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
CYAN   = "\033[36m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

# ── Seed scenarios ────────────────────────────────────────────────────────────
# (name, sent_conversations, replied_conversations)
SEED_SCENARIOS = [
    ("inst_OK_50pct",     40, 20),   # 50%  → ok
    ("inst_OK_35pct",     40, 14),   # 35%  → ok (barely)
    ("inst_WARN_25pct",   40, 10),   # 25%  → warning
    ("inst_WARN_16pct",   40,  7),   # ~17% → warning
    ("inst_DANGER_10pct", 40,  4),   # 10%  → danger
    ("inst_DANGER_0pct",  40,  0),   # 0%   → danger
    ("inst_IDLE_nodata",   0,  0),   # —    → no_data
]


# ── SQLite in-memory engine ───────────────────────────────────────────────────

def _make_sqlite_session() -> async_sessionmaker:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False), engine


# ── Display ───────────────────────────────────────────────────────────────────

def _bar(rate: Optional[float], width: int = 28) -> str:
    if rate is None:
        return DIM + "─" * width + RESET
    filled = round(rate * width)
    color = GREEN if rate >= REPLY_RATE_WARNING else (YELLOW if rate >= REPLY_RATE_DANGER else RED)
    return color + "█" * filled + DIM + "░" * (width - filled) + RESET


def _status_icon(status: str) -> str:
    return {
        "ok":      f"{GREEN}✓ OK     {RESET}",
        "warning": f"{YELLOW}⚠ WARNING{RESET}",
        "danger":  f"{RED}⛔ DANGER {RESET}",
        "no_data": f"{DIM}– no data{RESET}",
    }.get(status, status)


def _print_rates(rates: dict, title: str = "Reply Rate Report") -> None:
    print(f"\n{'═' * 72}")
    print(f"{BOLD}  {title}  (last {REPLY_RATE_LOOKBACK_DAYS} days){RESET}")
    print(f"  Thresholds:  "
          f"{GREEN}OK ≥ {int(REPLY_RATE_WARNING*100)}%{RESET}  "
          f"{YELLOW}WARNING ≥ {int(REPLY_RATE_DANGER*100)}%{RESET}  "
          f"{RED}DANGER < {int(REPLY_RATE_DANGER*100)}%{RESET}")
    print(f"{'─' * 72}")

    if not rates:
        print(f"  {DIM}Нет данных. Запустите скрипт ещё раз после отправки сообщений.{RESET}")
        return

    order = {"danger": 0, "warning": 1, "ok": 2, "no_data": 3}
    sorted_items = sorted(rates.items(), key=lambda kv: order.get(classify_reply_rate(kv[1]), 9))

    for inst_id, rate in sorted_items:
        status = classify_reply_rate(rate)
        pct    = f"{rate*100:5.1f}%" if rate is not None else "   n/a"
        print(f"  {CYAN}{inst_id:<28}{RESET}  {pct}  {_bar(rate)}  {_status_icon(status)}")

    statuses = [classify_reply_rate(r) for r in rates.values()]
    n_ok      = statuses.count("ok")
    n_warn    = statuses.count("warning")
    n_danger  = statuses.count("danger")
    n_nodata  = statuses.count("no_data")

    print(f"{'─' * 72}")
    print(f"  {BOLD}Итого:{RESET}  "
          f"{GREEN}OK: {n_ok}{RESET}  "
          f"{YELLOW}Warning: {n_warn}{RESET}  "
          f"{RED}Danger: {n_danger}{RESET}  "
          f"{DIM}No data: {n_nodata}{RESET}")

    if n_danger:
        print(f"\n  {RED}{BOLD}⛔  {n_danger} инстанс(а) в DANGER — срочно снизить объём!{RESET}")
    elif n_warn:
        print(f"\n  {YELLOW}{BOLD}⚠   {n_warn} инстанс(а) в WARNING — следить внимательно.{RESET}")
    else:
        print(f"\n  {GREEN}{BOLD}✓  Все инстансы в безопасной зоне.{RESET}")
    print()


# ── Seed ──────────────────────────────────────────────────────────────────────

async def _seed(db: AsyncSession) -> None:
    now = datetime.now(timezone.utc)

    campaign = Campaign(external_id="test", name="[test]", agent_id="test-agent")
    db.add(campaign)
    await db.flush()

    for inst_name, n_sent, n_replied in SEED_SCENARIOS:
        inst = WhatsAppInstance(
            name=inst_name,
            instance_id=inst_name,
            api_token="test-token",
            phone_number="000",
            is_active=True,
            is_banned=False,
            health_status="authorized",
        )
        db.add(inst)
        await db.flush()

        conv_ids = []
        # Use inst_idx to keep phones globally unique across all scenarios
        inst_idx = [s[0] for s in SEED_SCENARIOS].index(inst_name)
        for i in range(n_sent):
            conv = Conversation(
                campaign_id=campaign.id,
                lead_id=f"{inst_name}_{i}",
                phone=f"+{inst_idx:02d}{i:05d}",
                status="active",
            )
            db.add(conv)
            await db.flush()
            conv_ids.append(conv.id)

            db.add(WhatsAppMessage(
                conversation_id=conv.id,
                instance_id=inst.id,
                direction="outbound",
                body="Test outreach",
                status="sent",
                created_at=now - timedelta(hours=2),
            ))

        for i in range(min(n_replied, len(conv_ids))):
            db.add(WhatsAppMessage(
                conversation_id=conv_ids[i],
                instance_id=None,        # inbound — как в реальных данных
                direction="inbound",
                body="Test reply",
                status="received",
                created_at=now - timedelta(hours=1),
            ))

    await db.commit()


# ── Validation ────────────────────────────────────────────────────────────────

def _validate(rates: dict) -> bool:
    print(f"\n{BOLD}── Проверка сценариев ──{RESET}")
    all_pass = True

    for inst_name, n_sent, n_replied in SEED_SCENARIOS:
        exp_rate   = (n_replied / n_sent) if n_sent > 0 else None
        exp_status = classify_reply_rate(exp_rate)
        got_rate   = rates.get(inst_name)
        got_status = classify_reply_rate(got_rate)

        rate_ok   = (got_rate is None and exp_rate is None) or (
            got_rate is not None and exp_rate is not None and abs(got_rate - exp_rate) < 0.001
        )
        ok = rate_ok and got_status == exp_status
        icon = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"

        exp_pct = f"{exp_rate*100:.1f}%" if exp_rate is not None else "None"
        got_pct = f"{got_rate*100:.1f}%" if got_rate is not None else "None"
        print(f"  {icon}  {inst_name:<24}  "
              f"ожидалось {exp_pct:>6} ({exp_status:<8})  "
              f"получено {got_pct:>6} ({got_status})")
        if not ok:
            all_pass = False

    print()
    if all_pass:
        print(f"  {GREEN}{BOLD}✓ Все {len(SEED_SCENARIOS)} сценариев прошли!{RESET}")
    else:
        print(f"  {RED}{BOLD}✗ Есть ошибки — проверь логику запросов.{RESET}")
    print()
    return all_pass


# ── Live mode (реальная БД) ───────────────────────────────────────────────────

async def run_live() -> None:
    from app.db.session import AsyncSessionLocal, init_db
    print(f"\n{BOLD}=== Reply Rate Monitor — LIVE (реальная БД) ==={RESET}")
    try:
        await init_db()
        async with AsyncSessionLocal() as db:
            rates = await get_all_reply_rates(db)
        _print_rates(rates, "Reply Rate Report (prod)")
    except Exception as e:
        print(f"\n  {RED}Ошибка подключения к БД: {e}{RESET}")
        print(f"  {DIM}Убедись что PostgreSQL запущен и DATABASE_URL в .env корректный.{RESET}")
        print(f"  {DIM}Для теста без БД запусти без флагов: python3 test_reply_monitor.py{RESET}\n")


# ── Test mode (SQLite in-memory) ──────────────────────────────────────────────

async def run_test() -> None:
    print(f"\n{BOLD}=== Reply Rate Monitor Test (SQLite in-memory) ==={RESET}")
    print(f"{DIM}База данных: временная, в памяти. Ничего не записывается в реальную БД.{RESET}")

    SessionLocal, engine = _make_sqlite_session()

    # Создаём таблицы в SQLite
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as db:
        print(f"\n{BOLD}Генерирую {len(SEED_SCENARIOS)} тестовых инстансов...{RESET}")
        await _seed(db)
        print(f"  Готово.\n")

        # Общий отчёт
        rates = await get_all_reply_rates(db)
        _print_rates(rates, "Reply Rate Report (test data)")

        # Проверка сценариев
        passed = _validate(rates)

        # Проверка get_reply_rate (per-instance)
        print(f"{BOLD}── Проверка get_reply_rate() (per-instance) ──{RESET}")
        for inst_name, n_sent, n_replied in SEED_SCENARIOS:
            rate = await get_reply_rate(db, inst_name)
            status = classify_reply_rate(rate)
            pct = f"{rate*100:.1f}%" if rate is not None else "None"
            print(f"  {CYAN}{inst_name:<28}{RESET}  rate={pct:<7}  status={_status_icon(status)}")
        print()

    await engine.dispose()

    if passed:
        print(f"{GREEN}{BOLD}✅  Тест пройден — reply monitor работает корректно.{RESET}\n")
    else:
        print(f"{RED}{BOLD}❌  Тест не пройден — есть баги в логике.{RESET}\n")
        sys.exit(1)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--live" in sys.argv:
        asyncio.run(run_live())
    else:
        asyncio.run(run_test())
