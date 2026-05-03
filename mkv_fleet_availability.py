"""
MKV Luxury – Fleet Availability

Classification rules (dailyrent > 0 vehicles only):
  1. RENTED STR      — confirmed contract active today (start <= today <= end)
  2. RETURNING TODAY — checkin direction=In today
  3. DELIVERY TODAY  — draft contract with start date = today (expected out)
  4. AVAILABLE       — no confirmed or draft contract touching today

Plate matching:
  All 3 APIs use different formats. Extract digits only for comparison:
    Bookings:    '26603'   (digits only)
    CheckIn/Out: 'R26603'  (letter prefix)
    Vehicles:    'B15789'  (letter prefix), 'G/9358' (slash format)
  plate_digits() → '26603' across all three ✅
"""
import os, json, re, requests
from datetime import datetime, timezone, timedelta

SLACK_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
APPIC_KEY     = os.environ.get("APPIC_KEY", "96QQYxPRVRTiHjL0tEmgP0cr5FkLvED0")
SLACK_CHANNEL = "C0B0TGBDCDU"
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

def parse_date(date_str: str):
    if not date_str:
        return None
    date_str = date_str.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None

def plate_digits(plate) -> str:
    """
    Extract digits only — the common key across all 3 Appic APIs.
      Bookings:    '26603'   → '26603'
      CheckIn/Out: 'R26603'  → '26603'  | 'CC83762' → '83762'
      Vehicles:    'B15789'  → '15789'  | 'G/9358'  → '9358'
    """
    return re.sub(r"[^0-9]", "", str(plate or ""))

def get_plate_raw(v: dict) -> str:
    for key in ["plate", "vehiclePlate", "plateNo", "plate_no", "number_plate", "licensePlate"]:
        val = str(v.get(key, "") or "").strip()
        if val and val not in ("0", ""):
            return val
    return ""

def fmt_name(v: dict) -> str:
    make  = v.get("make",  "").strip().upper()
    model = v.get("model", "").strip().upper()
    year  = str(v.get("year", "")).strip()
    color = v.get("color", "").strip().upper()
    plate = get_plate_raw(v)
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

# ══════════════════════════════════════════════════════════════════════════════
# API FETCHERS
# ══════════════════════════════════════════════════════════════════════════════

def fetch_vehicles() -> list:
    try:
        r = requests.get(ALL_VEHICLES_URL,
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        all_v  = r.json().get("data", [])
        active = [v for v in all_v if float(v.get("dailyrent", 0) or 0) > 0]
        print(f"  Vehicles: total={len(all_v)} | dailyrent>0={len(active)}")
        return active
    except Exception as ex:
        print(f"  [ERROR] Vehicles: {ex}")
        return []


def fetch_booking_sets(today) -> dict:
    """
    Returns:
      confirmed_plates — digit plates with confirmed contract active today
                         (start <= today <= end)
      draft_today_plates — digit plates with draft contract starting today
                           (start == today)  → Delivery Today
    """
    start_str = (now_dubai() - timedelta(days=365)).strftime("%Y-%m-%d")
    end_str   = (now_dubai() + timedelta(days=365)).strftime("%Y-%m-%d")
    try:
        r = requests.post(BOOKINGS_URL,
                          data={"key": APPIC_KEY,
                                "startDate": start_str,
                                "endDate":   end_str},
                          timeout=20)
        r.raise_for_status()
        bookings = r.json().get("bookings", [])
        print(f"  Bookings API: {len(bookings)} records")

        confirmed_plates   = set()   # confirmed & active today → Rented
        draft_today_plates = set()   # draft & start==today     → Delivery

        status_counts = {}
        for b in bookings:
            status = str(b.get("status") or "").lower().strip()
            status_counts[status] = status_counts.get(status, 0) + 1

            plate = plate_digits(b.get("vehiclePlate", ""))
            if not plate:
                continue

            s = parse_date(b.get("startDate") or "")
            e = parse_date(b.get("endDate")   or "")
            if not s or not e:
                continue

            if status == "confirmed":
                # Active confirmed contract spanning today
                if s <= today <= e:
                    confirmed_plates.add(plate)

            elif status == "draft":
                # Draft contract starting today = expected delivery
                if s == today:
                    draft_today_plates.add(plate)

        print(f"  Status breakdown: {status_counts}")
        print(f"  Confirmed active today:  {len(confirmed_plates)} → {sorted(confirmed_plates)}")
        print(f"  Draft delivery today:    {len(draft_today_plates)} → {sorted(draft_today_plates)}")
        return {"confirmed": confirmed_plates, "draft_today": draft_today_plates}

    except Exception as ex:
        print(f"  [ERROR] Bookings: {ex}")
        return {"confirmed": set(), "draft_today": set()}


def fetch_checkinout_plates(today, direction: str) -> set:
    """Digit plates physically checked out (Out) or in (In) today."""
    today_str = today.strftime("%Y-%m-%d")
    try:
        r = requests.post(CHECKINOUT_URL,
                          data={"key":       APPIC_KEY,
                                "startDate": today_str,
                                "endDate":   today_str,
                                "direction": direction},
                          timeout=20)
        r.raise_for_status()
        records = r.json().get("data", [])
        print(f"  CheckIn/Out [{direction}]: {len(records)} records")

        plates = set()
        for rec in records:
            raw = str(rec.get("vehiclePlate") or rec.get("plate") or
                      rec.get("plateNo") or "").strip()
            d = plate_digits(raw)
            if d:
                plates.add(d)

        print(f"  {direction} plates: {sorted(plates)}")
        return plates
    except Exception as ex:
        print(f"  [ERROR] CheckIn/Out [{direction}]: {ex}")
        return set()


# ══════════════════════════════════════════════════════════════════════════════
# CLASSIFY
# ══════════════════════════════════════════════════════════════════════════════

def classify(vehicles, confirmed_plates, draft_today_plates,
             checkout_plates, checkin_plates):
    """
    Rule priority (first match wins):
      1. checkin today (In)          → RETURNING TODAY
      2. confirmed contract today    → RENTED STR
      3. draft contract, start today → DELIVERY TODAY
      4. no contract                 → AVAILABLE
    Garage/Service from vehicle availability field (separate from above).
    """
    returning_v = []
    rented_v    = []
    delivery_v  = []
    available_v = []
    garage_v    = []
    service_v   = []

    for v in vehicles:
        d          = plate_digits(get_plate_raw(v))
        avail      = (v.get("availability") or "").lower().strip()
        status_raw = (v.get("status")       or "").lower().strip()

        if avail in ("garage",) or status_raw in ("garage",):
            garage_v.append(v)
        elif avail in ("service", "maintenance") or status_raw in ("service", "maintenance"):
            service_v.append(v)
        elif d and d in checkin_plates:
            returning_v.append(v)        # physically returning today
        elif d and d in confirmed_plates:
            rented_v.append(v)           # confirmed contract active today
        elif d and d in draft_today_plates:
            delivery_v.append(v)         # draft contract starting today
        else:
            available_v.append(v)        # no contract → available

    print(f"  Result → Rented:{len(rented_v)} | Returning:{len(returning_v)} | "
          f"Delivery:{len(delivery_v)} | Available:{len(available_v)} | "
          f"Garage:{len(garage_v)} | Service:{len(service_v)}")

    return rented_v, returning_v, delivery_v, available_v, garage_v, service_v


# ══════════════════════════════════════════════════════════════════════════════
# SLACK
# ══════════════════════════════════════════════════════════════════════════════

def text_to_blocks(text):
    blocks, chunk = [], ""
    for line in text.split("\n"):
        cand = chunk + line + "\n"
        if len(cand) > BLOCK_LIMIT:
            if chunk:
                blocks.append({"type": "section", "text": {"type": "mrkdwn",
                                "text": f"```{chunk.rstrip()}```"}})
            chunk = line + "\n"
        else:
            chunk = cand
    if chunk.strip():
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                        "text": f"```{chunk.rstrip()}```"}})
    return blocks


def build_message(rented_v, returning_v, delivery_v,
                  available_v, garage_v, service_v, total):
    now      = now_dubai()
    date_str = now.strftime("%B %d, %Y").upper()
    day_str  = now.strftime("%A").upper()

    summary = "\n".join([
        f"❝{date_str} {day_str}❞", "",
        f"✦ Total           : {total}", "",
        f"✦ Rented STR      : {len(rented_v)}", "",
        f"✦ Returning Today : {len(returning_v)}", "",
        f"✦ Delivery Today  : {len(delivery_v)}", "",
        f"✦ Available       : {len(available_v)}", "",
        f"✦ Garage          : {len(garage_v)}", "",
        f"✦ Service         : {len(service_v)}", "",
        f"✦ Lease           : 0", "",
        f"✦ Longterm        : 0", "",
        f"✦ NRV             : 0",
    ])

    lines = []

    def section(emoji, title, lst):
        if not lst:
            return
        if lines:
            lines.append("")
        lines.append(f"{emoji} {title}")
        lines.append("-" * 30)
        for i, v in enumerate(lst, 1):
            lines.append(f"{i}. {fmt_name(v)}")

    section("🔁", "RETURNING TODAY", returning_v)
    section("🚗", "DELIVERY TODAY",  delivery_v)
    section("✅", "AVAILABLE",       available_v)
    section("🔧", "GARAGE",          garage_v)
    section("⚙️",  "SERVICE",        service_v)

    lines += ["", "For inquiries please contact us", "", CONTACT_FOOTER]

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
        headers={"Authorization": f"Bearer {SLACK_TOKEN}",
                 "Content-Type": "application/json"},
        data=json.dumps({"channel": SLACK_CHANNEL, "username": "MKV Fleet Status",
                         "icon_emoji": ":car:", "unfurl_links": False,
                         "unfurl_media": False, **message}),
        timeout=15,
    )
    res = r.json()
    if not res.get("ok"):
        print(f"  Slack error: {res.get('error')}")
        raise SystemExit(1)
    print("  ✅ Posted to Slack")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  MKV FLEET AVAILABILITY")
    print("  " + now_dubai().strftime("%d %b %Y | %I:%M %p Dubai Time"))
    print("=" * 60)

    today = now_dubai().date()

    print("\n[1/4] Fetching vehicles (dailyrent > 0)...")
    vehicles = fetch_vehicles()
    if not vehicles:
        print("  No vehicles — aborting")
        raise SystemExit(1)

    print("\n[2/4] Fetching bookings (confirmed + draft)...")
    booking_sets = fetch_booking_sets(today)
    confirmed_plates   = booking_sets["confirmed"]
    draft_today_plates = booking_sets["draft_today"]

    print("\n[3/4] Fetching check-ins / check-outs today...")
    checkin_plates  = fetch_checkinout_plates(today, "In")
    checkout_plates = fetch_checkinout_plates(today, "Out")

    print("\n[4/4] Classifying fleet...")
    (rented_v, returning_v, delivery_v,
     available_v, garage_v, service_v) = classify(
        vehicles, confirmed_plates, draft_today_plates,
        checkout_plates, checkin_plates)

    msg = build_message(rented_v, returning_v, delivery_v,
                        available_v, garage_v, service_v,
                        total=len(vehicles))
    post_slack(msg)

    print("\n" + "=" * 60)
    print("  Done.")
    print("=" * 60)
