"""
Bulk sender — загружает leads из CSV и отправляет батчами по 50.

CSV формат (с заголовком):
  phone,lead_id,lead_name

Использование:
  python send_bulk.py leads.csv --campaign golden_reels_march --message "Hey {name}! This is Riley..."
"""
import argparse
import csv
import json
import sys
import time
import urllib.request
import urllib.error

API_URL  = "http://localhost:8000"
API_KEY  = "your-secret-key"   # поменяй на свой
BATCH    = 50                   # лидов за один запрос


def send_batch(leads: list[dict]) -> dict:
    payload = json.dumps({
        "campaign_external_id": leads[0]["campaign_external_id"],
        "leads": leads,
    }).encode()

    req = urllib.request.Request(
        f"{API_URL}/api/send/bulk",
        data=payload,
        headers={"Content-Type": "application/json", "X-Api-Key": API_KEY},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_file", help="Path to CSV with leads")
    parser.add_argument("--campaign", required=True, help="campaign_external_id")
    parser.add_argument("--message", required=True, help="Initial message text. Use {name} for lead name.")
    args = parser.parse_args()

    with open(args.csv_file, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    print(f"📋 Загружено {len(rows)} лидов из {args.csv_file}")

    # Формируем список
    leads = []
    for i, row in enumerate(rows):
        name  = row.get("lead_name", "").strip() or None
        msg   = args.message.replace("{name}", name or "there")
        leads.append({
            "phone":                 row["phone"].strip(),
            "lead_id":               row.get("lead_id", row["phone"]).strip(),
            "lead_name":             name,
            "campaign_external_id":  args.campaign,
            "initial_message":       msg,
            "batch_index":           i,
        })

    # Разбиваем на батчи по BATCH
    total_sent  = 0
    total_skip  = 0
    total_error = 0

    for start in range(0, len(leads), BATCH):
        batch = leads[start : start + BATCH]
        end   = min(start + BATCH, len(leads))
        print(f"  → Отправляю лидов {start+1}–{end}...", end=" ", flush=True)

        try:
            result = send_batch(batch)
            sent  = result.get("sent", 0)
            total = result.get("total", len(batch))
            skip  = sum(1 for r in result["results"] if r["status"] in ("skipped", "blacklisted"))
            err   = sum(1 for r in result["results"] if r["status"] == "error")

            total_sent  += sent
            total_skip  += skip
            total_error += err
            print(f"✅ отправлено={sent}  пропущено={skip}  ошибок={err}")

        except urllib.error.HTTPError as e:
            body = e.read().decode()
            print(f"❌ HTTP {e.code}: {body}")
            total_error += len(batch)

        except Exception as ex:
            print(f"❌ {ex}")
            total_error += len(batch)

        # Небольшая пауза между батчами чтобы не перегружать сервер
        if end < len(leads):
            time.sleep(2)

    print()
    print("=" * 40)
    print(f"ИТОГО: {len(leads)} лидов")
    print(f"  ✅ Отправлено:  {total_sent}")
    print(f"  ⏭  Пропущено:  {total_skip}")
    print(f"  ❌ Ошибок:     {total_error}")


if __name__ == "__main__":
    main()
