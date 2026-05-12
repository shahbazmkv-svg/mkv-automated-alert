"""
MKV Luxury – Fleet Availability
=================================
MASTER_FLEET = hardcoded plate + category + name
Status fetched daily from get-mkv-available-vehicle.php

To add a new vehicle: add one line to MASTER_FLEET
To change category: update "cat" field

Categories: str | lease | ltr | nrv

Availability API status:
  isBooked=False + "available" → Available
  isBooked=True                → Rented (use cat for STR/Lease/LTR)
  "gone for service"           → Service
  "accident" / "damage"        → NRV
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

CONTACT_FOOTER = (
    "📱 +971 56 279 4545\n☎️  +971 4 238 8987\n"
    "🌐 https://www.mkvluxury.com/\n📸 https://www.instagram.com/mkvluxurydubai/\n"
    "✉️  contact@mkvluxury.com\n📍 Al Jreena Street 41, Al Qouz Industrial Third, Dubai, UAE"
)

# ─────────────────────────────────────────────────────────
# MASTER FLEET
# plate  = exactly as stored in Appic
# cat    = str | lease | ltr | nrv
# name   = display name for available list
#
# To add: {"plate": "XX12345", "cat": "str", "name": "VEHICLE NAME"}
# ─────────────────────────────────────────────────────────
MASTER_FLEET = [
    # ── STR ──────────────────────────────────────────────────────────
    {"plate": "Y 97019",  "cat": "str",   "name": "FERRARI PUROSANGUE"},
    {"plate": "I 47203",  "cat": "str",   "name": "MORGAN SUPERSPORT"},
    {"plate": "U 24545",  "cat": "str",   "name": "MERCEDES G63 BLACK 2025"},
    {"plate": "O66789",   "cat": "str",   "name": "MERCEDES G63 BRABUS"},
    {"plate": "X55789",   "cat": "str",   "name": "MERCEDES S500"},
    {"plate": "L94545",   "cat": "str",   "name": "RANGE ROVER SPORT GRAY"},
    {"plate": "T55789",   "cat": "str",   "name": "RANGE ROVER SPORT BLACK"},
    {"plate": "J77540",   "cat": "str",   "name": "RANGE ROVER SVR BLACK"},
    {"plate": "AA68620",  "cat": "str",   "name": "RANGE ROVER VELAR"},
    {"plate": "CC83762",  "cat": "str",   "name": "LAND ROVER DEFENDER V8"},
    {"plate": "AA78043",  "cat": "str",   "name": "LAND ROVER DEFENDER 130 V6"},
    {"plate": "AA77491",  "cat": "str",   "name": "FORD MUSTANG GT V8 CONVERTIBLE RED"},
    {"plate": "AA77490",  "cat": "str",   "name": "FORD MUSTANG GT COUPE WHITE"},
    {"plate": "Y72712",   "cat": "str",   "name": "CHEVROLET CORVETTE"},
    {"plate": "E23652",   "cat": "str",   "name": "LOTUS EMIRA"},
    {"plate": "AA78051",  "cat": "str",   "name": "BMW 735i"},
    {"plate": "K70691",   "cat": "str",   "name": "BMW 520i"},
    {"plate": "D70688",   "cat": "str",   "name": "BMW 420i"},
    {"plate": "K19443",   "cat": "str",   "name": "MERCEDES GLB 250"},
    {"plate": "AA78042",  "cat": "str",   "name": "CHEVROLET TAHOE"},
    {"plate": "CC 69367", "cat": "str",   "name": "GMC YUKON"},
    {"plate": "W46015",   "cat": "str",   "name": "AUDI RS Q3"},
    {"plate": "Z89438",   "cat": "str",   "name": "AUDI A6"},
    {"plate": "Z92156",   "cat": "str",   "name": "AUDI A6"},
    {"plate": "Z90158",   "cat": "str",   "name": "AUDI A3"},
    {"plate": "B15789",   "cat": "str",   "name": "FORD MUSTANG BLACK/YELLOW"},
    {"plate": "BB60137",  "cat": "str",   "name": "MERCEDES G63 2026 RETRO"},
    {"plate": "O94545",   "cat": "str",   "name": "LAMBORGHINI URUS MY20 YELLOW"},
    {"plate": "X44789",   "cat": "str",   "name": "CADILLAC ESCALADE"},
    {"plate": "X33789",   "cat": "str",   "name": "BENTLEY BENTAYGA MANSORY"},
    {"plate": "J47041",   "cat": "str",   "name": "MCLAREN ARTURA"},
    {"plate": "EE 42165", "cat": "str",   "name": "PORSCHE 911"},
    {"plate": "T64545",   "cat": "str",   "name": "PORSCHE GT4 RS"},
    {"plate": "H75037",   "cat": "str",   "name": "RANGE ROVER SVR GRAY/BLUE"},
    {"plate": "W97521",   "cat": "str",   "name": "LAMBORGHINI HURACAN EVO SPYDER"},
    # ── LEASE ─────────────────────────────────────────────────────────
    {"plate": "U74545",   "cat": "lease", "name": "FERRARI 296 GTB"},
    {"plate": "S 66789",  "cat": "lease", "name": "MERCEDES G63 WHITE 2025"},
    {"plate": "T3660",    "cat": "lease", "name": "MERCEDES G63 BLUE"},
    {"plate": "AA78067",  "cat": "lease", "name": "MERCEDES C200"},
    {"plate": "CC94084",  "cat": "lease", "name": "RANGE ROVER SPORT WHITE"},
    {"plate": "X33567",   "cat": "lease", "name": "FORD BRONCO"},
    {"plate": "N27852",   "cat": "lease", "name": "AUDI Q3"},
    {"plate": "Z90154",   "cat": "lease", "name": "AUDI A3"},
    {"plate": "F98103",   "cat": "lease", "name": "KIA SPORTAGE WHITE"},
    {"plate": "F98438",   "cat": "lease", "name": "KIA SORENTO"},
    {"plate": "W81946",   "cat": "lease", "name": "JETOUR T2 BLUE"},
    {"plate": "D68539",   "cat": "lease", "name": "DONGFENG FORTHING S7"},
    {"plate": "BB53403",  "cat": "lease", "name": "GAC M8 2026"},
    {"plate": "C69703",   "cat": "lease", "name": "NISSAN PATROL WHITE"},
    {"plate": "T78242",   "cat": "lease", "name": "JETOUR T2 BROWN"},
    # ── LTR ───────────────────────────────────────────────────────────
    {"plate": "S39810",   "cat": "ltr",   "name": "NISSAN PATROL"},
    {"plate": "F83209",   "cat": "ltr",   "name": "RANGE ROVER SPORT BLACK"},
    {"plate": "1243",     "cat": "ltr",   "name": "ROLLS ROYCE GRAY"},
    {"plate": "H23155",   "cat": "ltr",   "name": "NISSAN PATROL"},
    {"plate": "F 97580",  "cat": "ltr",   "name": "CADILLAC ESCALADE SPORT"},
    {"plate": "K19503",   "cat": "ltr",   "name": "MERCEDES GLB 250"},
    # ── NRV ───────────────────────────────────────────────────────────
    {"plate": "P38848",   "cat": "nrv",   "name": "MERCEDES G63 BLACK 2024"},
    {"plate": "Z66246",   "cat": "nrv",   "name": "GMC YUKON"},
    {"plate": "H31727",   "cat": "nrv",   "name": "TOYOTA LAND CRUISER"},
    {"plate": "Y97020",   "cat": "nrv",   "name": "KIA K5"},
    {"plate": "Y97018",   "cat": "nrv",   "name": "KIA CERATO"},
    {"plate": "R26603",   "cat": "nrv",   "name": "SUZUKI SWIFT"},
]

def now_dubai():
    return datetime.now(DUBAI_TZ)

def plate_key(plate: str) -> str:
    return re.sub(r"\s+", "", str(plate).upper())

def now_dubai():
    return datetime.now(DUBAI_TZ)

# ─────────────────────────────────────────────────────────
# STEP 1: Match master fleet plates to Appic vehicleIDs
# ─────────────────────────────────────────────────────────
def fetch_vehicle_ids() -> dict:
    """Returns { plate_key -> vehicleID }"""
    try:
        r     = requests.post(MKV_VEHICLES_URL, data={"key": APPIC_KEY}, timeout=20)
        r.raise_for_status()
        resp  = r.json()
        all_v = resp if isinstance(resp, list) else resp.get("data", resp.get("vehicles", []))

        lookup = {}
        for v in all_v:
            raw = str(v.get("plate", "") or "").strip()
            vid = str(v.get("vehicleID", "") or "").strip()
            if raw and vid:
                lookup[plate_key(raw)] = vid

        matched, missing = [], []
        result = {}
        for f in MASTER_FLEET:
            pk  = plate_key(f["plate"])
            vid = lookup.get(pk)
            if vid:
                result[f["plate"]] = vid
                matched.append(f["plate"])
            else:
                missing.append(f["plate"])

        print(f"  Appic total : {len(all_v)} | Matched: {len(matched)} / {len(MASTER_FLEET)}")
        if missing:
            print(f"  ⚠️  NOT FOUND in Appic (check plate):")
            for p in missing:
                print(f"      '{p}'")
        return result

    except Exception as ex:
        print(f"  ❌ Vehicles API error: {ex}")
        return {}

# ─────────────────────────────────────────────────────────
# STEP 2: Fetch availability status per vehicle
# Returns: { plate -> "available" | "booked" | "service" | "nrv" }
# ─────────────────────────────────────────────────────────
def fetch_statuses(plate_to_vid: dict) -> dict:
    today_str    = now_dubai().strftime("%Y-%m-%d")
    tomorrow_str = (now_dubai() + timedelta(days=1)).strftime("%Y-%m-%d")
    statuses     = {}

    for f in MASTER_FLEET:
        plate = f["plate"]
        cat   = f["cat"]
        vid   = plate_to_vid.get(plate)

        # NRV vehicles — no API call needed, always NRV
        if cat == "nrv":
            statuses[plate] = "nrv"
            continue

        if not vid:
            # Not found in Appic — mark unknown, skip API call
            statuses[plate] = "unknown"
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

            # Print full response for any non-standard status
            if raw not in ("booked", "available", ""):
                print(f"  🔍 SPECIAL: plate={plate} vid={vid} raw='{raw}' full={resp}")

            if "accident" in raw or "damage" in raw:
                result = "nrv"
            elif "service" in raw or "garage" in raw or "gone for" in raw:
                result = "service"
            elif is_booked or "booked" in raw or "rented" in raw:
                result = "booked"
            else:
                result = "available"

            statuses[plate] = result

        except Exception as ex:
            print(f"  ⚠️  Error: plate={plate} vid={vid}: {ex}")
            statuses[plate] = "available"

    from collections import Counter
    print(f"  Status summary: {dict(Counter(statuses.values()))}")
    return statuses

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
def build_message(statuses: dict):
    now      = now_dubai()
    date_str = now.strftime("%B %d, %Y").upper()
    day_str  = now.strftime("%A").upper()

    str_v       = []
    lease_v     = []
    ltr_v       = []
    available_v = []
    service_v   = []
    nrv_v       = []

    for f in MASTER_FLEET:
        plate  = f["plate"]
        cat    = f["cat"]
        status = statuses.get(plate, "available")

        if status == "service":
            service_v.append(f)
        elif status == "nrv" or cat == "nrv":
            nrv_v.append(f)
        elif status == "booked":
            if cat == "ltr":
                ltr_v.append(f)
            elif cat == "lease":
                lease_v.append(f)
            else:
                str_v.append(f)
        else:
            # available or unknown
            available_v.append(f)

    total   = len(MASTER_FLEET)
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
    print(f"  Master fleet   : {len(MASTER_FLEET)} vehicles")

    print("\n[1] Fetching vehicleIDs from Appic ...")
    plate_to_vid = fetch_vehicle_ids()

    print(f"\n[2] Fetching availability status ...")
    statuses = fetch_statuses(plate_to_vid)

    print("\n[3] Building Slack message ...")
    msg = build_message(statuses)

    print("\n[4] Posting to Slack ...")
    post_slack(msg)
