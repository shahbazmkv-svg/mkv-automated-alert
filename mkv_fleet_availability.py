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
SLACK_CHANNEL = "C0ABW8AGMRU"   # #mkv-fleet-availability
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

TOTAL_FLEET = 62  # must always match len(MASTER_PLATES) — update both together

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

APPIC_BOOKINGS_URL = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-bookings.php"
SKIP_STATUSES      = {"cancelled", "canceled", "voided", "void", "deleted"}

def fetch_bookings_data() -> dict:
    """Fetch next booking dates per plate + today's deliveries and returns."""
    today     = now_dubai().strftime("%Y-%m-%d")
    lookback  = (now_dubai() - timedelta(days=30)).strftime("%Y-%m-%d")
    lookahead = (now_dubai() + timedelta(days=90)).strftime("%Y-%m-%d")
    try:
        r = requests.post(
            APPIC_BOOKINGS_URL,
            data={"key": APPIC_KEY, "startDate": lookback, "endDate": lookahead},
            timeout=20
        )
        bookings = r.json().get("bookings", [])

        next_booking   = {}   # plate → earliest future startDate str
        to_deliver     = []   # startDate == today
        to_return      = []   # endDate == today and startDate < today

        for b in bookings:
            status = (b.get("status") or "").lower().strip()
            if status in SKIP_STATUSES:
                continue

            raw_plate = str(b.get("vehiclePlate") or "").strip()
            pk        = plate_key(raw_plate)
            start     = (b.get("startDate") or "").strip()
            end       = (b.get("endDate")   or "").strip()
            customer  = (b.get("customerName") or "N/A").strip().title()
            vehicle   = (b.get("vehicleName")  or "N/A").strip().title()
            s_time    = (b.get("startTime") or "")[:5]
            e_time    = (b.get("endTime")   or "")[:5]

            # Next booking date per plate
            if start > today:
                if pk not in next_booking or start < next_booking[pk]:
                    next_booking[pk] = start

            # Today's deliveries
            if start == today:
                to_deliver.append({
                    "vehicle": vehicle, "plate": raw_plate,
                    "customer": customer, "time": s_time,
                })

            # Today's returns
            if end == today and start < today:
                to_return.append({
                    "vehicle": vehicle, "plate": raw_plate,
                    "customer": customer, "time": e_time,
                })

        print(f"  Next booking dates: {len(next_booking)} plates mapped")
        print(f"  To deliver today  : {len(to_deliver)}")
        print(f"  To return today   : {len(to_return)}")
        return {
            "next_booking": next_booking,
            "to_deliver":   to_deliver,
            "to_return":    to_return,
        }
    except Exception as ex:
        print(f"  ❌ Bookings API error: {ex}")
        return {"next_booking": {}, "to_deliver": [], "to_return": []}


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

def check_fleet_mismatch(counts: dict) -> dict | None:
    """
    Compare TOTAL_FLEET (hardcoded) against len(MASTER_PLATES) — the real source of truth.
    Appic assignments total is shown for info only, not used for comparison.
    """
    str_c   = counts.get("shortTermRental", 0)
    lease_c = counts.get("lease", 0)
    ltr_c   = counts.get("longTermRental", 0)
    svc_c   = counts.get("service", 0)
    nrv_c   = counts.get("nrv", 0)
    appic_total = str_c + lease_c + ltr_c + svc_c + nrv_c

    master_count = len(MASTER_PLATES)
    diff         = TOTAL_FLEET - master_count

    if diff == 0:
        print(f"  ✅ Fleet count matches: TOTAL_FLEET={TOTAL_FLEET} = MASTER_PLATES={master_count}")
        return None

    direction = f"+{diff}" if diff > 0 else str(diff)
    action    = "Add new plate(s) to MASTER_PLATES" if diff > 0 else "Remove plate(s) from MASTER_PLATES"
    print(f"  ⚠️  Fleet mismatch: TOTAL_FLEET={TOTAL_FLEET}, MASTER_PLATES={master_count}, diff={direction}")
    return {
        "hardcoded":   TOTAL_FLEET,
        "master":      master_count,
        "appic":       appic_total,
        "diff":        direction,
        "action":      action,
        "breakdown":   f"STR: {str_c}  Lease: {lease_c}  LTR: {ltr_c}  Service: {svc_c}  NRV: {nrv_c}  (Appic info only)",
    }

def post_mismatch_alert(mismatch: dict):
    """Post a fleet mismatch alert to Slack."""
    date_str = now_dubai().strftime("%d %b %Y | %I:%M %p")
    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "⚠️ FLEET COUNT MISMATCH DETECTED", "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            f"```\n"
            f"{'TOTAL_FLEET (hardcoded)':<26}: {mismatch['hardcoded']}\n"
            f"{'MASTER_PLATES (actual)':<26}: {mismatch['master']}\n"
            f"{'Difference':<26}: {mismatch['diff']}\n"
            f"{'─' * 40}\n"
            f"Appic assignments (info only):\n"
            f"{mismatch['breakdown']}\n"
            f"```"
        )}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*Action needed:* {mismatch['action']} in `mkv_fleet_availability.py` and update `TOTAL_FLEET`"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"Detected: {date_str} · Auto-check via Fleet Availability script"}]},
    ]
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"},
        json={"channel": SLACK_CHANNEL, "username": "MKV Fleet Alert",
              "icon_emoji": ":warning:", "blocks": blocks,
              "text": f"⚠️ Fleet mismatch: hardcoded={mismatch['hardcoded']}, Appic={mismatch['appic']}"},
        timeout=15,
    )
    res = r.json()
    if res.get("ok"):
        print("  ✅ Mismatch alert posted to Slack")
    else:
        print(f"  ❌ Alert post error: {res.get('error')}")

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
# 3. Generate fleet header image
# ─────────────────────────────────────────────────────────
def generate_fleet_image(total, lease_c, ltr_c, str_c, avail_c, date_str) -> bytes | None:
    try:
        from PIL import Image, ImageDraw, ImageFont
        import io

        W, H       = 900, 160
        DARK_BG    = (26, 26, 24)
        BOX_BG     = (38, 38, 36)
        BOX_BORDER = (60, 60, 58)
        WHITE      = (255, 255, 255)
        GRAY       = (140, 135, 128)
        COLORS     = {
            "total":     (55, 138, 221),
            "lease":     (186, 117, 23),
            "ltr":       (83, 74, 183),
            "str":       (226, 75, 74),
            "available": (99, 153, 34),
        }

        metrics = [
            ("Total Fleet",      total,   "total"),
            ("Lease",            lease_c, "lease"),
            ("Long-term",        ltr_c,   "ltr"),
            ("Short-term (STR)", str_c,   "str"),
            ("Available",        avail_c, "available"),
        ]

        img  = Image.new("RGB", (W, H), DARK_BG)
        draw = ImageDraw.Draw(img)

        font_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        ]
        font_bold_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        ]

        def load_font(paths, size):
            for p in paths:
                try: return ImageFont.truetype(p, size)
                except: pass
            return ImageFont.load_default()

        font_label = load_font(font_paths, 14)
        font_value = load_font(font_bold_paths, 36)
        font_title = load_font(font_bold_paths, 15)

        # Title
        draw.text((20, 14), f"MKV Fleet Availability  —  {date_str}", font=font_title, fill=WHITE)

        # 5 metric boxes
        box_w = 160
        gap   = 12
        start_x = (W - (5 * box_w + 4 * gap)) // 2
        box_y, box_h = 48, 92

        for label, value, key in metrics:
            x = start_x + metrics.index((label, value, key)) * (box_w + gap)
            draw.rounded_rectangle([x, box_y, x+box_w, box_y+box_h], radius=10,
                                   fill=BOX_BG, outline=BOX_BORDER, width=1)
            lb = draw.textlength(label, font=font_label)
            draw.text((x + (box_w - lb)//2, box_y + 10), label, font=font_label, fill=GRAY)
            vb = draw.textlength(str(value), font=font_value)
            draw.text((x + (box_w - vb)//2, box_y + 34), str(value), font=font_value, fill=COLORS[key])

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        print("  ✅ Fleet header image generated")
        return buf.getvalue()

    except Exception as e:
        print(f"  ⚠️  Image generation failed: {e}")
        return None


def upload_image_to_slack(image_bytes: bytes, filename: str, channel: str) -> bool:
    """Upload image to Slack using v2 upload API."""
    try:
        headers = {"Authorization": f"Bearer {SLACK_TOKEN}"}

        # Step 1 — get upload URL
        r1 = requests.post(
            "https://slack.com/api/files.getUploadURLExternal",
            headers=headers,
            data={"filename": filename, "length": len(image_bytes)},
            timeout=15
        )
        d1 = r1.json()
        print(f"  getUploadURL response: {d1}")
        if not d1.get("ok"):
            print(f"  getUploadURL error: {d1.get('error')} — check files:write scope on Slack app")
            return False

        # Step 2 — upload bytes
        r2 = requests.post(d1["upload_url"], data=image_bytes, timeout=30)
        print(f"  Upload PUT status: {r2.status_code}")
        if r2.status_code not in (200, 201):
            print(f"  Upload PUT failed: {r2.status_code} — {r2.text[:200]}")
            return False

        # Step 3 — complete and share to channel
        r3 = requests.post(
            "https://slack.com/api/files.completeUploadExternal",
            headers={**headers, "Content-Type": "application/json"},
            json={"files": [{"id": d1["file_id"]}], "channel_id": channel},
            timeout=15
        )
        d3 = r3.json()
        print(f"  completeUpload response: {d3}")
        if not d3.get("ok"):
            print(f"  completeUpload error: {d3.get('error')}")
            return False

        print("  ✅ Fleet header image uploaded to Slack")
        return True
    except Exception as e:
        print(f"  Image upload error: {e}")
        return False


def build_message(counts: dict, available_vehicles: list, bookings_data: dict):
    now      = now_dubai()
    date_str = now.strftime("%d %b %Y")
    today    = now.strftime("%Y-%m-%d")

    str_c    = counts.get("shortTermRental", 0)
    lease_c  = counts.get("lease", 0)
    ltr_c    = counts.get("longTermRental", 0)
    svc_c    = counts.get("service", 0)
    avail_c  = len(available_vehicles)
    on_rent  = TOTAL_FLEET - avail_c

    next_booking = bookings_data.get("next_booking", {})
    to_deliver   = bookings_data.get("to_deliver", [])
    to_return    = bookings_data.get("to_return", [])

    blocks = []

    # ── HEADER ────────────────────────────────────────────
    blocks.append({"type": "header",
        "text": {"type": "plain_text",
            "text": f"📋 MKV Fleet Availability — {date_str}", "emoji": True}})

    # ── FLEET STATUS fallback text (image posted separately above) ────────────
    blocks.append({"type": "section", "fields": [
        {"type": "mrkdwn", "text": f"*Total Fleet*\n{TOTAL_FLEET}"},
        {"type": "mrkdwn", "text": f"*Available*\n{avail_c}"},
    ]})
    blocks.append({"type": "section", "fields": [
        {"type": "mrkdwn", "text": f"*Short-term (STR)*\n{str_c}"},
        {"type": "mrkdwn", "text": f"*Lease*\n{lease_c}"},
    ]})
    blocks.append({"type": "section", "fields": [
        {"type": "mrkdwn", "text": f"*Long-term*\n{ltr_c}"},
        {"type": "mrkdwn", "text": f"*Service*\n{svc_c}"},
    ]})

    blocks.append({"type": "divider"})

    # ── AVAILABLE CARS ────────────────────────────────────
    if available_vehicles:
        avail_lines = []
        for v in available_vehicles:
            pk = plate_key(v["plate"])
            nb = next_booking.get(pk)
            if nb:
                try:
                    nb_fmt = datetime.strptime(nb, "%Y-%m-%d").strftime("%d %b %Y")
                    next_str = f"Next: {nb_fmt}"
                except:
                    next_str = f"Next: {nb}"
            else:
                next_str = "No upcoming booking"
            avail_lines.append(
                f"*{v['name']}*  `{v['plate']}`  ·  _{next_str}_"
            )

        # Split into chunks (Slack section limit ~3000 chars)
        chunk, chunks = "", []
        for line in avail_lines:
            candidate = chunk + line + "\n"
            if len(candidate) > 2800:
                chunks.append(chunk.rstrip())
                chunk = line + "\n"
            else:
                chunk = candidate
        if chunk.strip():
            chunks.append(chunk.rstrip())

        for i, chunk in enumerate(chunks):
            header_txt = f"✅ *AVAILABLE CARS ({avail_c})*\n" if i == 0 else ""
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                "text": header_txt + chunk}})
    else:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": "✅ *AVAILABLE CARS*\nNo vehicles available today."}})

    blocks.append({"type": "divider"})

    # ── TO BE DELIVERED TODAY ─────────────────────────────
    if to_deliver:
        lines = "\n".join(
            f"• *{e['vehicle']}*  `{e['plate']}`  ·  {e['customer']}  ·  {e['time']}"
            for e in to_deliver
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"🚗 *TO BE DELIVERED TODAY ({len(to_deliver)})*\n{lines}"}})
    else:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": "🚗 *TO BE DELIVERED TODAY*\nNone scheduled."}})

    blocks.append({"type": "divider"})

    # ── TO BE RETURNED TODAY ──────────────────────────────
    if to_return:
        lines = "\n".join(
            f"• *{e['vehicle']}*  `{e['plate']}`  ·  {e['customer']}  ·  Due {e['time']}"
            for e in to_return
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"🔑 *TO BE RETURNED TODAY ({len(to_return)})*\n{lines}"}})
    else:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": "🔑 *TO BE RETURNED TODAY*\nNone due today."}})

    # ── FOOTER ────────────────────────────────────────────
    blocks.append({"type": "divider"})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": f"MKV Fleet Availability · {date_str} · Auto-posted 10:00 AM Dubai time · Available sorted by next booking date"}]})

    return {"blocks": blocks, "text": f"MKV Fleet Availability — {date_str}"}

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

    print("\n[2] Checking fleet mismatch ...")
    mismatch = check_fleet_mismatch(counts)
    if mismatch:
        post_mismatch_alert(mismatch)

    print("\n[3] Fetching available vehicles ...")
    available = fetch_available_vehicles()

    print("\n[4] Fetching bookings data ...")
    bookings_data = fetch_bookings_data()

    print("\n[5] Generating fleet header image ...")
    date_str  = now_dubai().strftime("%d %b %Y")
    str_c     = counts.get("shortTermRental", 0)
    lease_c   = counts.get("lease", 0)
    ltr_c     = counts.get("longTermRental", 0)
    avail_c   = len(available)
    img_bytes = generate_fleet_image(TOTAL_FLEET, lease_c, ltr_c, str_c, avail_c, date_str)
    if img_bytes:
        upload_image_to_slack(img_bytes, f"fleet_{date_str.replace(' ','_')}.png", SLACK_CHANNEL)

    print("\n[6] Building Slack message ...")
    msg = build_message(counts, available, bookings_data)

    print("\n[7] Posting to Slack ...")
    post_slack(msg)
