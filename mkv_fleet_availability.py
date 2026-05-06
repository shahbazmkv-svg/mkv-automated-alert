"""
MKV Luxury - Fleet Availability
Accurate classification using bookings API.
"""
import os, json, requests, re
from datetime import datetime, timezone, timedelta

SLACK_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
APPIC_KEY     = os.environ.get("APPIC_KEY", "")
SLACK_CHANNEL = "C0B0TGBDCDU"   # #mkvtest
DUBAI_TZ      = timezone(timedelta(hours=4))
BLOCK_LIMIT   = 2900

ALL_VEHICLES_URL = "https://www.appicfleet.com/appiccar-apis-mkv/get-all-vehicles.php"
BOOKINGS_URL     = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-bookings.php"
CHECKINOUT_URL   = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-checkin-checkout.php"

CONTACT_FOOTER = (
    "📱 +971 52 940 9280\n📱 +971 56 279 4545\n☎️  +971 4 238 8987\n"
    "🌐 https://www.mkvluxury.com/\n📸 https://www.instagram.com/mkvluxurydubai/\n"
    "✉️  contact@mkvluxury.com\n📍 Al Jreena Street 41, Al Qouz Industrial Third, Dubai, UAE"
)

def now_dubai():
    return datetime.now(DUBAI_TZ)

def norm(plate):
    """Normalize plate to digits only for comparison."""
    digits = re.sub(r"[^0-9]", "", str(plate or ""))
    return digits.lstrip("0") if digits else ""

def duration_days(start, end):
    try:
        return (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days
    except:
        return 0

def get_plate(v):
    for key in ["plate", "vehiclePlate", "plateNo", "plate_no"]:
        val = str(v.get(key, "") or "").strip()
        if val and val != "0":
            return val
    return ""

def fmt_name(v):
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

def fetch_vehicles():
    r = requests.get(ALL_VEHICLES_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    all_v = r.json().get("data", [])
    str_v = [v for v in all_v if float(v.get("dailyrent", 0) or 0) > 0]
    print(f"  Vehicles: total={len(all_v)} | STR={len(str_v)}")
    return str_v

def fetch_active_contracts(today):
    """
    Classify confirmed active contracts by duration:
    - STR      : <= 30 days
    - Lease    : 31-365 days  
    - Longterm : > 365 days
    Only CONFIRMED status — excludes draft/closed.
    Deduplicates by plate (keeps longest contract per plate).
    """
    start_window = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=400)).strftime("%Y-%m-%d")
    r = requests.post(BOOKINGS_URL, data={
        "key": APPIC_KEY, "startDate": start_window, "endDate": today
    }, timeout=20)
    r.raise_for_status()
    bookings = r.json().get("bookings", [])

    # Only confirmed, active today
    active = {}
    for b in bookings:
        status = (b.get("status") or "").lower().strip()
        start  = (b.get("startDate") or "").strip()
        end    = (b.get("endDate")   or "").strip()
        plate  = norm(b.get("vehiclePlate") or "")

        if status != "confirmed":
            continue
        if not plate or not (start <= today <= end):
            continue

        days = duration_days(start, end)
        # Keep longest contract per plate
        if plate not in active or days > active[plate]["days"]:
            active[plate] = {"days": days, "booking": b}

    str_norm     = set()
    lease_norm   = set()
    longterm_norm = set()

    for plate_n, info in active.items():
        days = info["days"]
        if days > 365:
            longterm_norm.add(plate_n)
        elif days > 30:
            lease_norm.add(plate_n)
        else:
            str_norm.add(plate_n)

    print(f"  Confirmed active → STR:{len(str_norm)} | Lease:{len(lease_norm)} | Longterm:{len(longterm_norm)}")
    return str_norm, lease_norm, longterm_norm

def fetch_checkinout(today, direction):
    r = requests.post(CHECKINOUT_URL, data={
        "key": APPIC_KEY, "startDate": today, "endDate": today, "direction": direction
    }, timeout=20)
    r.raise_for_status()
    records = r.json().get("data") or []
    print(f"  CheckIn/Out [{direction}]: {len(records)} records")
    return records

def build_message(today):
    now      = now_dubai()
    date_str = now.strftime("%B %d, %Y").upper()
    day_str  = now.strftime("%A").upper()

    print("[1/4] Fetching STR vehicles...")
    str_vehicles = fetch_vehicles()
    str_total    = len(str_vehicles)

    print("[2/4] Fetching active contracts...")
    str_norm, lease_norm, longterm_norm = fetch_active_contracts(today)

    print("[3/4] Fetching check-ins/check-outs today...")
    out_records = fetch_checkinout(today, "Out")
    in_records  = fetch_checkinout(today, "In")

    print("[4/4] Classifying fleet...")

    rented   = len(str_norm)
    lease    = len(lease_norm)
    longterm = len(longterm_norm)
    returning = len(in_records)
    delivery  = len(out_records)

    # All rented norm plates (STR + Lease + Longterm)
    all_rented_norm = str_norm | lease_norm | longterm_norm

    # Available = STR vehicles NOT in any active contract
    available_v = [
        v for v in str_vehicles
        if norm(get_plate(v)) not in all_rented_norm
    ]
    available = len(available_v)

    print(f"  Result → STR:{str_total} | Rented:{rented} | Lease:{lease} | Longterm:{longterm} | Available:{available} | Returning:{returning} | Delivery:{delivery}")

    summary = "\n".join([
        f"❝{date_str} {day_str}❞", "",
        f"✦ Total STR    : {str_total}", "",
        f"✦ Rented STR   : {rented}", "",
        f"✦ Returning    : {returning}", "",
        f"✦ Delivery     : {delivery}", "",
        f"✦ Available    : {available}", "",
        f"✦ Lease        : {lease}", "",
        f"✦ Longterm     : {longterm}", "",
        f"✦ Garage       : —", "",
        f"✦ Service      : —", "",
        f"✦ NRV          : —",
    ])

    lines = []
    if available_v:
        lines.append("AVAILABLE")
        lines.append("-" * 30)
        for i, v in enumerate(available_v, 1):
            lines.append(f"{i}. {fmt_name(v)}")

    if out_records:
        lines.append("")
        lines.append("DELIVERIES TODAY")
        lines.append("-" * 30)
        for i, b in enumerate(out_records, 1):
            lines.append(f"{i}. {b.get('vehicleName','')} [{b.get('vehiclePlate','')}] — {b.get('customerName','')}")

    if in_records:
        lines.append("")
        lines.append("RETURNING TODAY")
        lines.append("-" * 30)
        for i, b in enumerate(in_records, 1):
            lines.append(f"{i}. {b.get('vehicleName','')} [{b.get('vehiclePlate','')}] — {b.get('customerName','')}")

    lines += ["", "For inquiries please contact this number", "", CONTACT_FOOTER]

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```{summary}```"}},
        {"type": "divider"},
        *text_to_blocks("\n".join(lines)),
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "MKV Active Fleet • Auto-posted daily 10:00 AM Dubai time"}]},
    ]
    return {"blocks": blocks, "text": f"MKV Fleet Availability — {date_str} {day_str}"}

def post_slack(message):
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"},
        data=json.dumps({"channel": SLACK_CHANNEL, "username": "MKV Fleet Status",
                         "icon_emoji": ":car:", "unfurl_links": False, "unfurl_media": False, **message}),
        timeout=15,
    )
    res = r.json()
    if not res.get("ok"):
        print(f"Slack error: {res.get('error')}")
        raise SystemExit(1)
    print("✅ Posted to Slack")

if __name__ == "__main__":
    print("=" * 60)
    print("  MKV FLEET AVAILABILITY")
    print("=" * 60)
    today = now_dubai().strftime("%Y-%m-%d")
    print(f"  Date: {today}")
    print("=" * 60)
    msg = build_message(today)
    post_slack(msg)
    print("=" * 60)
    print("  Done.")
    print("=" * 60)
