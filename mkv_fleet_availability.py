"""
MKV Luxury – Fleet Availability
=================================
STEP 1: get-mkv-vehicles.php
        → All MKV vehicles (dailyrent > 0, not borrowed)

STEP 2: get-mkv-available-vehicle.php (per vehicle, today)
        → "available"        → Available
        → "gone for service" → Service
        → "accident/damage"  → NRV (Accident/Damage)
        → "booked"           → has active contract → go to STEP 3

STEP 3: get-mkv-bookings.php
        → Find active contract for booked vehicle today
        → Classify by duration:
            1–30 days   → STR
            31–365 days → Lease
            366+ days   → LTR

OUTPUT (Slack):
  Summary  → Total / STR / Lease / LTR / Available / Service / NRV
  List     → AVAILABLE vehicles (name + plate)
"""
import os, json, re, requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict

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

def normalize_plate(plate: str) -> str:
    """Digits only, no leading zeros — for bookings API plate matching."""
    digits = re.sub(r"[^0-9]", "", str(plate))
    return digits.lstrip("0") if digits else str(plate)

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
# STEP 1: Fetch MKV fleet (dailyrent > 0, not borrowed)
# ─────────────────────────────────────────────────────────
def fetch_vehicles() -> list:
    try:
        r     = requests.post(MKV_VEHICLES_URL, data={"key": APPIC_KEY}, timeout=20)
        r.raise_for_status()
        resp  = r.json()
        all_v = resp if isinstance(resp, list) else resp.get("data", resp.get("vehicles", []))

        active = [
            v for v in all_v
            if (
                float(v.get("dailyrent",   0) or 0) > 0 or
                float(v.get("weeklyrent",  0) or 0) > 0 or
                float(v.get("monthlyrent", 0) or 0) > 0
            )
            and str(v.get("isBorrowed", v.get("borrowed", "0"))).strip() in ("0", "", "false", "False", "no")
        ]

        print(f"  Total in Appic : {len(all_v)}")
        print(f"  MKV fleet      : {len(active)} (rate>0, not borrowed)")
        return active
    except Exception as ex:
        print(f"  ❌ Vehicles API error: {ex}")
        return []

# ─────────────────────────────────────────────────────────
# STEP 2: Get status per vehicle from availability API
# Returns: { vehicleID -> "available" | "booked" | "service" | "nrv" }
# ─────────────────────────────────────────────────────────
def fetch_vehicle_statuses(vehicles: list) -> dict:
    today_str    = now_dubai().strftime("%Y-%m-%d")
    tomorrow_str = (now_dubai() + timedelta(days=1)).strftime("%Y-%m-%d")
    statuses     = {}

    for v in vehicles:
        vid   = get_vehicle_id(v)
        plate = get_plate(v)

        if not vid:
            statuses[""] = "available"
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

            # Classify by status string (Appic dropdown values)
            if "accident" in raw or "damage" in raw:
                statuses[vid] = "nrv"
            elif "service" in raw or "garage" in raw or "gone for" in raw:
                statuses[vid] = "service"
            elif is_booked or "booked" in raw or "rented" in raw:
                statuses[vid] = "booked"
            elif "available" in raw:
                statuses[vid] = "available"
            else:
                # Unknown — treat as available, log it
                print(f"  ⚠️  Unknown status: vehicleID={vid} plate={plate} raw='{raw}' resp={resp}")
                statuses[vid] = "available"

        except Exception as ex:
            print(f"  ⚠️  Avail error: vehicleID={vid} plate={plate}: {ex}")
            statuses[vid] = "available"

    from collections import Counter
    print(f"  Avail API      : {dict(Counter(statuses.values()))}")
    return statuses

# ─────────────────────────────────────────────────────────
# STEP 3: For booked vehicles → get contract type from bookings API
# Returns: { normalized_plate -> "str" | "lease" | "ltr" }
# Duration rules:
#   STR   : 1–30   days
#   Lease : 31–365 days
#   LTR   : 366+   days
# ─────────────────────────────────────────────────────────
def fetch_contract_types(booked_plates_norm: set) -> dict:
    if not booked_plates_norm:
        return {}
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

        PRIORITY   = {"ltr": 3, "lease": 2, "str": 1}
        plate_type = {}

        for b in bookings:
            status = str(b.get("status") or b.get("bookingStatus") or "").lower().strip()
            if status in ("cancelled", "canceled", "voided", "void", "deleted"):
                continue

            plate = str(b.get("vehiclePlate", "") or "").strip()
            if not plate:
                continue

            norm = normalize_plate(plate)
            if norm not in booked_plates_norm:
                continue  # not a booked MKV vehicle today

            sd = parse_date(b.get("startDate"))
            ed = parse_date(b.get("endDate"))
            if sd is None or ed is None:
                continue
            if not (sd <= today <= ed):
                continue

            duration = (ed - sd).days
            ctype    = "ltr" if duration >= 366 else "lease" if duration >= 31 else "str"

            if norm not in plate_type or PRIORITY[ctype] > PRIORITY[plate_type[norm]]:
                plate_type[norm] = ctype
                print(f"  CONTRACT: plate={plate:<14} {sd}→{ed} ({duration}d) → {ctype.upper()}")

        from collections import Counter
        print(f"  Contract types : {dict(Counter(plate_type.values()))}")
        return plate_type

    except Exception as ex:
        print(f"  ❌ Bookings API error: {ex}")
        return {}

# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────
def fmt_name(v: dict) -> str:
    name  = str(v.get("vehicle_name") or "").strip().upper()
    plate = get_plate(v)
    if name:
        return f"{name} [{plate}]" if plate else name
    make  = v.get("make",  "").strip().upper()
    model = v.get("model", "").strip().upper()
    year  = str(v.get("year", "")).strip()
    color = v.get("color", "").strip().upper()
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
# Build Slack message
# ─────────────────────────────────────────────────────────
def build_message(vehicles, vehicle_statuses, contract_types):
    now      = now_dubai()
    date_str = now.strftime("%B %d, %Y").upper()
    day_str  = now.strftime("%A").upper()

    str_v       = []
    lease_v     = []
    ltr_v       = []
    available_v = []
    service_v   = []
    nrv_v       = []

    for v in vehicles:
        vid        = get_vehicle_id(v)
        plate_norm = normalize_plate(get_plate(v))
        status     = vehicle_statuses.get(vid, "available")
        ctype      = contract_types.get(plate_norm, "str")

        if status == "service":
            service_v.append(v)
        elif status == "nrv":
            nrv_v.append(v)
        elif status == "booked":
            if ctype == "ltr":
                ltr_v.append(v)
            elif ctype == "lease":
                lease_v.append(v)
            else:
                str_v.append(v)
        else:
            available_v.append(v)

    total   = len(vehicles)
    str_c   = len(str_v)
    lease_c = len(lease_v)
    ltr_c   = len(ltr_v)
    avail_c = len(available_v)
    svc_c   = len(service_v)
    nrv_c   = len(nrv_v)

    print(f"\n  ┌─────────────────────────────────┐")
    print(f"  │ FLEET SUMMARY                   │")
    print(f"  │ Total    : {total:<22} │")
    print(f"  │ STR      : {str_c:<22} │")
    print(f"  │ Lease    : {lease_c:<22} │")
    print(f"  │ LTR      : {ltr_c:<22} │")
    print(f"  │ Available: {avail_c:<22} │")
    print(f"  │ Service  : {svc_c:<22} │")
    print(f"  │ NRV      : {nrv_c:<22} │")
    print(f"  └─────────────────────────────────┘")

    summary = "\n".join([
        f"❝{date_str} {day_str}❞", "",
        f"✦ Total        : {total}", "",
        f"✦ Rented STR   : {str_c}", "",
        f"✦ Service      : {svc_c}", "",
        f"✦ Available    : {avail_c}", "",
        f"✦ Lease        : {lease_c}", "",
        f"✦ Longterm     : {ltr_c}", "",
        f"✦ NRV          : {nrv_c}",
    ])

    lines = []
    if available_v:
        lines.append("AVAILABLE")
        lines.append("-" * 30)
        for i, v in enumerate(available_v, 1):
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
# Post to Slack
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

    # STEP 1 — Fleet list
    print("\n[1] Fetching MKV fleet ...")
    vehicles = fetch_vehicles()
    if not vehicles:
        print("  No vehicles returned — exiting")
        raise SystemExit(1)

    # STEP 2 — Status per vehicle
    print("\n[2] Fetching availability status per vehicle ...")
    vehicle_statuses = fetch_vehicle_statuses(vehicles)

    # STEP 3 — Contract type for booked vehicles only
    booked_vids        = {vid for vid, s in vehicle_statuses.items() if s == "booked"}
    booked_plates_norm = set(
        normalize_plate(get_plate(v))
        for v in vehicles
        if get_vehicle_id(v) in booked_vids
    )
    print(f"\n[3] Fetching contract types for {len(booked_plates_norm)} booked vehicles ...")
    contract_types = fetch_contract_types(booked_plates_norm)

    # BUILD & POST
    print("\n[4] Building Slack message ...")
    msg = build_message(vehicles, vehicle_statuses, contract_types)

    print("\n[5] Posting to Slack ...")
    post_slack(msg)
