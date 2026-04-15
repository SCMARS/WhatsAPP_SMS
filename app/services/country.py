"""
Country detection by phone prefix.
Determines routing campaign and ElevenLabs language from phone number.
"""

COUNTRY_MAP = {
    # Portugal
    "351": {
        "code": "PT",
        "name": "Portugal",
        "campaign": "portugal",
        "lang": "pt",
        "promo": "50Pragmatic",
    },
    # Argentina
    "54": {
        "code": "AR",
        "name": "Argentina",
        "campaign": "argentina",
        "lang": "es",
        "promo": None,
    },
}

# Default fallback if country not recognized
DEFAULT_COUNTRY = {
    "code": "XX",
    "name": "Unknown",
    "campaign": "default",
    "lang": "en",
    "promo": None,
}


def detect_country(phone: str) -> dict:
    """
    Detect country from phone number.
    Accepts any format: +351912345678, 351912345678, 00351912345678.
    Returns dict with: code, name, campaign, lang, promo.
    """
    # Strip all non-digits
    digits = "".join(c for c in phone if c.isdigit())

    # Remove leading 00 (international prefix)
    if digits.startswith("00"):
        digits = digits[2:]

    # Try longest prefix first (to avoid +5 matching +54)
    for prefix in sorted(COUNTRY_MAP.keys(), key=len, reverse=True):
        if digits.startswith(prefix):
            return COUNTRY_MAP[prefix].copy()

    return DEFAULT_COUNTRY.copy()
