"""
MKV Luxury – Fleet Availability
=================================
API 1: get-mkv-vehicles.php          → STR fleet list (dailyrent > 0)
API 2: get-mkv-available-vehicle.php → availability status per vehicle (Available / Gone for service / Accident/Damage / Rented)
API 3: get-mkv-bookings.php          → classify active STR contracts only (plates must exist in STR fleet)

Key fix: bookings API returns ALL contracts across all fleets.
We only classify a booking if its plate exists in the STR vehicle list.

Contract type by duration (STR fleet only):
  STR      : 1–30  days
  Lease    : 31–730 days  ← only if plate is in STR fleet AND has a long contract
  Longterm : 731+  days

Appic availability API status values:
  "Available"        → available
  "Gone for service" → service
  "Accident/Damage"  → NRV
  Rented             → determined by active bookings
"""
import os, json, re, requests
from datetime import datetime, timezone, timedelta

SLACK_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
APPIC_KEY     = os.environ.get("APPIC_KEY", "")
SLACK_CHANNEL = "C0B0TGBDCDU"   # #mkvtest
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
    digits = re.sub(r"[^0-9]", "", str(plate))
    return digits.lstrip("0") if digits else str(plate)

def get_plate(v: dict) -> str:
    for key in ["plate", "vehiclePlate", "plateNo", "plate_no", "number_plate", "licensePlate"]:
        val = str(v.get(key, "") or "").strip()
        if val and val != "0":
            return val
    return ""

def get_vehicle_id(v: dict) -> str:
    for k in ["vehicleID", "id", "vehicleId", "vehicle_id", "ID"]:
        val = str(v.get(k, "") or "").strip()
        if val and val not in ("0", ""):
            return val
    return ""

# ─────────────────────────────────────────────────────────
# 1. Fetch STR fleet
# ─────────────────────────────────────────────────────────
def fetch_vehicles() -> list:
    """POST get-mkv-vehicles.php → STR fleet (dailyrent > 0)."""
    try:
        r = requests.post(MKV_VEHICLES_URL, data={"key": APPIC_KEY}, timeout=20)
        r.raise_for_status()
        resp  = r.json()
        all_v = resp if isinstance(resp, list) else resp.get("data", resp.get("vehicles", []))

        if all_v:
            s = all_v[0]
            print(f"  Vehicle keys : {list(s.keys())}")
            print(f"  Sample       : vehicleID={s.get('vehicleID')} | plate={s.get('plate')} | dailyrent={s.get('dailyrent')}")

        active = [v for v in all_v if float(v.get("dailyrent", 0) or 0) > 0]
        print(f"  Total: {len(all_v)} | STR fleet (dailyrent>0): {len(active)}")
        return active
    except Exception as ex:
        print(f"  ❌ Vehicles API error: {ex}")
        return []

# ─────────────────────────────────────────────────────────
# 2. Availability status per vehicle from dedicated API
#    Returns: { vehicleID -> "available" | "service" | "nrv" | "rented" }
# ─────────────────────────────────────────────────────────
def fetch_vehicle_statuses(vehicles: list) -> dict:
    """
    Calls get-mkv-available-vehicle.php per vehicle for today.
    Uses the status string from Appic to determine:
      Available        → available
      Gone for service → service
      Accident/Damage  → nrv
      Rented/booked    → rented (fallback — bookings API takes priority)
    """
    today_str    = now_dubai().strftime("%Y-%m-%d")
    tomorrow_str = (now_dubai() + timedelta(days=1)).strftime("%Y-%m-%d")
    statuses     = {}

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
            resp = r.json()

            # Print raw response for first vehicle to understand structure
            if len(statuses) == 0:
                print(f"  Avail API sample (vehicleID={vid}): {json.dumps(resp)[:300]}")

            # Extract status string — try all known keys
            raw = ""
            if isinstance(resp, dict):
                raw = str(
                    resp.get("status") or
                    resp.get("availability") or
                    resp.get("vehicleStatus") or
                    resp.get("message") or
                    ""
                ).lower().strip()

                # Also check nested data
                data = resp.get("data") or resp.get("vehicle") or {}
                if not raw and isinstance(data, dict):
                    raw = str(
                        data.get("status") or
                        data.get("availability") or ""
                    ).lower().strip()

            # Classify
            if "accident" in raw or "damage" in raw:
                statuses[vid] = "nrv"
            elif "service" in raw:
                statuses[vid] = "service"
            elif "available" in raw:
                statuses[vid] = "available"
            elif "rent" in raw or "booked" in raw or "unavailable" in raw:
                statuses[vid] = "rented"
            else:
                statuses[vid] = "unknown"

        except Exception as ex:
            print(f"  ⚠️  Avail API error for vehicleID={vid}: {ex}")
            statuses[vid] = "unknown"

    # Summary
    from collections import Counter
    counts = Counter(statuses.values())
    print(f"  Avail API results: {dict(counts)}")
    return statuses

# ─────────────────────────────────────────────────────────
# 3. Classify active contracts — STR fleet plates ONLY
# ─────────────────────────────────────────────────────────
def fetch_contract_type_map(str_plates_norm: set) -> dict:
    """
    Returns { normalized_plate -> 'str' | 'lease' | 'longterm' }
    ONLY for plates that exist in the STR fleet.
    This prevents long-term/lease fleet contracts from polluting STR fleet status.
    """
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

        plate_type  = {}
        skipped     = 0

        for b in bookings:
            status = str(b.get("status") or b.get("bookingStatus") or "").lower().strip()
            if status in ("cancelled", "canceled", "voided", "void", "deleted"):
                continue

            plate = str(b.get("vehiclePlate", "") or "").strip()
            if not plate:
                continue

            norm = normalize_plate(plate)

            # ── KEY FIX: skip if plate not in STR fleet ──
            if norm not in str_plates_norm:
                skipped += 1
                continue

            sd = parse_date(b.get("startDate"))
            ed = parse_date(b.get("endDate"))
            if sd is None or ed is None:
                continue
            if not (sd <= today <= ed):
                continue

            duration = (ed - sd).days
            ctype    = "longterm" if duration >= 731 else "lease" if duration >= 31 else "str"
            agr      = b.get("agreementNo") or b.get("agr_no") or "N/A"

            print(f"  CONTRACT: AGR={agr} | plate={plate} | {sd}→{ed} ({duration}d) [{ctype.upper()}]")

            if norm not in plate_type or PRIORITY[ctype] > PRIORITY[plate_type[norm]]:
                plate_type[norm] = ctype

        print(f"  Skipped {skipped} bookings for non-STR plates")
        str_c = sum(1 for t in plate_type.values() if t == "str")
        lea_c = sum(1 for t in plate_type.values() if t == "lease")
        lt_c  = sum(1 for t in plate_type.values() if t == "longterm")
        print(f"  STR fleet contracts: STR={str_c} | Lease={lea_c} | Longterm={lt_c}")
        return plate_type

    except Exception as ex:
        print(f"  ❌ Bookings API error: {ex}")
        return {}

# ─────────────────────────────────────────────────────────
# Helpers
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
def build_message(vehicles, contract_map, vehicle_statuses):
    now      = now_dubai()
    date_str = now.strftime("%B %d, %Y").upper()
    day_str  = now.strftime("%A").upper()

    str_v       = []
    lease_v     = []
    longterm_v  = []
    available_v = []
    service_v   = []
    nrv_v       = []

    for v in vehicles:
        plate_norm = normalize_plate(get_plate(v))
        vid        = get_vehicle_id(v)
        contract   = contract_map.get(plate_norm)
        api_status = vehicle_statuses.get(vid, "unknown")

        # Priority 1: active booking contract
        if contract == "str":
            str_v.append(v)
        elif contract == "lease":
            lease_v.append(v)
        elif contract == "longterm":
            longterm_v.append(v)

        # Priority 2: Appic availability API status
        elif api_status == "nrv":
            nrv_v.append(v)
        elif api_status == "service":
            service_v.append(v)
        elif api_status == "rented":
            # API says rented but no contract found — treat as STR
            str_v.append(v)
        else:
            # available or unknown — treat as available
            available_v.append(v)

    total    = len(vehicles)
    rented   = len(str_v)
    lease    = len(lease_v)
    longterm = len(longterm_v)
    avail    = len(available_v)
    service  = len(service_v)
    nrv      = len(nrv_v)

    print(f"\n  ┌─────────────────────────────────┐")
    print(f"  │ FLEET SUMMARY                   │")
    print(f"  │ Total    : {total:<22} │")
    print(f"  │ STR      : {rented:<22} │")
    print(f"  │ Lease    : {lease:<22} │")
    print(f"  │ Longterm : {longterm:<22} │")
    print(f"  │ Available: {avail:<22} │")
    print(f"  │ Service  : {service:<22} │")
    print(f"  │ NRV      : {nrv:<22} │")
    print(f"  └─────────────────────────────────┘")

    summary = "\n".join([
        f"❝{date_str} {day_str}❞", "",
        f"✦ Total        : {total}", "",
        f"✦ Rented STR   : {rented}", "",
        f"✦ Service      : {service}", "",
        f"✦ Available    : {avail}", "",
        f"✦ Lease        : {lease}", "",
        f"✦ Longterm     : {longterm}", "",
        f"✦ NRV          : {nrv}",
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
    section("SERVICE",    service_v)
    section("NRV",        nrv_v)
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

    print("\n[1] Fetching STR fleet from get-mkv-vehicles.php ...")
    vehicles = fetch_vehicles()
    if not vehicles:
        print("  No vehicles returned — exiting")
        raise SystemExit(1)

    # Build normalised plate set for STR fleet
    str_plates_norm = set(normalize_plate(get_plate(v)) for v in vehicles)
    print(f"  STR plate set  : {len(str_plates_norm)} plates")

    print("\n[2] Fetching availability status per vehicle ...")
    vehicle_statuses = fetch_vehicle_statuses(vehicles)

    print("\n[3] Classifying STR fleet contracts (bookings API — STR plates only) ...")
    contract_map = fetch_contract_type_map(str_plates_norm)

    print("\n[4] Building Slack message ...")
    msg = build_message(vehicles, contract_map, vehicle_statuses)

    print("\n[5] Posting to Slack ...")
    post_slack(msg)
