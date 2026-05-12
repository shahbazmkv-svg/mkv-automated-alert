"""
MKV Luxury – Fleet Availability
=================================
API 1: get-mkv-vehicles.php          → vehicle list (names, plates, IDs)
API 2: get-mkv-available-vehicle.php → per-vehicle status (available/booked/service/nrv)

PLATE_CATEGORY uses full plate key (letters + digits, no spaces) for uniqueness.
All 62 MKV fleet vehicles are mapped. Update when fleet changes.

Status logic (priority order):
  1. SERVICE  → availability API returns "service" / "gone for service" / "garage"
  2. NRV      → plate in NRV category (Accident/Damage vehicles)
  3. BOOKED   → split by PLATE_CATEGORY → STR / Lease / LTR
  4. AVAILABLE → not booked, not service, not NRV

Slack output:
  Summary → Total / STR / Lease / LTR / Available / Service / NRV
  List    → AVAILABLE vehicles only
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

CONTACT_FOOTER = (
    "📱 +971 56 279 4545\n☎️  +971 4 238 8987\n"
    "🌐 https://www.mkvluxury.com/\n📸 https://www.instagram.com/mkvluxurydubai/\n"
    "✉️  contact@mkvluxury.com\n📍 Al Jreena Street 41, Al Qouz Industrial Third, Dubai, UAE"
)

# ─────────────────────────────────────────────────────────
# MASTER FLEET CATEGORY MAP
# Key   = plate letters+digits uppercased, spaces removed
# Value = "str" | "lease" | "ltr" | "nrv"
# ─────────────────────────────────────────────────────────
PLATE_CATEGORY = {
    # ── STR ──────────────────────────────────────────────
    "Y97019":   "str",   # FERRARI PUROSANGUE
    "I47203":   "str",   # MORGAN SUPERSPORT
    "U24545":   "str",   # MERCEDES G63 BLACK 2025
    "O66789":   "str",   # MERCEDES G63 BRABUS
    "X55789":   "str",   # MERCEDES S500
    "L94545":   "str",   # RANGE ROVER SPORT GRAY
    "T55789":   "str",   # RANGE ROVER SPORT BLACK
    "J77540":   "str",   # RANGE ROVER SVR BLACK
    "AA68620":  "str",   # RANGE ROVER VELAR
    "CC83762":  "str",   # LAND ROVER DEFENDER V8
    "AA78043":  "str",   # LAND ROVER DEFENDER 130 V6
    "AA77491":  "str",   # FORD MUSTANG CONVERTIBLE RED
    "AA77490":  "str",   # FORD MUSTANG COUPE WHITE
    "Y72712":   "str",   # CHEVROLET CORVETTE
    "E23652":   "str",   # LOTUS EMIRA
    "AA78051":  "str",   # BMW 735i
    "K70691":   "str",   # BMW 520i
    "D70688":   "str",   # BMW 420i
    "K19443":   "str",   # MERCEDES GLB 250
    "AA78042":  "str",   # CHEVROLET TAHOE
    "CC69367":  "str",   # GMC YUKON
    "W46015":   "str",   # AUDI RS Q3
    "Z89438":   "str",   # AUDI A6
    "Z92156":   "str",   # AUDI A6
    "Z90158":   "str",   # AUDI A3
    "B15789":   "str",   # FORD MUSTANG BLACK/YELLOW
    "BB60137":  "str",   # MERCEDES G63 2026 RETRO
    "O94545":   "str",   # LAMBORGHINI URUS MY20 YELLOW
    "X44789":   "str",   # CADILLAC ESCALADE
    "X33789":   "str",   # BENTLEY BENTAYGA MANSORY
    "J47041":   "str",   # MCLAREN ARTURA          (SERVICE)
    "EE42165":  "str",   # PORSCHE 911             (SERVICE)
    "T64545":   "str",   # PORSCHE GT4 RS          (SERVICE)
    "H75037":   "str",   # RANGE ROVER SVR GRAY/BLUE (SERVICE)
    "W97521":   "str",   # LAMBORGHINI HURACAN EVO SPYDER (SERVICE)
    "N56026":   "str",   # LAMBORGHINI URUS (N 56026)
    # ── LEASE ────────────────────────────────────────────
    "U74545":   "lease", # FERRARI 296 GTB
    "S66789":   "lease", # MERCEDES G63 WHITE 2025
    "T3660":    "lease", # MERCEDES G63 BLUE
    "AA78067":  "lease", # MERCEDES C200
    "CC94084":  "lease", # RANGE ROVER SPORT WHITE
    "X33567":   "lease", # FORD BRONCO
    "N27852":   "lease", # AUDI Q3
    "Z90154":   "lease", # AUDI A3
    "F98103":   "lease", # KIA SPORTAGE WHITE
    "F98438":   "lease", # KIA SORENTO
    "W81946":   "lease", # JETOUR T2 BLUE
    "D68539":   "lease", # DONGFENG FORTHING S7
    "BB53403":  "lease", # GAC M8 2026
    "C69703":   "lease", # NISSAN PATROL WHITE     (SERVICE)
    "T78242":   "lease", # JETOUR T2 BROWN         (SERVICE)
    # ── LTR ──────────────────────────────────────────────
    "S39810":   "ltr",   # NISSAN PATROL
    "F83209":   "ltr",   # RANGE ROVER SPORT BLACK
    "V1243":    "ltr",   # ROLLS ROYCE GRAY
    "H23155":   "ltr",   # NISSAN PATROL
    "F97580":   "ltr",   # CADILLAC ESCALADE SPORT
    "K19503":   "ltr",   # MERCEDES GLB 250
    # ── NRV ──────────────────────────────────────────────
    "P38848":   "nrv",   # MERCEDES G63 BLACK 2024
    "Z66246":   "nrv",   # GMC YUKON
    "H31727":   "nrv",   # TOYOTA LAND CRUISER
    "Y97020":   "nrv",   # KIA K5
    "Y97018":   "nrv",   # KIA CERATO
    "R26603":   "nrv",   # SUZUKI SWIFT
}

def now_dubai():
    return datetime.now(DUBAI_TZ)

def plate_key(plate: str) -> str:
    """Normalize plate to letters+digits, uppercase, no spaces. E.g. 'AA 68620' → 'AA68620'"""
    return re.sub(r"[^A-Z0-9]", "", str(plate).upper())

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
# 1. Fetch MKV fleet vehicle list
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
        print(f"  Vehicles total : {len(all_v)} | MKV fleet: {len(active)}")

        # Warn: master list plates missing from Appic
        appic_keys = set(plate_key(get_plate(v)) for v in active)
        missing = [p for p in PLATE_CATEGORY if p not in appic_keys]
        if missing:
            print(f"  ⚠️  In master list but NOT in Appic (set rate>0 to fix):")
            for p in missing:
                print(f"      {p}")

        # Warn: Appic vehicles not in master list
        extra = [v for v in active if plate_key(get_plate(v)) not in PLATE_CATEGORY]
        if extra:
            print(f"  ⚠️  In Appic but NOT in master list (add to PLATE_CATEGORY):")
            for v in extra:
                print(f"      vehicleID={get_vehicle_id(v)} plate={get_plate(v)} "
                      f"name={v.get('vehicle_name', '')}")

        return active
    except Exception as ex:
        print(f"  ❌ Vehicles API error: {ex}")
        return []

# ─────────────────────────────────────────────────────────
# 2. Get per-vehicle status from availability API
# ─────────────────────────────────────────────────────────
def fetch_vehicle_statuses(vehicles: list) -> dict:
    today_str    = now_dubai().strftime("%Y-%m-%d")
    tomorrow_str = (now_dubai() + timedelta(days=1)).strftime("%Y-%m-%d")
    statuses     = {}

    for v in vehicles:
        vid      = get_vehicle_id(v)
        plate    = get_plate(v)
        pkey     = plate_key(plate)
        category = PLATE_CATEGORY.get(pkey, "str")

        # NRV — no API call needed
        if category == "nrv":
            statuses[vid] = "nrv"
            print(f"  NRV           : vehicleID={vid:<6} plate={plate}")
            continue

        if not vid:
            statuses[vid] = "available"
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
                statuses[vid] = "nrv"
            elif "service" in raw or "garage" in raw or "gone for" in raw:
                statuses[vid] = "service"
                print(f"  SERVICE       : vehicleID={vid:<6} plate={plate} status='{raw}'")
            elif is_booked or "booked" in raw or "rented" in raw:
                statuses[vid] = "booked"
            elif "available" in raw:
                statuses[vid] = "available"
            else:
                statuses[vid] = "available"
                print(f"  ⚠️  Unknown   : vehicleID={vid:<6} plate={plate} raw='{raw}'")

        except Exception as ex:
            print(f"  ⚠️  Error      : vehicleID={vid} plate={plate}: {ex}")
            statuses[vid] = "available"

    from collections import Counter
    counts = Counter(statuses.values())
    print(f"  Status summary : {dict(counts)}")
    return statuses

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
# 3. Build Slack message
# ─────────────────────────────────────────────────────────
def build_message(vehicles, vehicle_statuses):
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
        vid      = get_vehicle_id(v)
        pkey     = plate_key(get_plate(v))
        status   = vehicle_statuses.get(vid, "available")
        category = PLATE_CATEGORY.get(pkey, "str")

        if status == "service":
            service_v.append(v)
        elif status == "nrv":
            nrv_v.append(v)
        elif status == "booked":
            if category == "ltr":
                ltr_v.append(v)
            elif category == "lease":
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
# 4. Post to Slack
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

    print("\n[1] Fetching MKV fleet from Appic ...")
    vehicles = fetch_vehicles()
    if not vehicles:
        print("  No vehicles returned — exiting")
        raise SystemExit(1)

    print("\n[2] Fetching per-vehicle availability status ...")
    vehicle_statuses = fetch_vehicle_statuses(vehicles)

    print("\n[3] Building Slack message ...")
    msg = build_message(vehicles, vehicle_statuses)

    print("\n[4] Posting to Slack ...")
    post_slack(msg)
