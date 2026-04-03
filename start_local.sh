#!/bin/bash
# ============================================================
# WR WhatsApp Service — Local Startup Script
# Usage: ./start_local.sh
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env
set -a; source .env; set +a

API_KEY="${API_SECRET_KEY:-wr-whatsapp-secret-2026}"
BASE_URL="http://localhost:8000"

echo ""
echo "============================================================"
echo " WR WhatsApp Service — Local Startup"
echo "============================================================"

# ---- Step 1: Docker Compose --------------------------------
echo ""
echo "[1/5] Starting Docker (postgres + app)..."
docker compose down --remove-orphans 2>/dev/null || true
docker compose up -d --build

echo "      Waiting for app to be healthy..."
for i in $(seq 1 30); do
  if curl -sf "$BASE_URL/health" > /dev/null 2>&1; then
    echo "      App is up!"
    break
  fi
  sleep 2
  if [ $i -eq 30 ]; then
    echo "      [ERROR] App did not start in 60s. Check logs: docker compose logs app"
    exit 1
  fi
done

# ---- Step 2: ngrok -----------------------------------------
echo ""
echo "[2/5] Starting ngrok on port 8000..."
pkill -f "ngrok http 8000" 2>/dev/null || true
sleep 1
ngrok http 8000 --log=stdout > /tmp/ngrok_wr.log 2>&1 &
NGROK_PID=$!
echo "      ngrok PID: $NGROK_PID"
sleep 3

NGROK_URL=$(curl -sf http://127.0.0.1:4040/api/tunnels 2>/dev/null \
  | python3 -c "import sys,json; t=json.load(sys.stdin)['tunnels']; print([x for x in t if x['proto']=='https'][0]['public_url'])" 2>/dev/null)

if [ -z "$NGROK_URL" ]; then
  echo "      [ERROR] Could not get ngrok URL. Is ngrok authenticated?"
  echo "      Run: ngrok config add-authtoken YOUR_TOKEN"
  exit 1
fi

echo "      ngrok URL: $NGROK_URL"
WEBHOOK_URL="${NGROK_URL}/webhook/${INSTANCE_ID}"
echo "      Webhook URL: $WEBHOOK_URL"

# ---- Step 3: Update Green API webhook ----------------------
echo ""
echo "[3/5] Configuring Green API webhook..."
python3 - <<EOF
import urllib.request, json

data = json.dumps({
    "webhookUrl": "$WEBHOOK_URL",
    "incomingWebhook": "yes",
    "outgoingMessageWebhook": "yes",
    "outgoingAPIMessageWebhook": "yes",
    "markIncomingMessagesReaded": "yes"
}).encode("utf-8")

req = urllib.request.Request(
    "https://7107.api.greenapi.com/waInstance${INSTANCE_ID}/setSettings/${INSTANCE_API_TOKEN}",
    data=data, headers={"Content-Type": "application/json"}
)
with urllib.request.urlopen(req) as r:
    print("      Green API:", r.read().decode())
EOF

# ---- Step 4: Create Instance + Campaign --------------------
echo ""
echo "[4/5] Creating WhatsApp instance and campaign in DB..."

# Create instance (ignore 409 if already exists)
python3 - <<EOF
import urllib.request, json

data = json.dumps({
    "name": "Instance ${INSTANCE_ID}",
    "instance_id": "${INSTANCE_ID}",
    "api_token": "${INSTANCE_API_TOKEN}",
    "phone_number": "447488817897",
    "daily_limit": 150,
    "hourly_limit": 30,
    "min_delay_sec": 2,
    "max_delay_sec": 5
}).encode("utf-8")

req = urllib.request.Request(
    "$BASE_URL/api/instances",
    data=data,
    headers={"Content-Type": "application/json", "x-api-key": "$API_KEY"}
)
try:
    with urllib.request.urlopen(req) as r:
        print("      Instance created:", json.loads(r.read())["id"])
except urllib.error.HTTPError as e:
    if e.code == 409:
        print("      Instance already exists — OK")
    else:
        print("      Instance error:", e.read().decode())
EOF

# Create campaign (ignore 409 if already exists)
python3 - <<EOF
import urllib.request, json

data = json.dumps({
    "external_id": "golden-reels-reactivation",
    "name": "Golden Reels Reactivation",
    "agent_id": "${AGENT_ID}"
}).encode("utf-8")

req = urllib.request.Request(
    "$BASE_URL/api/campaigns",
    data=data,
    headers={"Content-Type": "application/json", "x-api-key": "$API_KEY"}
)
try:
    with urllib.request.urlopen(req) as r:
        print("      Campaign created:", json.loads(r.read())["id"])
except urllib.error.HTTPError as e:
    if e.code == 409:
        print("      Campaign already exists — OK")
    else:
        print("      Campaign error:", e.read().decode())
EOF

# ---- Step 5: Send first message to test leads --------------
echo ""
echo "[5/5] Sending first messages to test leads..."

python3 - <<EOF
import urllib.request, json

leads = [
    {"phone": "380671202709", "lead_id": "lead-001", "lead_name": "Тест UA"},
    {"phone": "351966242501", "lead_id": "lead-002", "lead_name": "Тест PT"},
]

for lead in leads:
    data = json.dumps({
        "phone": lead["phone"],
        "lead_id": lead["lead_id"],
        "lead_name": lead["lead_name"],
        "campaign_external_id": "golden-reels-reactivation",
        "initial_message": "Привет! Я Райли из Golden Reels. Хорошие новости для вашего аккаунта — есть кое-что для вас! Удобно сейчас поговорить?"
    }).encode("utf-8")

    req = urllib.request.Request(
        "$BASE_URL/api/send",
        data=data,
        headers={"Content-Type": "application/json", "x-api-key": "$API_KEY"}
    )
    try:
        with urllib.request.urlopen(req) as r:
            result = json.loads(r.read())
            print(f"      {lead['phone']}: {result}")
    except urllib.error.HTTPError as e:
        print(f"      {lead['phone']} ERROR: {e.read().decode()}")
EOF

# ---- Done --------------------------------------------------
echo ""
echo "============================================================"
echo " ALL DONE!"
echo "============================================================"
echo ""
echo "  Service:     $BASE_URL"
echo "  Webhook:     $WEBHOOK_URL"
echo "  ngrok UI:    http://127.0.0.1:4040"
echo ""
echo "  Now write to +380671202709 or +351966242501 in WhatsApp"
echo "  and Riley (ElevenLabs) will reply automatically."
echo ""
echo "  Logs: docker compose logs -f app"
echo "============================================================"
