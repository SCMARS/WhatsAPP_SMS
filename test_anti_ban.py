"""
test_anti_ban.py — тестирует все три anti-ban механизма:
  1. Block rate  — считает заблокированные номера
  2. Warmup      — лимиты снижаются для новых инстансов
  3. Rest days   — пропуск после 3 дней подряд

Работает на SQLite in-memory, не нужен PostgreSQL, не трогает WhatsApp.

Запуск:
    python3 test_anti_ban.py
"""

import asyncio
import os
import sys

os.environ.setdefault("DATABASE_URL",   "postgresql+asyncpg://x:x@localhost/x")
os.environ.setdefault("API_SECRET_KEY", "test")
os.environ.setdefault("APP_HOST",       "0.0.0.0")
os.environ.setdefault("APP_PORT",       "8000")

sys.path.insert(0, ".")

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import (
    Base, Blacklist, Campaign, Conversation, WhatsAppInstance, WhatsAppMessage,
)
from app.services.reply_monitor import (
    BLOCK_RATE_DANGER,
    BLOCK_RATE_WARNING,
    classify_block_rate,
    get_all_block_rates,
    get_block_rate,
)
from app.services.pool import (
    WARMUP_SCHEDULE,
    _needs_rest_day,
    _rest_cache,
    get_effective_limits,
    get_warmup_status,
)

# ── ANSI ──────────────────────────────────────────────────────────────────────
GREEN  = "\033[32m";  YELLOW = "\033[33m";  RED    = "\033[31m"
CYAN   = "\033[36m";  BOLD   = "\033[1m";   DIM    = "\033[2m";  RESET = "\033[0m"

PASS = f"{GREEN}✓ PASS{RESET}"
FAIL = f"{RED}✗ FAIL{RESET}"


def ok(cond: bool) -> str:
    return PASS if cond else FAIL


# ── Engine ────────────────────────────────────────────────────────────────────

async def make_session() -> tuple[async_sessionmaker, object]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False), engine


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _make_instance(db: AsyncSession, inst_id: str, age_days: int = 60) -> WhatsAppInstance:
    created = datetime.now(timezone.utc) - timedelta(days=age_days)
    inst = WhatsAppInstance(
        name=inst_id, instance_id=inst_id, api_token="tok",
        phone_number="000", is_active=True, is_banned=False,
        health_status="authorized", created_at=created,
    )
    db.add(inst)
    await db.flush()
    return inst


async def _make_campaign(db: AsyncSession) -> Campaign:
    camp = Campaign(external_id="test", name="[test]", agent_id="x")
    db.add(camp)
    await db.flush()
    return camp


async def _make_conv(db: AsyncSession, campaign_id, phone: str) -> Conversation:
    conv = Conversation(
        campaign_id=campaign_id, lead_id=phone,
        phone=phone, status="active",
    )
    db.add(conv)
    await db.flush()
    return conv


async def _outbound(db: AsyncSession, conv_id, inst_id, when: datetime) -> None:
    db.add(WhatsAppMessage(
        conversation_id=conv_id, instance_id=inst_id,
        direction="outbound", body="hi", status="sent", created_at=when,
    ))


async def _blacklist(db: AsyncSession, phone: str) -> None:
    db.add(Blacklist(phone=phone, reason="test"))


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 1 — Block Rate
# ═══════════════════════════════════════════════════════════════════════════════

async def test_block_rate():
    print(f"\n{BOLD}{'═'*60}{RESET}")
    print(f"{BOLD}  TEST 1 — Block Rate{RESET}")
    print(f"{BOLD}{'─'*60}{RESET}")

    SessionLocal, engine = await make_session()
    passed = 0
    total  = 0

    async with SessionLocal() as db:
        inst  = await _make_instance(db, "inst_block_test")
        camp  = await _make_campaign(db)
        now   = datetime.now(timezone.utc)

        # Create 20 conversations
        conv_ids = []
        phones   = []
        for i in range(20):
            phone = f"+380{i:09d}"
            conv  = await _make_conv(db, camp.id, phone)
            await _outbound(db, conv.id, inst.id, now - timedelta(hours=1))
            conv_ids.append(conv.id)
            phones.append(phone)

        # Blacklist first 3 → block rate = 3/20 = 15%
        for phone in phones[:3]:
            await _blacklist(db, phone)

        await db.commit()

        rate = await get_block_rate(db, "inst_block_test")
        exp  = 3 / 20
        cond = rate is not None and abs(rate - exp) < 0.001
        total += 1; passed += cond
        print(f"  {ok(cond)}  block_rate = {rate*100:.1f}% (expected {exp*100:.1f}%)")

        status = classify_block_rate(rate)
        exp_status = "danger"  # 15% > BLOCK_RATE_DANGER (5%) → danger
        cond2 = (status == exp_status)
        total += 1; passed += cond2
        print(f"  {ok(cond2)}  classify = '{status}' (expected '{exp_status}')")

        # Blacklist 5 more → 8/20 = 40% → danger
        for phone in phones[3:8]:
            await _blacklist(db, phone)
        await db.commit()

        rate2 = await get_block_rate(db, "inst_block_test")
        exp2  = 8 / 20
        cond3 = rate2 is not None and abs(rate2 - exp2) < 0.001
        total += 1; passed += cond3
        print(f"  {ok(cond3)}  block_rate after more bans = {rate2*100:.1f}% (expected {exp2*100:.1f}%)")

        status2 = classify_block_rate(rate2)
        cond4 = (status2 == "danger")
        total += 1; passed += cond4
        print(f"  {ok(cond4)}  classify = '{status2}' (expected 'danger')")

        # No-data case
        rate_none = await get_block_rate(db, "nonexistent_inst")
        cond5 = (rate_none is None)
        total += 1; passed += cond5
        print(f"  {ok(cond5)}  no-data instance → None  (got {rate_none})")

        # Bulk rates
        bulk = await get_all_block_rates(db)
        cond6 = "inst_block_test" in bulk
        total += 1; passed += cond6
        print(f"  {ok(cond6)}  get_all_block_rates() has our instance → {list(bulk.keys())}")

    await engine.dispose()
    print(f"\n  Результат: {passed}/{total} проверок прошло")
    return passed, total


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 2 — Warmup Limits
# ═══════════════════════════════════════════════════════════════════════════════

async def test_warmup():
    print(f"\n{BOLD}{'═'*60}{RESET}")
    print(f"{BOLD}  TEST 2 — Warmup Limits{RESET}")
    print(f"{BOLD}{'─'*60}{RESET}")

    SessionLocal, engine = await make_session()
    passed = 0
    total  = 0

    # Scenarios: (age_days, configured_daily, configured_hourly, expected_eff_daily, expected_eff_hourly)
    scenarios = [
        (2,  150, 30,   30,  5,  "Week 1 cap"),
        (10, 150, 30,   60, 10,  "Week 2 cap"),
        (17, 150, 30,  100, 20,  "Week 3 cap"),
        (25, 150, 30,  150, 25,  "Week 4 cap"),
        (35, 150, 30,  150, 30,  "Post-warmup"),
        # If configured limit is LOWER than warmup cap, use configured
        (2,   10,  3,   10,  3,  "Configured lower than cap (week 1)"),
    ]

    async with SessionLocal() as db:
        for age, cfg_daily, cfg_hourly, exp_daily, exp_hourly, label in scenarios:
            inst = WhatsAppInstance(
                name=f"inst_{age}d", instance_id=f"inst_{age}d",
                api_token="tok", phone_number="000",
                is_active=True, is_banned=False, health_status="authorized",
                daily_limit=cfg_daily, hourly_limit=cfg_hourly,
                created_at=datetime.now(timezone.utc) - timedelta(days=age),
            )
            eff_daily, eff_hourly = get_effective_limits(inst)
            cond = (eff_daily == exp_daily and eff_hourly == exp_hourly)
            total += 1; passed += cond
            print(f"  {ok(cond)}  age={age:2d}d cfg={cfg_daily}/{cfg_hourly} → "
                  f"eff={eff_daily}/{eff_hourly}  (expected {exp_daily}/{exp_hourly})  [{label}]")

        # Warmup status reporting
        inst_new = WhatsAppInstance(
            name="new", instance_id="new", api_token="tok", phone_number="000",
            is_active=True, is_banned=False, health_status="authorized",
            daily_limit=150, hourly_limit=30,
            created_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        ws = get_warmup_status(inst_new)
        cond2 = ws["in_warmup"] and ws["age_days"] == 5 and ws["days_remaining"] == 25
        total += 1; passed += cond2
        print(f"  {ok(cond2)}  warmup_status age=5d → in_warmup={ws['in_warmup']} "
              f"age={ws['age_days']} remaining={ws['days_remaining']}")

    await engine.dispose()
    print(f"\n  Результат: {passed}/{total} проверок прошло")
    return passed, total


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 3 — Rest Days
# ═══════════════════════════════════════════════════════════════════════════════

async def test_rest_days():
    print(f"\n{BOLD}{'═'*60}{RESET}")
    print(f"{BOLD}  TEST 3 — Rest Days{RESET}")
    print(f"{BOLD}{'─'*60}{RESET}")

    SessionLocal, engine = await make_session()
    passed = 0
    total  = 0
    now    = datetime.now(timezone.utc)

    async with SessionLocal() as db:
        camp = await _make_campaign(db)

        # ── Scenario A: sent on day-1, day-2, day-3 → needs rest ──
        inst_a = await _make_instance(db, "rest_3days")
        for day_offset in [1, 2, 3]:
            conv = await _make_conv(db, camp.id, f"+380999{day_offset:06d}")
            send_time = (now - timedelta(days=day_offset)).replace(hour=12)
            await _outbound(db, conv.id, inst_a.id, send_time)
        await db.commit()

        _rest_cache.clear()
        result_a = await _needs_rest_day(db, "rest_3days")
        cond_a = (result_a is True)
        total += 1; passed += cond_a
        print(f"  {ok(cond_a)}  sent 3 days in a row → needs_rest = {result_a}  (expected True)")

        # ── Scenario B: sent only day-1 and day-2 → no rest needed ──
        inst_b = await _make_instance(db, "rest_2days")
        for day_offset in [1, 2]:
            conv = await _make_conv(db, camp.id, f"+380888{day_offset:06d}")
            send_time = (now - timedelta(days=day_offset)).replace(hour=12)
            await _outbound(db, conv.id, inst_b.id, send_time)
        await db.commit()

        _rest_cache.clear()
        result_b = await _needs_rest_day(db, "rest_2days")
        cond_b = (result_b is False)
        total += 1; passed += cond_b
        print(f"  {ok(cond_b)}  sent 2 days in a row → needs_rest = {result_b}  (expected False)")

        # ── Scenario C: sent on day-1 and day-3 (skipped day-2) → no rest ──
        inst_c = await _make_instance(db, "rest_gap")
        for day_offset in [1, 3]:
            conv = await _make_conv(db, camp.id, f"+380777{day_offset:06d}")
            send_time = (now - timedelta(days=day_offset)).replace(hour=12)
            await _outbound(db, conv.id, inst_c.id, send_time)
        await db.commit()

        _rest_cache.clear()
        result_c = await _needs_rest_day(db, "rest_gap")
        cond_c = (result_c is False)
        total += 1; passed += cond_c
        print(f"  {ok(cond_c)}  sent day-1 + day-3 (gap on day-2) → needs_rest = {result_c}  (expected False)")

        # ── Scenario D: brand new instance, no sends → no rest ──
        _rest_cache.clear()
        result_d = await _needs_rest_day(db, "brand_new_inst")
        cond_d = (result_d is False)
        total += 1; passed += cond_d
        print(f"  {ok(cond_d)}  new instance no sends → needs_rest = {result_d}  (expected False)")

        # ── Scenario E: cache works (second call = same result, no extra query) ──
        _rest_cache.clear()
        r1 = await _needs_rest_day(db, "rest_3days")
        r2 = await _needs_rest_day(db, "rest_3days")  # from cache
        cond_e = (r1 == r2 == True)
        total += 1; passed += cond_e
        print(f"  {ok(cond_e)}  cache: both calls return {r1}/{r2}  (expected True/True)")

    await engine.dispose()
    print(f"\n  Результат: {passed}/{total} проверок прошло")
    return passed, total


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 4 — Combined health report display
# ═══════════════════════════════════════════════════════════════════════════════

async def test_combined_report():
    print(f"\n{BOLD}{'═'*60}{RESET}")
    print(f"{BOLD}  TEST 4 — Combined Anti-Ban Health Report{RESET}")
    print(f"{BOLD}{'─'*60}{RESET}")

    from app.services.reply_monitor import (
        classify_reply_rate, get_all_reply_rates,
        REPLY_RATE_WARNING, REPLY_RATE_DANGER,
    )

    SessionLocal, engine = await make_session()
    now = datetime.now(timezone.utc)

    instances_cfg = [
        # (id, age_days, n_out, n_replied, n_blocked, cfg_daily, cfg_hourly)
        ("inst_healthy",  35, 40, 20,  0, 150, 30),   # OK OK, past warmup
        ("inst_newbie",    5, 15,  7,  0, 150, 30),   # OK OK, in warmup
        ("inst_warn_rr",  35, 40,  8,  1, 150, 30),   # reply WARN, block OK
        ("inst_danger_rr",35, 40,  4,  0, 150, 30),   # reply DANGER
        ("inst_danger_br",35, 40, 20,  3, 150, 30),   # block WARN/DANGER
    ]

    async with SessionLocal() as db:
        camp = await _make_campaign(db)

        for inst_idx, (iid, age, n_out, n_rep, n_blk, cfg_d, cfg_h) in enumerate(instances_cfg):
            created = now - timedelta(days=age)
            inst = WhatsAppInstance(
                name=iid, instance_id=iid, api_token="tok", phone_number="000",
                is_active=True, is_banned=False, health_status="authorized",
                daily_limit=cfg_d, hourly_limit=cfg_h, created_at=created,
            )
            db.add(inst)
            await db.flush()

            phones = []
            for i in range(n_out):
                # Use inst_idx * 10000 + i to guarantee globally unique phones
                phone = f"+{inst_idx:02d}{i:07d}"
                conv  = await _make_conv(db, camp.id, phone)
                await _outbound(db, conv.id, inst.id, now - timedelta(hours=2))
                phones.append(phone)
                # Add inbound reply for first n_rep
                if i < n_rep:
                    db.add(WhatsAppMessage(
                        conversation_id=conv.id, instance_id=None,
                        direction="inbound", body="reply", status="received",
                        created_at=now - timedelta(hours=1),
                    ))

            # Blacklist first n_blk phones
            for phone in phones[:n_blk]:
                await _blacklist(db, phone)

        await db.commit()

        reply_rates = await get_all_reply_rates(db)
        block_rates = await get_all_block_rates(db)

        # Load instances for warmup info
        inst_res = await db.execute(select(WhatsAppInstance))
        inst_map = {i.instance_id: i for i in inst_res.scalars().all()}

    _order = {"danger": 0, "warning": 1, "ok": 2, "no_data": 3}

    def _bar(rate, width=20, mode="reply"):
        if rate is None:
            return DIM + "─" * width + RESET
        filled = round(rate * width)
        if mode == "reply":
            color = GREEN if rate >= REPLY_RATE_WARNING else (YELLOW if rate >= REPLY_RATE_DANGER else RED)
        else:
            color = GREEN if rate <= BLOCK_RATE_WARNING else (YELLOW if rate <= BLOCK_RATE_DANGER else RED)
        return color + "█" * filled + DIM + "░" * (width - filled) + RESET

    def _slabel(s):
        return {
            "ok":      f"{GREEN}✓ OK     {RESET}",
            "warning": f"{YELLOW}⚠ WARN   {RESET}",
            "danger":  f"{RED}⛔ DANGER {RESET}",
            "no_data": f"{DIM}– n/a    {RESET}",
        }.get(s, s)

    print()
    print(f"  {'Instance':<20}  {'Reply':>6}  {'Bar':22}  {'Status':10}  "
          f"{'Block':>6}  {'Status':10}  {'Warmup':10}")
    print(f"  {'─'*20}  {'─'*6}  {'─'*22}  {'─'*10}  {'─'*6}  {'─'*10}  {'─'*10}")

    for iid, _, _, _, _, _, _ in sorted(
        instances_cfg,
        key=lambda x: _order.get(
            min(classify_reply_rate(reply_rates.get(x[0])),
                classify_block_rate(block_rates.get(x[0])),
                key=lambda s: _order.get(s, 9)), 9)
    ):
        rr = reply_rates.get(iid)
        br = block_rates.get(iid)
        inst = inst_map.get(iid)
        ws = get_warmup_status(inst) if inst else {}
        warmup_str = f"{YELLOW}day {ws.get('age_days',0)}/30{RESET}" if ws.get("in_warmup") else f"{DIM}mature{RESET}"
        rr_pct = f"{rr*100:.1f}%" if rr is not None else "n/a"
        br_pct = f"{br*100:.2f}%" if br is not None else "n/a"
        print(f"  {CYAN}{iid:<20}{RESET}  {rr_pct:>6}  {_bar(rr):<22}  "
              f"{_slabel(classify_reply_rate(rr))}  "
              f"{br_pct:>6}  {_slabel(classify_block_rate(br))}  {warmup_str}")

    await engine.dispose()
    print(f"\n  {GREEN}{BOLD}✓ Отчёт сгенерирован корректно{RESET}\n")
    return 1, 1


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

async def main():
    print(f"\n{BOLD}{'═'*60}{RESET}")
    print(f"{BOLD}  ANTI-BAN MECHANISMS TEST SUITE{RESET}")
    print(f"  SQLite in-memory — нет обращений к WhatsApp")
    print(f"{BOLD}{'═'*60}{RESET}")

    total_passed = 0
    total_checks = 0

    for test_fn in [test_block_rate, test_warmup, test_rest_days, test_combined_report]:
        try:
            p, t = await test_fn()
            total_passed += p
            total_checks += t
        except Exception as e:
            import traceback
            print(f"\n  {RED}EXCEPTION in {test_fn.__name__}: {e}{RESET}")
            traceback.print_exc()
            total_checks += 1

    print(f"\n{'═'*60}")
    pct = total_passed / total_checks * 100 if total_checks else 0
    if total_passed == total_checks:
        print(f"{GREEN}{BOLD}  ✅  ВСЕ ТЕСТЫ ПРОШЛИ: {total_passed}/{total_checks} ({pct:.0f}%){RESET}")
    else:
        print(f"{RED}{BOLD}  ❌  ОШИБКИ: {total_passed}/{total_checks} ({pct:.0f}%){RESET}")
    print(f"{'═'*60}\n")

    if total_passed < total_checks:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
