"""
MKV Luxury – Fleet Availability
=================================
API 1: get-mkv-vehicles.php          → full STR fleet list (dailyrent > 0)
API 2: get-mkv-available-vehicle.php → check availability per vehicle for today
API 3: get-mkv-bookings.php          → classify contracts: STR / Lease / Longterm

Contract type by duration:
  STR      : 1–30  days
  Lease    : 31–730 days
  Longterm : 731+  days
"""
import os, json, re, requests
from datetime import datetime, timezone, timedelta

SLACK_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
APPIC_KEY     = os.environ.get("APPIC_KEY", "")
SLACK_CHANNEL = "C0B0TGBDCDU"   # #mkvtest — switch to live channel when ready
DUBAI_TZ      = timezone(timedelta(hours=4))
BLOCK_LIMIT   = 2900

BASE_URL         = "https://www.appicfleet.com/appiccar-apis-mkv"
MKV_VEHICLES_URL = f"{BASE_URL}/get-mkv-vehicles.php"
MKV_AVAIL_URL    = f"{BASE_URL}/get-mkv-available-vehicle.php"
BOOKINGS_URL     = f"{BASE_URL}/get-mkv-bookings.php"

CONTACT_FOOTER = (
    "📱 +971 56 279 4545\n☎️  +971 4 238 8987\n"
    "🌐 https://www.mkvluxury.com/\n📸 https://www.instagram.com/mkvluxurydubai/\n"
    "✉️  contact@mkvluxury.com\n📍 Al Jreena Street 41, Al Qouz Industrial Third, Dubai, UAE"
)

def now_dubai():
    return datetime.now(DUBAI_TZ)

def parse_date(s):
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

# ─────────────────────────────────────────────────────────
# 1. Fetch full fleet list
# ─────────────────────────────────────────────────────────
def fetch_vehicles() -> list:
    """POST get-mkv-vehicles.php → returns all STR fleet (dailyrent > 0)."""
    try:
        r = requests.post(MKV_VEHICLES_URL, data={"key": APPIC_KEY}, timeout=20)
        r.raise_for_status()
        resp = r.json()

        all_v = resp if isinstance(resp, list) else resp.get("data", resp.get("vehicles", []))

        if all_v:
            s = all_v[0]
            print(f"  Vehicle keys   : {list(s.keys())}")
            print(f"  Sample         : id={s.get('id')} | plate={s.get('plate')} | "
                  f"vehiclePlate={s.get('vehiclePlate')} | dailyrent={s.get('dailyrent')}")

        active = [v for v in all_v if float(v.get("dailyrent", 0) or 0) > 0]
        print(f"  Vehicles total : {len(all_v)} | STR fleet (dailyrent>0): {len(active)}")
        return active
    except Exception as ex:
        print(f"  ❌ Vehicles API error: {ex}")
        return []

# ─────────────────────────────────────────────────────────
# 2. Fetch active contracts → classify STR / Lease / Longterm
# ─────────────────────────────────────────────────────────
def fetch_contract_type_map() -> dict:
    """Returns { normalized_plate -> 'str' | 'lease' | 'longterm' }"""
    PRIORITY = {"longterm": 3, "lease": 2, "str": 1}
    try:
        today     = now_dubai().date()
        start_str = (now_dubai() - timedelta(days=730)).strftime("%Y-%m-%d")
        end_str   = (now_dubai() + timedelta(days=730)).strftime("%Y-%m-%d")

        r = requests.post(BOOKINGS_URL, data={
            "key": APPIC_KEY, "startDate": start_str, "endDate": end_str
        }, timeout=20)
        r.raise_for_status()
        bookings = r.json().get("bookings", [])
        print(f"  Bookings total : {len(bookings)}")

        plate_type = {}
        for b in bookings:
            status = str(b.get("status") or b.get("bookingStatus") or "").lower().strip()
            if status in ("cancelled", "canceled", "voided", "void", "deleted"):
                continue

            plate = str(b.get("vehiclePlate", "") or "").strip()
            if not plate:
                continue

            sd = parse_date(b.get("startDate"))
            ed = parse_date(b.get("endDate"))
            if sd is None or ed is None:
                continue
            if not (sd <= today <= ed):
                continue

            duration = (ed - sd).days
            ctype    = "longterm" if duration >= 731 else "lease" if duration >= 31 else "str"
            norm     = normalize_plate(plate)

            if norm not in plate_type or PRIORITY[ctype] > PRIORITY[plate_type[norm]]:
                plate_type[norm] = ctype

        str_c = sum(1 for t in plate_type.values() if t == "str")
        lea_c = sum(1 for t in plate_type.values() if t == "lease")
        lt_c  = sum(1 for t in plate_type.values() if t == "longterm")
        print(f"  Contracts today: STR={str_c} | Lease={lea_c} | Longterm={lt_c}")
        return plate_type

    except Exception as ex:
        print(f"  ❌ Bookings API error: {ex}")
        return {}

# ─────────────────────────────────────────────────────────
# 3. Check availability per vehicle via dedicated API
# ─────────────────────────────────────────────────────────
def get_vehicle_id(v: dict) -> str:
    for k in ["id", "vehicleID", "vehicleId", "vehicle_id", "ID"]:
        val = str(v.get(k, "") or "").strip()
        if val and val not in ("0", ""):
            return val
    return ""

def fetch_available_ids(vehicle_ids: list, today_str: str) -> set:
    """
    Calls get-mkv-available-vehicle.php for each vehicle.
    Returns set of vehicleIDs confirmed available today.
    Handles multiple response shapes from Appic API.
    """
    available_ids = set()
    tomorrow_str  = (now_dubai() + timedelta(days=1)).strftime("%Y-%m-%d")

    for vid in vehicle_ids:
        try:
            r = requests.post(MKV_AVAIL_URL, data={
                "key":       APPIC_KEY,
                "startDate": today_str,
                "endDate":   tomorrow_str,
                "vehicleID": vid
            }, timeout=15)
            r.raise_for_status()
            resp = r.json()

            if not isinstance(resp, dict):
                continue

            # Pattern A: {"available": true/false/1/0}
            avail_flag = resp.get("available", resp.get("isAvailable"))
            # Pattern B: {"status": "available"/"rented"/"booked"}
            status_val = str(resp.get("status", "")).lower()
            # Pattern C: {"data": [...]} — non-empty = available
            data_val   = resp.get("data", resp.get("vehicles", []))

            if avail_flag in (True, "true", "1", 1):
                available_ids.add(str(vid))
            elif avail_flag in (False, "false", "0", 0):
                pass  # confirmed not available
            elif status_val == "available":
                available_ids.add(str(vid))
            elif status_val in ("rented", "booked", "unavailable"):
                pass
            elif isinstance(data_val, list) and len(data_val) > 0:
                available_ids.add(str(vid))

        except Exception as ex:
            print(f"  ⚠️  Avail check error for id={vid}: {ex}")
            continue

    print(f"  Available (API) : {len(available_ids)} / {len(vehicle_ids)}")
    return available_ids

# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────
def get_plate(v: dict) -> str:
    for key in ["plate", "vehiclePlate", "plateNo", "plate_no", "number_plate", "licensePlate"]:
        val = str(v.get(key, "") or "").strip()
        if val and val != "0":
            return val
    return ""

def normalize_plate(plate: str) -> str:
    digits = re.sub(r"[^0-9]", "", str(plate))
    return digits.lstrip("0") if digits else str(plate)

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
def build_message(vehicles, contract_map, available_ids):
    now      = now_dubai()
    date_str = now.strftime("%B %d, %Y").upper()
    day_str  = now.strftime("%A").upper()

    str_v      = []
    lease_v    = []
    longterm_v = []
    available_v= []
    garage_v   = []
    service_v  = []

    for v in vehicles:
        plate_norm = normalize_plate(get_plate(v))
        vid        = str(get_vehicle_id(v))
        avail_flag = (v.get("availability") or "").lower().strip()
        status_raw = (v.get("status") or "").lower().strip()
        contract   = contract_map.get(plate_norm)

        if contract == "str":
            str_v.append(v)
        elif contract == "lease":
            lease_v.append(v)
        elif contract == "longterm":
            longterm_v.append(v)
        elif avail_flag == "garage" or status_raw == "garage":
            garage_v.append(v)
        elif avail_flag in ("service", "maintenance") or status_raw in ("service", "maintenance"):
            service_v.append(v)
        else:
            available_v.append(v)  # no active contract = available

    total    = len(vehicles)
    rented   = len(str_v)
    lease    = len(lease_v)
    longterm = len(longterm_v)
    avail    = len(available_v)
    garage   = len(garage_v)
    service  = len(service_v)

    print(f"\n  ┌─────────────────────────────────┐")
    print(f"  │ FLEET SUMMARY                   │")
    print(f"  │ Total    : {total:<22} │")
    print(f"  │ STR      : {rented:<22} │")
    print(f"  │ Lease    : {lease:<22} │")
    print(f"  │ Longterm : {longterm:<22} │")
    print(f"  │ Available: {avail:<22} │")
    print(f"  │ Garage   : {garage:<22} │")
    print(f"  │ Service  : {service:<22} │")
    print(f"  └─────────────────────────────────┘")

    summary = "\n".join([
        f"❝{date_str} {day_str}❞", "",
        f"✦ Total        : {total}", "",
        f"✦ Rented STR   : {rented}", "",
        f"✦ Garage       : {garage}", "",
        f"✦ Service      : {service}", "",
        f"✦ Available    : {avail}", "",
        f"✦ Lease        : {lease}", "",
        f"✦ Longterm     : {longterm}", "",
        f"✦ NRV          : 0",
    ])

    lines = []

    def section(title, vlist):
        if not vlist:
            return
        lines.append(title)
        lines.append("-" * 30)
        for i, v in enumerate(vlist, 1):
            lines.append(f"{i}. {fmt_name(v)}")
        lines.append("")

    section("AVAILABLE",  available_v)
    section("GARAGE",     garage_v)
    section("SERVICE",    service_v)
    section("LEASE",      lease_v)
    section("LONGTERM",   longterm_v)

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

    print("\n[1] Fetching vehicles from get-mkv-vehicles.php ...")
    vehicles = fetch_vehicles()
    if not vehicles:
        print("  No vehicles returned — exiting")
        raise SystemExit(1)

    print("\n[2] Classifying contracts (STR / Lease / Longterm) ...")
    contract_map = fetch_contract_type_map()

    print("\n[3] Checking availability via get-mkv-available-vehicle.php ...")
    today_str    = now_dubai().strftime("%Y-%m-%d")
    vehicle_ids  = [get_vehicle_id(v) for v in vehicles if get_vehicle_id(v)]
    available_ids= fetch_available_ids(vehicle_ids, today_str)

    print("\n[4] Building Slack message ...")
    msg = build_message(vehicles, contract_map, available_ids)

    print("\n[5] Posting to Slack ...")
    post_slack(msg)
