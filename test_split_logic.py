"""
Test split_message() using the exact same production function used in WhatsApp sends.

Run: python test_split_logic.py
"""

import sys
sys.path.insert(0, ".")

from app.services.sender import split_message

CASES = [
    {
        "desc": "Full message — promo+link already in body",
        "ai_message": (
            "Olá! Tenho uma oferta especial para ti hoje.\n"
            "Usa o teu código exclusivo e recebe 50 Free Spins no depósito.\n"
            "O link ativa assim que responderes qualquer coisa 🎰\n"
            "PROMO123\n"
            "https://oro.casino/ref/abc"
        ),
        "promo": "PROMO123",
        "link": "https://oro.casino/ref/abc",
    },
    {
        "desc": "Spanish — no promo, link not in body",
        "ai_message": (
            "¡Hola! Tenemos algo especial para ti hoy. "
            "Recibirás 50 giros gratis con tu primer depósito. "
            "¡Es muy fácil participar!"
        ),
        "promo": "",
        "link": "https://pampas.casino/ref/xyz",
    },
    {
        "desc": "Very short message",
        "ai_message": "Olá!",
        "promo": "BONUS50",
        "link": "https://oro.casino/ref/test",
    },
    {
        "desc": "Multi-sentence body, no promo",
        "ai_message": (
            "Olá! Vi que tens interesse no nosso casino. "
            "Temos uma promoção exclusiva só para ti esta semana. "
            "Podes aproveitar até 200€ de bónus no primeiro depósito. "
            "É muito fácil de activar, basta registares-te."
        ),
        "promo": "",
        "link": "https://oro.casino/ref/multi",
    },
    {
        "desc": "Empty promo and empty link — part3 falls back to part2",
        "ai_message": "Aqui está a tua resposta. Espero que ajude!",
        "promo": "",
        "link": "",
    },
]

all_ok = True
for c in CASES:
    p1, p2, p3 = split_message(c["ai_message"], c["promo"], c["link"])
    ok = True
    errors = []

    # Link must NOT appear in part1 or part2 (only in part3)
    if c["link"] and c["link"] in p1:
        errors.append(f"Link leaked into part 1: {p1!r}")
        ok = False
    if c["link"] and c["link"] in p2:
        errors.append(f"Link leaked into part 2: {p2!r}")
        ok = False
    # Link MUST appear in part3 (when provided)
    if c["link"] and c["link"] not in p3:
        errors.append(f"Link missing from part 3: {p3!r}")
        ok = False

    # Promo must NOT appear in part1 or part2
    if c["promo"] and c["promo"] in p1:
        errors.append(f"Promo leaked into part 1: {p1!r}")
        ok = False
    if c["promo"] and c["promo"] in p2:
        errors.append(f"Promo leaked into part 2: {p2!r}")
        ok = False
    # Promo MUST appear in part3 (when provided)
    if c["promo"] and c["promo"] not in p3:
        errors.append(f"Promo missing from part 3: {p3!r}")
        ok = False

    # All parts must be non-empty strings
    for idx, p in enumerate([p1, p2, p3], 1):
        if not isinstance(p, str) or not p.strip():
            errors.append(f"Part {idx} is empty or not a string: {p!r}")
            ok = False

    status = "OK" if ok else "FAIL"
    print(f"\n=== [{status}] {c['desc']} ===")
    print(f"  Part 1: {p1!r}")
    print(f"  Part 2: {p2!r}")
    print(f"  Part 3: {p3!r}")
    for err in errors:
        print(f"  ERROR: {err}")
    if not ok:
        all_ok = False

print()
if all_ok:
    print("All tests passed.")
else:
    print("Some tests FAILED — check errors above.")
    sys.exit(1)
