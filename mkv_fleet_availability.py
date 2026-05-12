"""
MKV Luxury – Fleet Availability
=================================
API 1: get-mkv-vehicle-assignments.php → counts (STR / Lease / LTR / Service / NRV)
API 2: get-mkv-vehicles.php + get-mkv-available-vehicle.php → list of available vehicles

Total = 62 (hardcoded master fleet size)
"""
import os, json, re, requests
from datetime import datetime, timezone, timedelta

SLACK_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
APPIC_KEY     = os.environ.get("APPIC_KEY", "")
SLACK_CHANNEL = "C0B0TGBDCDU"
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

TOTAL_FLEET = 62  # update when fleet changes

# Master plate list — only used to identify available vehicles for the list
MASTER_PLATES = [
    "Y 97019", "I 47203", "U 24545", "O66789",  "X55789",
    "L94545",  "T55789",  "J77540",  "AA68620", "CC83762",
    "AA78043", "AA77491", "AA77490", "Y72712",  "E23652",
    "AA78051", "K70691",  "D70688",  "K19443",  "AA78042",
    "CC 69367","W46015",  "Z89438",  "Z92156",  "Z90158",
    "B15789",  "BB60137", "O94545",  "X44789",  "X33789",
    "U74545",  "S 66789", "T3660",   "AA78067", "CC94084",
    "X33567",  "N27852",  "Z90154",  "F98103",  "F98438",
    "W81946",  "D68539",  "BB53403", "S39810",  "F83209",
    "1243",    "H23155",  "F 97580", "K19503",  "P38848",
    "Z66246",  "H31727",  "Y97020",  "Y97018",  "R26603",
    "J47041",  "EE 42165","C69703",  "T64545",  "H75037",
    "W97521",  "T78242",
]

def now_dubai():
    return datetime.now(DUBAI_TZ)

def plate_key(plate: str) -> str:
    return re.sub(r"\s+", "", str(plate).upper())

def get_vehicle_id(v: dict) -> str:
    for k in ["vehicleID", "id", "vehicleId"]:
        val = str(v.get(k, "") or "").strip()
        if val and val not in ("0", ""):
            return val
    return ""

# ─────────────────────────────────────────────────────────
# 1. Get counts from assignments API
# ─────────────────────────────────────────────────────────
def fetch_counts() -> dict:
    try:
        r = requests.post(MKV_ASSIGNMENTS_URL, data={"key": APPIC_KEY}, timeout=20)
        r.raise_for_status()
        counts = r.json().get("counts", {})
        print(f"  Assignments API: {counts}")
        return counts
    except Exception as ex:
        print(f"  ❌ Assignments API error: {ex}")
        return {}

# ─────────────────────────────────────────────────────────
# 2. Get list of available vehicles
# ─────────────────────────────────────────────────────────
def fetch_available_vehicles() -> list:
    today_str    = now_dubai().strftime("%Y-%m-%d")
    tomorrow_str = (now_dubai() + timedelta(days=1)).strftime("%Y-%m-%d")

    # Get all vehicles and match to master plates
    try:
        r     = requests.post(MKV_VEHICLES_URL, data={"key": APPIC_KEY}, timeout=20)
        r.raise_for_status()
        resp  = r.json()
        all_v = resp if isinstance(resp, list) else resp.get("data", resp.get("vehicles", []))

        # Build lookup
        lookup = {}
        for v in all_v:
            raw = str(v.get("plate", "") or "").strip()
            if raw:
                lookup[plate_key(raw)] = v

        # Match master plates
        fleet = []
        for plate in MASTER_PLATES:
            v = lookup.get(plate_key(plate))
            if v:
                vid  = get_vehicle_id(v)
                name = str(v.get("vehicle_name") or
                           f"{v.get('make','')} {v.get('model','')}").strip().upper()
                fleet.append({"plate": plate, "vid": vid, "name": name})

        print(f"  Fleet matched  : {len(fleet)} / {len(MASTER_PLATES)}")

    except Exception as ex:
        print(f"  ❌ Vehicles API error: {ex}")
        return []

    # Check availability per vehicle
    available = []
    for f in fleet:
        if not f["vid"]:
            continue
        try:
            r = requests.post(MKV_AVAIL_URL, data={
                "key":       APPIC_KEY,
                "startDate": today_str,
                "endDate":   tomorrow_str,
                "vehicleID": f["vid"]
            }, timeout=15)
            r.raise_for_status()
            resp      = r.json()
            raw       = str(resp.get("status", "") or "").lower().strip()
            is_booked = resp.get("isBooked", False)

            if not is_booked and "available" in raw:
                available.append(f)
                print(f"  ✅ AVAILABLE: {f['name']} [{f['plate']}]")

        except Exception as ex:
            print(f"  ⚠️  Error: plate={f['plate']}: {ex}")

    print(f"  Available      : {len(available)}")
    return available

# ─────────────────────────────────────────────────────────
# 3. Build Slack message
# ─────────────────────────────────────────────────────────
def build_message(counts: dict, available_vehicles: list):
    now      = now_dubai()
    date_str = now.strftime("%B %d, %Y").upper()
    day_str  = now.strftime("%A").upper()

    str_c   = counts.get("shortTermRental", 0)
    lease_c = counts.get("lease", 0)
    ltr_c   = counts.get("longTermRental", 0)
    svc_c   = counts.get("service", 0)
    nrv_c   = counts.get("unavailable", 0)
    avail_c = len(available_vehicles)

    print(f"\n  ┌─────────────────────────────────┐")
    print(f"  │ FLEET SUMMARY                   │")
    print(f"  │ Total    : {TOTAL_FLEET:<22} │")
    print(f"  │ STR      : {str_c:<22} │")
    print(f"  │ Lease    : {lease_c:<22} │")
    print(f"  │ LTR      : {ltr_c:<22} │")
    print(f"  │ Available: {avail_c:<22} │")
    print(f"  │ Service  : {svc_c:<22} │")
    print(f"  │ NRV      : {nrv_c:<22} │")
    print(f"  └─────────────────────────────────┘")

    summary = "\n".join([
        f"❝{date_str} {day_str}❞", "",
        f"✦ Total        : {TOTAL_FLEET}", "",
        f"✦ Rented STR   : {str_c}", "",
        f"✦ Service      : {svc_c}", "",
        f"✦ Available    : {avail_c}", "",
        f"✦ Lease        : {lease_c}", "",
        f"✦ Longterm     : {ltr_c}", "",
        f"✦ NRV          : {nrv_c}",
    ])

    lines = []
    if available_vehicles:
        lines.append("AVAILABLE")
        lines.append("-" * 30)
        for i, f in enumerate(available_vehicles, 1):
            lines.append(f"{i}. {f['name']} [{f['plate']}]")
        lines.append("")

    lines += ["For inquiries please contact this number", "", CONTACT_FOOTER]

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

    print("\n[1] Fetching fleet counts ...")
    counts = fetch_counts()

    print("\n[2] Fetching available vehicles ...")
    available = fetch_available_vehicles()

    print("\n[3] Building Slack message ...")
    msg = build_message(counts, available)

    print("\n[4] Posting to Slack ...")
    post_slack(msg)
