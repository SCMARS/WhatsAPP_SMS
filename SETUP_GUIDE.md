# Setup Guide: Oro Casino (PT) + Pampas (AR) Production Setup

## What Changed

### Code Changes (app/services/elevenlabs.py)

1. **Fixed duplicate clickability trigger** (lines 208-212)
   - Old: Always appended `_clickability_trigger()` to every message
   - New: Only appends if the AI message doesn't already contain "emoji"
   - Why: ElevenLabs agents now include clickability instruction in their output

2. **Updated `_clickability_trigger()` function** (lines 116-127)
   - Old: Short phrases like "Podes responder com um emoji..."
   - New: Longer, branded phrases matching ElevenLabs style ("O link ficará clicável se enviares... Boa sorte 🤞")
   - Why: Consistent with the new agent prompt style

3. **Updated `_fallback_outreach()` function** (lines 213-227)
   - Old: Generic template without personality ("Ola! Sou a Camila do Oro Casino. Tens 50 Rodadas Gratis...")
   - New: Branded with emojis and proper EU PT/AR ES ("Olá! Sou a Camila do Oro Casino 🙂 Foi um prazer falar contigo...")
   - Why: If ElevenLabs fails 4× in a row, fallback looks professional

---

## ElevenLabs Agent Setup

You have TWO separate agents configured:

### Agent 1: Oro Casino (Portugal) — Camila
- **Agent ID**: `agent_6901knmsm0cpfw39pzd84f33dwzp`
- **Language**: European Portuguese (pt-PT)
- **Persona**: Camila, friendly personal manager at Oro Casino
- **Offer**: Promo code {promo} → 50 Free Spins in Pragmatic Play, valid 5 days
- **Key requirements** (from agent prompt):
  - Use "tu/teu", NEVER "você"
  - Every message MUST be worded differently
  - Warm, personal, casual tone
  - 3-5 lines max, max 2 emoji
  - Always include {link}
  - Always include {promo}
  - Mention link activates on reply (even emoji)
  - Vary good luck phrase

### Agent 2: Pampas Casino (Argentina) — Olivia
- **Agent ID**: `agent_7101kp8jz5wnej79qrsz80mtk636`
- **Language**: Argentine Spanish (es-AR)
- **Persona**: Olivia, friendly personal manager at Pampas Casino
- **Offer**: 175% bonus on next deposit from ARS 5000, valid 5 days
- **Key requirements** (from agent prompt):
  - Use "vos" instead of "tú" (Argentine voseo)
  - Every message MUST be worded differently
  - Warm, casual, friendly tone
  - 3-5 lines max, max 2 emoji
  - Always include {link}
  - No {promo} for Pampas (bonus is fixed)
  - Mention link activates on reply (even emoji)
  - Vary good luck phrase

---

## Database Setup

You need campaigns configured in the database. Create them like this:

### Option 1: API (if you have the endpoint)
```bash
# Create Portugal campaign
curl -X POST http://localhost:8000/api/campaigns \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "external_id": "portugal",
    "name": "Oro Casino - Portugal",
    "agent_id": "agent_6901knmsm0cpfw39pzd84f33dwzp"
  }'

# Create Argentina campaign
curl -X POST http://localhost:8000/api/campaigns \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "external_id": "argentina",
    "name": "Pampas Casino - Argentina",
    "agent_id": "agent_7101kp8jz5wnej79qrsz80mtk636"
  }'
```

### Option 2: Direct SQL (if you have database access)
```sql
INSERT INTO campaigns (id, external_id, name, agent_id, is_active, created_at)
VALUES
  (
    gen_random_uuid(),
    'portugal',
    'Oro Casino - Portugal',
    'agent_6901knmsm0cpfw39pzd84f33dwzp',
    true,
    now()
  ),
  (
    gen_random_uuid(),
    'argentina',
    'Pampas Casino - Argentina',
    'agent_7101kp8jz5wnej79qrsz80mtk636',
    true,
    now()
  );
```

### Option 3: Python script
```python
# In Python shell or as script
from app.db.session import AsyncSessionLocal
from app.db.models import Campaign
import asyncio
import uuid

async def create_campaigns():
    async with AsyncSessionLocal() as db:
        campaigns = [
            Campaign(
                id=uuid.uuid4(),
                external_id="portugal",
                name="Oro Casino - Portugal",
                agent_id="agent_6901knmsm0cpfw39pzd84f33dwzp",
                is_active=True
            ),
            Campaign(
                id=uuid.uuid4(),
                external_id="argentina",
                name="Pampas Casino - Argentina",
                agent_id="agent_7101kp8jz5wnej79qrsz80mtk636",
                is_active=True
            ),
        ]
        db.add_all(campaigns)
        await db.commit()
        print("Campaigns created!")

asyncio.run(create_campaigns())
```

---

## Testing

### 1. Test Message Generation & Randomization

```bash
# Generate 3 messages from each agent, check randomization
cd /Users/glebuhovskij/Desktop/whatsApp\ sms\ bot

# Set required env vars
export ELEVENLABS_API_KEY="your-key-here"

# Run the test
python test_full_flow_agents.py
```

**What it checks:**
- ✓ Messages generated from correct agents
- ✓ Links substituted correctly
- ✓ Promot substituted correctly
- ✓ Messages split into 3 parts
- ✓ Randomization works (different openings in 3 sends)
- ✓ Required fields present in each part

**Expected output:**
```
═ ТЕСТ: Portuguese (Oro/Camila)
  Итерация 1/3
    [1] ✓ Получено (XXX символов)
    [2] ✓ Разделено на 3 части
      ✓ Ссылка найдена
      ✓ Промо найдено

═ ТЕСТ: Spanish AR (Pampas/Olivia)
  Итерация 1/3
    [1] ✓ Получено (XXX символов)
    [2] ✓ Разделено на 3 части
      ✓ Ссылка найдена

✅ ГОТОВО К PRODUCTION!
```

### 2. Test Full Integration (Message + Send)

```bash
# Test full flow: generate → split → send via Green API
# ⚠️  ADJUST THE PHONE NUMBERS BEFORE RUNNING!

python test_webhook_integration.py
```

Edit test_webhook_integration.py and update:
```python
TEST_CASES = [
    {
        "phone": "351XXXXXXXXX",  # ← PUT REAL PT NUMBER
        ...
    },
    {
        "phone": "54XXXXXXXXX",   # ← PUT REAL AR NUMBER
        ...
    },
]
```

**What it checks:**
- ✓ Country detection from phone prefix
- ✓ Campaign loaded correctly
- ✓ Correct agent_id used
- ✓ Conversation created/found
- ✓ Message generated
- ✓ Message split into 3 parts
- ✓ Links & promo preserved in parts
- ✓ Sent through Green API

**Expected output:**
```
✅ WEBHOOK SIMULATION: Oro Casino (Portugal)
  [1] ✓ Детектирована страна: Portugal
  [2] ✓ Найдена кампания
  [3] ✓ Найден существующий conversation
  [4] ✓ Получено сообщение
  [5] ✓ Разделено на 3 части
  [6] ✅ УСПЕШНО ОТПРАВЛЕНО!

✅ ВСЕ WEBHOOK ТЕСТЫ ПРОЙДЕНЫ!
🚀 ГОТОВО К PRODUCTION!
```

### 3. Production Send Test (Real Numbers)

```bash
# ⚠️  REAL SEND — ADJUST PHONE NUMBERS FIRST!

python test_production_send.py
```

Edit test_production_send.py:
```python
TEST_CONFIGS = [
    {
        "phone": "351XXXXXXXXX",  # ← REAL PORTUGAL NUMBER
        ...
    },
    {
        "phone": "54XXXXXXXXX",   # ← REAL ARGENTINA NUMBER
        ...
    },
]
```

---

## Webhook Integration

Once everything is tested, integrate with your SMS/call webhook handler:

```python
from app.api.routes import _resolve_initial_message
from app.db.session import AsyncSessionLocal

async def handle_inbound_webhook(phone: str):
    """Called when you receive an SMS/call on this number."""
    
    async with AsyncSessionLocal() as db:
        # _resolve_initial_message handles:
        # 1. Country detection
        # 2. Campaign lookup
        # 3. Message generation from correct agent
        # 4. Splitting into 3 parts
        # 5. Inserting links & promo
        
        parts = await _resolve_initial_message(
            db=db,
            campaign=campaign,  # fetched from country detection
            phone=phone,
            provided=None,  # No manual override
            language=language,  # Detected from country
            link_url="https://...",  # Your affiliate link
            promo_code=promo  # From country.py
        )
        
        # Then send via send_initial_message
        msg = await send_initial_message(db, conversation, parts, batch_index=0)
```

---

## Checklist

- [ ] Campaigns created in database (portugal + argentina)
- [ ] Agent IDs configured:
  - [ ] Oro: `agent_6901knmsm0cpfw39pzd84f33dwzp`
  - [ ] Pampas: `agent_7101kp8jz5wnej79qrsz80mtk636`
- [ ] ElevenLabs agents have correct system prompts
- [ ] Test 1: Randomization test PASSED (`test_full_flow_agents.py`)
- [ ] Test 2: Integration test PASSED (`test_webhook_integration.py`)
- [ ] Test 3: Production send PASSED (`test_production_send.py`)
- [ ] Green API instance is active in database
- [ ] Green API credentials configured in env
- [ ] Ready for production!

---

## Monitoring in Production

Check the logs for:
1. Language guard rejections → if too strict, agent generates wrong language
2. Fallback usage → if too frequent, check ElevenLabs API status
3. Message split sanity → ensure links/promo always in final part
4. Green API errors → check credentials and rate limits

```python
# In logs, look for:
logger.warning("Outreach attempt X rejected by language guard...")
logger.info("Outreach generated [pt-PT] attempt=X...")
logger.warning("ElevenLabs first_message is empty...")
```

---

## Troubleshooting

### Messages don't have correct persona
- Check: ElevenLabs agent prompt mentions the name and specific offer
- Check: Language guard isn't rejecting valid messages

### Links missing in final message
- Check: Link substitution in `_ensure_required_outreach_fields`
- Check: Link present in `_split_outreach_into_three_random_parts`

### Promo missing for Pampas
- Expected: Pampas doesn't need promo (it's "175% bonus from ARS 5000")
- Check: Fallback template doesn't include hardcoded promo for AR

### Same message sent twice
- Check: Anti-spam seed generation (should be unique)
- Check: Collision detection in `_recent_opening_keys`

### Green API not sending
- Check: Instance is active in database
- Check: Green API credentials in environment
- Check: Instance has enough balance/quota

---

Generated: 2026-04-15
Last tested: (update after successful production test)
