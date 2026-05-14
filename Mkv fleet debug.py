"""
mkv_fleet_debug.py
Explores both fleet APIs and dumps all fields.
Run once via GitHub Actions, then delete.
"""
import os, json, requests
from datetime import datetime, timezone, timedelta

APPIC_KEY     = os.environ["APPIC_KEY"]
SLACK_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL = "C0B0TGBDCDU"   # #mkvtest
DUBAI_TZ      = timezone(timedelta(hours=4))

VEHICLES_URL  = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-vehicles.php"
AVAIL_URL     = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-available-vehicle.php"
BOOKINGS_URL  = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-bookings.php"

HEADERS = {"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"}

def post_slack(text):
    requests.post("https://slack.com/api/chat.postMessage",
        headers=HEADERS,
        json={"channel": SLACK_CHANNEL, "text": text},
        timeout=10)

def main():
    today = datetime.now(DUBAI_TZ).strftime("%Y-%m-%d")
    report = f"*MKV Fleet API Debug — {today}*\n\n"

    # ── 1. Vehicles API ────────────────────────────────────
    print("Fetching vehicles API...")
    r = requests.post(VEHICLES_URL, data={"key": APPIC_KEY}, timeout=20)
    data = r.json()
    vehicles = data if isinstance(data, list) else data.get("data", data.get("vehicles", []))

    report += f"*1. Vehicles API — total returned: {len(vehicles)}*\n"
    report += "First 3 vehicle fields:\n```\n"
    for v in vehicles[:3]:
        for k, val in v.items():
            report += f"  {k:<30}: {val}\n"
        report += "---\n"
    report += "```\n\n"

    # Count dailyrent > 0 (MKV STR fleet)
    str_fleet = [v for v in vehicles if float(v.get("dailyrent", 0) or 0) > 0]
    report += f"*Vehicles with dailyrent > 0 (STR fleet): {len(str_fleet)}*\n\n"

    # Show all categories/types present
    cats = {}
    for v in vehicles:
        cat = str(v.get("category", v.get("vehicleType", v.get("type", "unknown"))) or "unknown")
        cats[cat] = cats.get(cat, 0) + 1
    report += f"*Categories found:* {json.dumps(cats)}\n\n"

    # ── 2. Availability API — test one vehicle ─────────────
    if str_fleet:
        test_v = str_fleet[0]
        vid = str(test_v.get("vehicleID", test_v.get("id", "")) or "")
        if vid:
            print(f"Testing availability API with vehicleID={vid}...")
            r2 = requests.post(AVAIL_URL, data={
                "key": APPIC_KEY, "startDate": today,
                "endDate": today, "vehicleID": vid
            }, timeout=15)
            avail_data = r2.json()
            report += f"*2. Availability API response (vehicleID={vid}):*\n```\n"
            for k, val in avail_data.items():
                report += f"  {k:<30}: {val}\n"
            report += "```\n\n"

    # ── 3. Bookings API — today's data ────────────────────
    print("Fetching bookings API...")
    lookback = (datetime.now(DUBAI_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    lookahead = (datetime.now(DUBAI_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
    r3 = requests.post(BOOKINGS_URL, data={
        "key": APPIC_KEY, "startDate": lookback, "endDate": lookahead
    }, timeout=20)
    bookings = r3.json().get("bookings", [])

    active = [b for b in bookings if (b.get("status") or "").lower() not in
              {"cancelled", "canceled", "voided", "void", "deleted", "closed"}]

    today_deliver = [b for b in active if b.get("startDate") == today]
    today_return  = [b for b in active if b.get("endDate") == today]

    report += f"*3. Bookings API — today ({today}):*\n"
    report += f"  Active bookings in window: {len(active)}\n"
    report += f"  Deliveries today: {len(today_deliver)}\n"
    report += f"  Returns today: {len(today_return)}\n\n"

    # Show first booking all fields
    if bookings:
        report += "*First booking all fields:*\n```\n"
        for k, val in bookings[0].items():
            report += f"  {k:<30}: {val}\n"
        report += "```\n"

    print(report)
    post_slack(report)
    print("Debug report posted to #mkvtest")

if __name__ == "__main__":
    main()
