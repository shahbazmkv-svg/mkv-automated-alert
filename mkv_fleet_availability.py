"""
MKV Luxury – Fleet Availability
=================================
API 1: get-mkv-vehicles.php              → vehicle list (names, plates)
API 2: get-mkv-vehicle-assignments.php   → accurate counts (STR, Lease, Longterm, Service, Available)
API 3: get-mkv-available-vehicle.php     → per-vehicle to identify AVAILABLE ones for the list

Slack message:
  Summary block  → all counts from assignments API
  Vehicle list   → AVAILABLE vehicles only (from availability API)
"""
import os, json, re, requests
from datetime import datetime, timezone, timedelta

SLACK_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
APPIC_KEY     = os.environ.get("APPIC_KEY", "")
SLACK_CHANNEL = "C0B0TGBDCDU"   # #mkvtest
DUBAI_TZ      = timezone(timedelta(hours=4))
BLOCK_LIMIT   = 2900

BASE_URL             = "https://www.appicfleet.com/appiccar-apis-mkv"
MKV_VEHICLES_URL     = f"{BASE_URL}/get-mkv-vehicles.php"
MKV_ASSIGNMENTS_URL  = f"{BASE_URL}/get-mkv-vehicle-assignments.php"
MKV_AVAIL_URL        = f"{BASE_URL}/get-mkv-available-vehicle.php"

CONTACT_FOOTER = (
    "📱 +971 56 279 4545\n☎️  +971 4 238 8987\n"
    "🌐 https://www.mkvluxury.com/\n📸 https://www.instagram.com/mkvluxurydubai/\n"
    "✉️  contact@mkvluxury.com\n📍 Al Jreena Street 41, Al Qouz Industrial Third, Dubai, UAE"
)

def now_dubai():
    return datetime.now(DUBAI_TZ)

def get_plate(v: dict) -> str:
    for key in ["plate", "vehiclePlate", "plateNo", "plate_no"]:
        val = str(v.get(key, "") or "").strip()
        if val and val != "0":
            return val
    return ""

def get_vehicle_id(v: dict) -> str:
    for k in ["vehicleID", "id", "vehicleId", "vehicle_id"]:
        val = str(v.get(k, "") or "").strip()
        if val and val not in ("0", ""):
            return val
    return ""

# ─────────────────────────────────────────────────────────
# 1. Fetch STR fleet vehicle list
# ─────────────────────────────────────────────────────────
def fetch_vehicles() -> list:
    try:
        r     = requests.post(MKV_VEHICLES_URL, data={"key": APPIC_KEY}, timeout=20)
        r.raise_for_status()
        resp  = r.json()
        all_v = resp if isinstance(resp, list) else resp.get("data", resp.get("vehicles", []))
        active = [
            v for v in all_v
            if float(v.get("dailyrent",   0) or 0) > 0
            or float(v.get("weeklyrent",  0) or 0) > 0
            or float(v.get("monthlyrent", 0) or 0) > 0
        ]
        print(f"  Vehicles total : {len(all_v)} | Active fleet (any rate>0): {len(active)}")
        return active
    except Exception as ex:
        print(f"  ❌ Vehicles API error: {ex}")
        return []

# ─────────────────────────────────────────────────────────
# 2. Fetch accurate counts from assignments API
# ─────────────────────────────────────────────────────────
def fetch_counts() -> dict:
    try:
        r = requests.post(MKV_ASSIGNMENTS_URL, data={"key": APPIC_KEY}, timeout=20)
        r.raise_for_status()
        resp   = r.json()
        counts = resp.get("counts", {})
        print(f"  Assignments API: {counts}")
        return counts
    except Exception as ex:
        print(f"  ❌ Assignments API error: {ex}")
        return {}

# ─────────────────────────────────────────────────────────
# 3. Identify AVAILABLE vehicles for the list
# ─────────────────────────────────────────────────────────
def fetch_available_vehicles(vehicles: list) -> list:
    """Returns list of vehicle dicts that are available today."""
    today_str    = now_dubai().strftime("%Y-%m-%d")
    tomorrow_str = (now_dubai() + timedelta(days=1)).strftime("%Y-%m-%d")
    available    = []

    for v in vehicles:
        vid = get_vehicle_id(v)
        if not vid:
            continue
        try:
            r = requests.post(MKV_AVAIL_URL, data={
                "key":       APPIC_KEY,
                "startDate": today_str,
                "endDate":   tomorrow_str,
                "vehicleID": vid
            }, timeout=15)
            r.raise_for_status()
            resp      = r.json()
            raw       = str(resp.get("status", "") or "").lower().strip()
            is_booked = resp.get("isBooked", False)

            if not is_booked and "available" in raw:
                available.append(v)
                print(f"  ✅ AVAILABLE: vehicleID={vid} | plate={get_plate(v)} | {raw}")
            else:
                print(f"  ⬜ {raw.upper():<20} vehicleID={vid} | plate={get_plate(v)}")

        except Exception as ex:
            print(f"  ⚠️  Error for vehicleID={vid}: {ex}")

    print(f"  Available vehicles: {len(available)} / {len(vehicles)}")
    return available

# ─────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────
def fmt_name(v: dict) -> str:
    make  = v.get("make",  "").strip().upper()
    model = v.get("model", "").strip().upper()
    year  = str(v.get("year", "")).strip()
    color = v.get("color", "").strip().upper()
    plate = get_plate(v)
    if model.startswith(make):
        model = model[len(make):].strip()
    parts = [p for p in [make, model] if p]
    if year and year not in ("0", "200", ""):
        parts.append(f"({year})")
    if color and color not in ("", "N/A"):
        parts.append(color)
    if plate:
        parts.append(f"[{plate}]")
    return " ".join(parts) or "UNKNOWN"

def text_to_blocks(text):
    blocks, chunk = [], ""
    for line in text.split("\n"):
        cand = chunk + line + "\n"
        if len(cand) > BLOCK_LIMIT:
            if chunk:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"```{chunk.rstrip()}```"}})
            chunk = line + "\n"
        else:
            chunk = cand
    if chunk.strip():
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"```{chunk.rstrip()}```"}})
    return blocks

# ─────────────────────────────────────────────────────────
# 4. Build Slack message
# ─────────────────────────────────────────────────────────
def build_message(counts: dict, available_vehicles: list, total_vehicles: int):
    now      = now_dubai()
    date_str = now.strftime("%B %d, %Y").upper()
    day_str  = now.strftime("%A").upper()

    total    = total_vehicles
    str_c    = counts.get("shortTermRental", 0)
    lease_c  = counts.get("lease", 0)
    ltr_c    = counts.get("longTermRental", 0)
    service_c= counts.get("service", 0)
    avail_c  = counts.get("available", 0)
    nrv_c    = counts.get("unavailable", 0)

    print(f"\n  ┌─────────────────────────────────┐")
    print(f"  │ FLEET SUMMARY                   │")
    print(f"  │ Total    : {total:<22} │")
    print(f"  │ STR      : {str_c:<22} │")
    print(f"  │ Lease    : {lease_c:<22} │")
    print(f"  │ Longterm : {ltr_c:<22} │")
    print(f"  │ Available: {avail_c:<22} │")
    print(f"  │ Service  : {service_c:<22} │")
    print(f"  │ NRV      : {nrv_c:<22} │")
    print(f"  └─────────────────────────────────┘")

    summary = "\n".join([
        f"❝{date_str} {day_str}❞", "",
        f"✦ Total        : {total}", "",
        f"✦ Rented STR   : {str_c}", "",
        f"✦ Service      : {service_c}", "",
        f"✦ Available    : {avail_c}", "",
        f"✦ Lease        : {lease_c}", "",
        f"✦ Longterm     : {ltr_c}", "",
        f"✦ NRV          : {nrv_c}",
    ])

    lines = []
    if available_vehicles:
        lines.append("AVAILABLE")
        lines.append("-" * 30)
        for i, v in enumerate(available_vehicles, 1):
            lines.append(f"{i}. {fmt_name(v)}")
        lines.append("")

    lines += ["For inquiries please contact this number", "", CONTACT_FOOTER]

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```{summary}```"}},
        {"type": "divider"},
        *text_to_blocks("\n".join(lines)),
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "MKV Active Fleet • Auto-posted daily 10:00 AM Dubai time"}]},
    ]
    return {"blocks": blocks, "text": f"MKV Fleet Availability — {date_str} {day_str}"}

# ─────────────────────────────────────────────────────────
# 5. Post to Slack
# ─────────────────────────────────────────────────────────
def post_slack(message):
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"},
        data=json.dumps({
            "channel":      SLACK_CHANNEL,
            "username":     "MKV Fleet Status",
            "icon_emoji":   ":car:",
            "unfurl_links": False,
            "unfurl_media": False,
            **message
        }),
        timeout=15,
    )
    res = r.json()
    if not res.get("ok"):
        print(f"  ❌ Slack error: {res.get('error')}")
        raise SystemExit(1)
    print("  ✅ Posted to Slack successfully")

# ─────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("  MKV FLEET AVAILABILITY")
    print(f"  {now_dubai().strftime('%d %b %Y | %I:%M %p Dubai Time')}")
    print("=" * 55)

    print("\n[1] Fetching STR fleet vehicle list ...")
    vehicles = fetch_vehicles()
    if not vehicles:
        print("  No vehicles returned — exiting")
        raise SystemExit(1)

    print("\n[2] Fetching fleet counts from assignments API ...")
    counts = fetch_counts()

    print("\n[3] Identifying available vehicles ...")
    available_vehicles = fetch_available_vehicles(vehicles)

    print("\n[4] Building Slack message ...")
    msg = build_message(counts, available_vehicles, len(vehicles))

    print("\n[5] Posting to Slack ...")
    post_slack(msg)
