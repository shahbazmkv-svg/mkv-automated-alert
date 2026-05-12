"""
MKV Luxury – Fleet Availability
=================================
Master fleet defined by plate list below.
To add a new vehicle: add its plate to MASTER_PLATES.

FLOW:
  1. get-mkv-vehicles.php       → match plates to get vehicleID + name
  2. get-mkv-available-vehicle.php → fetch status per vehicleID
       "available"        → Available
       "gone for service" → Service
       "accident/damage"  → NRV
       "booked"           → go to step 3
  3. get-mkv-bookings.php       → find active contract → STR / Lease / LTR
       1–30 days   → STR
       31–365 days → Lease
       366+ days   → LTR

OUTPUT:
  Summary  → Total / STR / Lease / LTR / Available / Service / NRV
  List     → AVAILABLE vehicles only (name + plate)
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

# ─────────────────────────────────────────────────────────
# MASTER PLATE LIST — add new vehicles here
# Format: "FULL PLATE AS IN APPIC"
# ─────────────────────────────────────────────────────────
MASTER_PLATES = [
    "Y 97019",   # 1  FERRARI PUROSANGUE
    "I 47203",   # 2  MORGAN SUPERSPORT
    "U 24545",   # 3  MERCEDES G63 BLACK 2025
    "O66789",    # 4  MERCEDES G63 BRABUS
    "X55789",    # 5  MERCEDES S500
    "L94545",    # 6  RANGE ROVER SPORT GRAY
    "T55789",    # 7  RANGE ROVER SPORT BLACK
    "J77540",    # 8  RANGE ROVER SVR BLACK
    "AA68620",   # 9  RANGE ROVER VELAR
    "CC83762",   # 10 LAND ROVER DEFENDER V8
    "AA78043",   # 11 LAND ROVER DEFENDER 130 V6
    "AA77491",   # 12 FORD MUSTANG CONVERTIBLE RED
    "AA77490",   # 13 FORD MUSTANG COUPE WHITE
    "Y72712",    # 14 CHEVROLET CORVETTE
    "E23652",    # 15 LOTUS EMIRA
    "AA78051",   # 16 BMW 735i
    "K70691",    # 17 BMW 520i
    "D70688",    # 18 BMW 420i
    "K19443",    # 19 MERCEDES GLB 250
    "AA78042",   # 20 CHEVROLET TAHOE
    "CC69367",   # 21 GMC YUKON
    "W46015",    # 22 AUDI RS Q3
    "Z89438",    # 23 AUDI A6
    "Z92156",    # 24 AUDI A6
    "Z90158",    # 25 AUDI A3
    "B15789",    # 26 FORD MUSTANG BLACK/YELLOW
    "BB60137",   # 27 MERCEDES G63 2026 RETRO
    "O94545",    # 28 LAMBORGHINI URUS MY20 YELLOW
    "X44789",    # 29 CADILLAC ESCALADE
    "X33789",    # 30 BENTLEY BENTAYGA MANSORY
    "U74545",    # 31 FERRARI 296 GTB
    "S 66789",   # 32 MERCEDES G63 WHITE 2025
    "T3660",     # 33 MERCEDES G63 BLUE
    "AA78067",   # 34 MERCEDES C200
    "CC94084",   # 35 RANGE ROVER SPORT WHITE
    "X33567",    # 36 FORD BRONCO
    "N27852",    # 37 AUDI Q3
    "Z90154",    # 38 AUDI A3
    "F98103",    # 39 KIA SPORTAGE WHITE
    "F98438",    # 40 KIA SORENTO
    "W81946",    # 41 JETOUR T2 BLUE
    "D68539",    # 42 DONGFENG FORTHING S7
    "BB53403",   # 43 GAC M8 2026
    "S39810",    # 44 NISSAN PATROL
    "F83209",    # 45 RANGE ROVER SPORT BLACK
    "V1243",     # 46 ROLLS ROYCE GRAY
    "H23155",    # 47 NISSAN PATROL
    "F 97580",   # 48 CADILLAC ESCALADE SPORT
    "K19503",    # 49 MERCEDES GLB 250
    "P38848",    # 50 MERCEDES G63 BLACK 2024
    "Z66246",    # 51 GMC YUKON
    "H31727",    # 52 TOYOTA LAND CRUISER
    "Y97020",    # 53 KIA K5
    "Y97018",    # 54 KIA CERATO
    "R26603",    # 55 SUZUKI SWIFT
    "J47041",    # 56 MCLAREN ARTURA
    "EE42165",   # 57 PORSCHE 911
    "C69703",    # 58 NISSAN PATROL WHITE
    "T64545",    # 59 PORSCHE GT4 RS
    "H75037",    # 60 RANGE ROVER SVR GRAY/BLUE
    "W97521",    # 61 LAMBORGHINI HURACAN EVO SPYDER
    "T78242",    # 62 JETOUR T2 BROWN
]

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

def plate_key(plate: str) -> str:
    """Uppercase, remove spaces — for matching. 'EE 42165' → 'EE42165'"""
    return re.sub(r"\s+", "", str(plate).upper())

def normalize_digits(plate: str) -> str:
    """Digits only, no leading zeros — for bookings API matching."""
    digits = re.sub(r"[^0-9]", "", str(plate))
    return digits.lstrip("0") if digits else str(plate)

# ─────────────────────────────────────────────────────────
# STEP 1: Match master plates to Appic vehicle records
# ─────────────────────────────────────────────────────────
def fetch_fleet() -> list:
    """
    Returns list of dicts: { plate, vehicle_id, name, appic_data }
    Only for plates in MASTER_PLATES.
    """
    try:
        r     = requests.post(MKV_VEHICLES_URL, data={"key": APPIC_KEY}, timeout=20)
        r.raise_for_status()
        resp  = r.json()
        all_v = resp if isinstance(resp, list) else resp.get("data", resp.get("vehicles", []))
        print(f"  Appic total    : {len(all_v)} vehicles")

        # Build lookup: plate_key → vehicle record
        appic_lookup = {}
        for v in all_v:
            raw_plate = str(v.get("plate", "") or "").strip()
            if raw_plate:
                appic_lookup[plate_key(raw_plate)] = v

        fleet = []
        not_found = []

        for plate in MASTER_PLATES:
            pk = plate_key(plate)
            v  = appic_lookup.get(pk)
            if v:
                vid  = str(v.get("vehicleID", "") or "").strip()
                name = str(v.get("vehicle_name", "") or
                           f"{v.get('make','')} {v.get('model','')}").strip().upper()
                fleet.append({
                    "plate":      plate,
                    "plate_key":  pk,
                    "vehicle_id": vid,
                    "name":       name,
                    "raw":        v
                })
            else:
                not_found.append(plate)

        print(f"  Master plates  : {len(MASTER_PLATES)}")
        print(f"  Matched        : {len(fleet)}")
        if not_found:
            print(f"  ⚠️  NOT FOUND in Appic (plate mismatch — fix in Appic):")
            for p in not_found:
                print(f"      '{p}' (key='{plate_key(p)}')")

        return fleet

    except Exception as ex:
        print(f"  ❌ Vehicles API error: {ex}")
        return []

# ─────────────────────────────────────────────────────────
# STEP 2: Get availability status per vehicle
# Returns: { vehicle_id -> "available" | "booked" | "service" | "nrv" }
# ─────────────────────────────────────────────────────────
def fetch_statuses(fleet: list) -> dict:
    today_str    = now_dubai().strftime("%Y-%m-%d")
    tomorrow_str = (now_dubai() + timedelta(days=1)).strftime("%Y-%m-%d")
    statuses     = {}

    for f in fleet:
        vid   = f["vehicle_id"]
        plate = f["plate"]

        if not vid:
            print(f"  ⚠️  No vehicleID for plate={plate}")
            statuses[plate] = "available"
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

            if "accident" in raw or "damage" in raw:
                result = "nrv"
            elif "service" in raw or "garage" in raw or "gone for" in raw:
                result = "service"
            elif is_booked or "booked" in raw or "rented" in raw:
                result = "booked"
            elif "available" in raw:
                result = "available"
            else:
                result = "available"
                print(f"  ⚠️  Unknown: plate={plate} vid={vid} raw='{raw}' full={resp}")

            statuses[plate] = result
            print(f"  {result.upper():<12} plate={plate:<14} vid={vid} status='{raw}'")

        except Exception as ex:
            print(f"  ⚠️  Error: plate={plate} vid={vid}: {ex}")
            statuses[plate] = "available"

    from collections import Counter
    print(f"  Status summary : {dict(Counter(statuses.values()))}")
    return statuses

# ─────────────────────────────────────────────────────────
# STEP 3: Contract type for booked vehicles
# Returns: { normalized_digits_plate -> "str" | "lease" | "ltr" }
# ─────────────────────────────────────────────────────────
def fetch_contract_types(booked_plates: list) -> dict:
    if not booked_plates:
        return {}

    booked_norms = {normalize_digits(p) for p in booked_plates}

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

            bp    = str(b.get("vehiclePlate", "") or "").strip()
            bnorm = normalize_digits(bp)
            if bnorm not in booked_norms:
                continue

            sd = parse_date(b.get("startDate"))
            ed = parse_date(b.get("endDate"))
            if sd is None or ed is None:
                continue
            if not (sd <= today <= ed):
                continue

            duration = (ed - sd).days
            ctype    = "ltr" if duration >= 366 else "lease" if duration >= 31 else "str"

            if bnorm not in plate_type or PRIORITY[ctype] > PRIORITY[plate_type[bnorm]]:
                plate_type[bnorm] = ctype
                print(f"  CONTRACT: plate={bp:<14} {sd}→{ed} ({duration}d) → {ctype.upper()}")

        from collections import Counter
        print(f"  Contract types : {dict(Counter(plate_type.values()))}")
        return plate_type

    except Exception as ex:
        print(f"  ❌ Bookings API error: {ex}")
        return {}

# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────
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
# BUILD SLACK MESSAGE
# ─────────────────────────────────────────────────────────
def build_message(fleet, statuses, contract_types):
    now      = now_dubai()
    date_str = now.strftime("%B %d, %Y").upper()
    day_str  = now.strftime("%A").upper()

    str_v       = []
    lease_v     = []
    ltr_v       = []
    available_v = []
    service_v   = []
    nrv_v       = []

    for f in fleet:
        plate  = f["plate"]
        norm   = normalize_digits(plate)
        status = statuses.get(plate, "available")
        ctype  = contract_types.get(norm, "str")

        if status == "service":
            service_v.append(f)
        elif status == "nrv":
            nrv_v.append(f)
        elif status == "booked":
            if ctype == "ltr":
                ltr_v.append(f)
            elif ctype == "lease":
                lease_v.append(f)
            else:
                str_v.append(f)
        else:
            available_v.append(f)

    total   = len(fleet)
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
        for i, f in enumerate(available_v, 1):
            lines.append(f"{i}. {f['name']} [{f['plate']}]")
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
# POST TO SLACK
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

    print(f"\n[1] Matching {len(MASTER_PLATES)} master plates to Appic ...")
    fleet = fetch_fleet()
    if not fleet:
        print("  No vehicles matched — exiting")
        raise SystemExit(1)

    print(f"\n[2] Fetching availability status for {len(fleet)} vehicles ...")
    statuses = fetch_statuses(fleet)

    booked_plates = [f["plate"] for f in fleet if statuses.get(f["plate"]) == "booked"]
    print(f"\n[3] Fetching contract types for {len(booked_plates)} booked vehicles ...")
    contract_types = fetch_contract_types(booked_plates)

    print("\n[4] Building Slack message ...")
    msg = build_message(fleet, statuses, contract_types)

    print("\n[5] Posting to Slack ...")
    post_slack(msg)
