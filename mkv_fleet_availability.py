"""
MKV Luxury — Fleet Availability
Counts from MASTER_FLEET + bookings API. No assignments API.
"""
import os, json, re, requests, io
from datetime import datetime, timezone, timedelta

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
SLACK_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
APPIC_KEY     = os.environ.get("APPIC_KEY", "")
DUBAI_TZ      = timezone(timedelta(hours=4))

CHANNEL_FLEET = "C0ABW8AGMRU"   # #team-mkv-car-availability (live)
CHANNEL_TEST  = "C0B0TGBDCDU"   # #mkvtest

TEST_MODE     = False
SLACK_CHANNEL = CHANNEL_TEST if TEST_MODE else CHANNEL_FLEET

APPIC_BOOKINGS_URL = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-bookings.php"
SKIP_STATUSES      = {"cancelled", "canceled", "voided", "void", "deleted", "closed"}

DEBUG_MODE = False  # set True to dump API fields to logs, then back to False

# ─────────────────────────────────────────────
#  GOOGLE SHEET — Fleet Master
#  Columns: Plate | Vehicle Name | Category
#  Category values: STR, LEASE, LTR, NRV
# ─────────────────────────────────────────────
SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1GfzIz2ASBurkPW63WwscnGUsTAnVGswdX46mf5aP1cI"
    "/export?format=csv&gid=571607065"
)

def fetch_master_fleet() -> dict:
    """
    Fetch fleet master from Google Sheet.
    Returns dict: plate_key → (vehicle_name, category, status)
    Columns: A=Plate, B=Vehicle Name, C=Category, D=Status
    """
    try:
        r = requests.get(SHEET_CSV_URL, timeout=15)
        r.raise_for_status()
        lines  = r.text.strip().splitlines()
        fleet  = {}
        for i, line in enumerate(lines[1:], 2):
            parts = line.split(",")
            if len(parts) < 3:
                continue
            raw_plate = parts[0].strip().strip('"')
            name      = parts[1].strip().strip('"')
            category  = parts[2].strip().strip('"').upper()
            status    = parts[3].strip().strip('"').upper() if len(parts) > 3 else ""
            if not raw_plate or not category:
                continue
            if category not in ("STR", "LEASE", "LTR", "NRV"):
                continue
            pk = plate_key(raw_plate)
            fleet[pk] = (name, category, status)
        print(f"  Fleet master loaded: {len(fleet)} vehicles from Google Sheet")
        return fleet
    except Exception as ex:
        print(f"  Google Sheet error: {ex} — fleet master empty")
        return {}



# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def now_dubai():
    return datetime.now(DUBAI_TZ)

def plate_key(plate: str) -> str:
    return re.sub(r"\s+", "", str(plate).upper())

def match_plate(appic_pk: str, master_plates: set) -> str:
    """
    Match Appic plate key to master fleet plate key.
    Appic often strips letter prefix (e.g. '24545' instead of 'U24545').
    First tries exact match, then numeric suffix match.
    """
    if appic_pk in master_plates:
        return appic_pk
    # Try matching on numeric suffix only
    appic_nums = re.sub(r"[^0-9]", "", appic_pk)
    if appic_nums:
        for mp in master_plates:
            if re.sub(r"[^0-9]", "", mp) == appic_nums:
                return mp
    return ""

# ─────────────────────────────────────────────
#  DEBUG DUMP
# ─────────────────────────────────────────────
def debug_dump():
    today    = now_dubai().strftime("%Y-%m-%d")
    lookback = (now_dubai() - timedelta(days=1)).strftime("%Y-%m-%d")
    print("\n" + "=" * 55)
    print("  DEBUG MODE — API FIELD DUMP")
    print("=" * 55)
    r = requests.post(APPIC_BOOKINGS_URL, data={
        "key": APPIC_KEY, "startDate": lookback, "endDate": today
    }, timeout=20)
    bookings = r.json().get("bookings", [])
    active   = [b for b in bookings if (b.get("status") or "").lower() not in SKIP_STATUSES]
    deliver  = [b for b in active if b.get("startDate") == today]
    returns  = [b for b in active if b.get("endDate") == today]
    print(f"  Bookings in window: {len(bookings)}")
    print(f"  Active: {len(active)}  Deliveries: {len(deliver)}  Returns: {len(returns)}")
    if bookings:
        print("  First booking fields:")
        for k, v in bookings[0].items():
            print(f"    {k:<30}: {v}")
    print("=" * 55)

# ─────────────────────────────────────────────
#  FETCH FLEET DATA
# ─────────────────────────────────────────────
def fetch_fleet_data() -> dict:
    today     = now_dubai().strftime("%Y-%m-%d")
    lookback  = (now_dubai() - timedelta(days=400)).strftime("%Y-%m-%d")
    lookahead = (now_dubai() + timedelta(days=400)).strftime("%Y-%m-%d")

    # ── Load fleet master from Google Sheet ───────────────
    master_fleet = fetch_master_fleet()
    if not master_fleet:
        print("  ERROR: Fleet master empty — aborting")
        raise SystemExit(1)

    total_fleet  = len(master_fleet)
    str_plates   = {k for k, v in master_fleet.items() if v[1] == "STR"}
    lease_plates = {k for k, v in master_fleet.items() if v[1] == "LEASE"}
    ltr_plates   = {k for k, v in master_fleet.items() if v[1] == "LTR"}
    nrv_plates   = {k for k, v in master_fleet.items() if v[1] == "NRV"}

    # Vehicles unavailable from sheet status
    unavailable  = {k for k, v in master_fleet.items()
                    if v[2] in ("SERVICE/GARAGE", "GARAGE", "SERVICE",
                                "WORKSHOP", "MAINTENANCE")}

    garage_service_count = len({k for k, v in master_fleet.items()
                                 if v[2] in ("SERVICE/GARAGE", "GARAGE", "SERVICE",
                                             "WORKSHOP", "MAINTENANCE")
                                 and k in str_plates})

    try:
        r = requests.post(APPIC_BOOKINGS_URL, data={
            "key": APPIC_KEY, "startDate": lookback, "endDate": lookahead
        }, timeout=20)
        bookings = r.json().get("bookings", [])
        print(f"  Bookings returned: {len(bookings)}")
    except Exception as ex:
        print(f"  Bookings API error: {ex}")
        bookings = []

    rented_str   = set()
    rented_lease = set()
    rented_ltr   = set()
    to_deliver   = []
    to_return    = []
    next_booking = {}
    deliver_plates = set()   # plates being delivered today — exclude from available

    for b in bookings:
        if (b.get("status") or "").lower().strip() in SKIP_STATUSES:
            continue

        raw   = str(b.get("vehiclePlate") or "").strip()
        pk    = plate_key(raw)
        start = (b.get("startDate") or "").strip()
        end   = (b.get("endDate")   or "").strip()
        cust  = (b.get("customerName") or "N/A").strip().title()
        veh   = (b.get("vehicleName")  or "N/A").strip().title()
        st    = (b.get("startTime") or "")[:5]
        et    = (b.get("endTime")   or "")[:5]

        # Active today = started strictly before today OR started today but already delivered
        # Use start < today for rented count to avoid counting future-today deliveries
        is_active = start < today <= end or (start == today and end >= today)

        if is_active and start <= today:
            all_plates = str_plates | lease_plates | ltr_plates | nrv_plates
            matched_pk = match_plate(pk, all_plates)

            if matched_pk and start < today:  # only count as rented if started before today
                if   matched_pk in str_plates:   rented_str.add(matched_pk)
                elif matched_pk in lease_plates: rented_lease.add(matched_pk)
                elif matched_pk in ltr_plates:   rented_ltr.add(matched_pk)

        # Next booking per plate
        all_plates = str_plates | lease_plates | ltr_plates | nrv_plates
        matched_pk = match_plate(pk, all_plates)
        if start > today and matched_pk:
            if matched_pk not in next_booking or start < next_booking[matched_pk]:
                next_booking[matched_pk] = start

    # Today deliveries
    deliver_plates = set()
    if start == today:
        matched_pk2 = match_plate(pk, str_plates | lease_plates | ltr_plates | nrv_plates)
        if matched_pk2:
            deliver_plates.add(matched_pk2)
        to_deliver.append({"vehicle": veh, "plate": raw, "customer": cust, "time": st})

        # Today returns
        if end == today and start < today:
            to_return.append({"vehicle": veh, "plate": raw, "customer": cust, "time": et})

    # Service/Garage takes priority — remove from all rented counts
    rented_str   = rented_str   - unavailable
    rented_lease = rented_lease - unavailable
    rented_ltr   = rented_ltr   - unavailable

    available = []
    for pk in str_plates - rented_str - unavailable - deliver_plates:
        name = master_fleet[pk][0]
        available.append({"name": name, "plate": pk, "next": next_booking.get(pk)})
    available.sort(key=lambda v: v["next"] or "9999-99-99")

    # Service/Garage count — across all categories
    svc_garage_plates = {k for k, v in master_fleet.items()
                         if v[2] in ("SERVICE/GARAGE", "GARAGE", "SERVICE",
                                     "WORKSHOP", "MAINTENANCE")}

    counts = {
        "total":           total_fleet,
        "str_total":       len(str_plates),
        "rented_str":      len(rented_str),
        "svc_garage":      len(svc_garage_plates),
        "lease_total":     len(lease_plates),
        "rented_lease":    len(rented_lease),
        "ltr_total":       len(ltr_plates),
        "rented_ltr":      len(rented_ltr),
        "nrv":             len(nrv_plates),
        "available":       len(available),
    }

    print(f"  STR {counts['rented_str']}/{counts['str_total']}  "
          f"Lease {counts['rented_lease']}/{counts['lease_total']}  "
          f"LTR {counts['rented_ltr']}/{counts['ltr_total']}  "
          f"NRV {counts['nrv']}  Available {counts['available']}")
    print(f"  Deliver today: {len(to_deliver)}  Return today: {len(to_return)}")

    return {
        "counts":     counts,
        "available":  available,
        "to_deliver": to_deliver,
        "to_return":  to_return,
    }

# ─────────────────────────────────────────────
#  GENERATE IMAGE
# ─────────────────────────────────────────────
def generate_fleet_image(total, lease_c, ltr_c, str_c, avail_c, date_str):
    try:
        from PIL import Image, ImageDraw, ImageFont

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
            ("Rented STR",       str_c,   "str"),
            ("Available",        avail_c, "available"),
        ]

        img  = Image.new("RGB", (W, H), DARK_BG)
        draw = ImageDraw.Draw(img)

        font_paths      = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                           "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"]
        font_bold_paths = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                           "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]

        def load_font(paths, size):
            for p in paths:
                try: return ImageFont.truetype(p, size)
                except: pass
            return ImageFont.load_default()

        font_label = load_font(font_paths, 14)
        font_value = load_font(font_bold_paths, 36)
        font_title = load_font(font_bold_paths, 15)

        draw.text((20, 14), f"MKV Fleet Status  —  {date_str}", font=font_title, fill=WHITE)

        box_w, gap = 160, 12
        start_x    = (W - (5 * box_w + 4 * gap)) // 2
        box_y, box_h = 48, 92

        for i, (label, value, key) in enumerate(metrics):
            x = start_x + i * (box_w + gap)
            draw.rounded_rectangle([x, box_y, x+box_w, box_y+box_h],
                                   radius=10, fill=BOX_BG, outline=BOX_BORDER, width=1)
            lb = draw.textlength(label, font=font_label)
            draw.text((x + (box_w - lb)//2, box_y + 10), label, font=font_label, fill=GRAY)
            vb = draw.textlength(str(value), font=font_value)
            draw.text((x + (box_w - vb)//2, box_y + 34), str(value), font=font_value, fill=COLORS[key])

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        print("  Fleet image generated")
        return buf.getvalue()
    except Exception as e:
        print(f"  Image generation failed: {e}")
        return None

# ─────────────────────────────────────────────
#  UPLOAD IMAGE
# ─────────────────────────────────────────────
def upload_image_to_slack(image_bytes, filename, channel):
    try:
        headers = {"Authorization": f"Bearer {SLACK_TOKEN}"}
        r1 = requests.post("https://slack.com/api/files.getUploadURLExternal",
            headers=headers,
            data={"filename": filename, "length": len(image_bytes)}, timeout=15)
        d1 = r1.json()
        if not d1.get("ok"):
            print(f"  getUploadURL error: {d1.get('error')}")
            return False
        r2 = requests.post(d1["upload_url"], data=image_bytes, timeout=30)
        if r2.status_code not in (200, 201):
            print(f"  Upload failed: {r2.status_code}")
            return False
        r3 = requests.post("https://slack.com/api/files.completeUploadExternal",
            headers={**headers, "Content-Type": "application/json"},
            json={"files": [{"id": d1["file_id"], "title": filename}],
                  "channel_id": channel}, timeout=15)
        d3 = r3.json()
        if not d3.get("ok"):
            print(f"  completeUpload error: {d3.get('error')}")
            return False
        print("  Image uploaded to Slack")
        return True
    except Exception as e:
        print(f"  Image upload error: {e}")
        return False

# ─────────────────────────────────────────────
#  BUILD MESSAGE
# ─────────────────────────────────────────────
def build_message(fleet_data: dict) -> dict:
    now      = now_dubai()
    date_str = now.strftime("%d %b %Y").upper()
    day_str  = now.strftime("%A").upper()

    counts       = fleet_data["counts"]
    available    = fleet_data["available"]
    to_deliver   = fleet_data["to_deliver"]
    to_return    = fleet_data["to_return"]

    total        = counts["total"]
    str_total    = counts["str_total"]
    rented_str   = counts["rented_str"]
    svc_garage   = counts["svc_garage"]
    lease_total  = counts["lease_total"]
    rented_lease = counts["rented_lease"]
    ltr_total    = counts["ltr_total"]
    rented_ltr   = counts["rented_ltr"]
    nrv          = counts["nrv"]
    avail_c      = counts["available"]

    # Split available into booked vs idle
    booked = [v for v in available if v.get("next")]
    idle   = [v for v in available if not v.get("next")]

    blocks = []

    # ── HEADER ─────────────────────────────────────────────
    blocks.append({"type": "header",
        "text": {"type": "plain_text",
            "text": f"🚗 MKV FLEET STATUS — {date_str} {day_str}", "emoji": True}})

    # ── FLEET COUNTS ───────────────────────────────────────
    blocks.append({"type": "section", "fields": [
        {"type": "mrkdwn", "text": f"*Total Fleet*\n{total}"},
        {"type": "mrkdwn", "text": f"*Available STR*\n{avail_c}"},
        {"type": "mrkdwn", "text": f"*Service / Garage*\n{svc_garage}"},
    ]})
    blocks.append({"type": "section", "fields": [
        {"type": "mrkdwn", "text": f"*Rented STR*\n{rented_str} / {str_total}"},
        {"type": "mrkdwn", "text": f"*Lease*\n{rented_lease} / {lease_total}"},
        {"type": "mrkdwn", "text": f"*LTR*\n{rented_ltr} / {ltr_total}"},
        {"type": "mrkdwn", "text": f"*NRV*\n{nrv}"},
    ]})

    blocks.append({"type": "divider"})

    # ── AVAILABLE STR ──────────────────────────────────────
    blocks.append({"type": "section", "text": {"type": "mrkdwn",
        "text": f"*✅ AVAILABLE STR ({avail_c})*"}})

    # With upcoming booking
    if booked:
        booked_lines = "*📅 With upcoming booking:*\n"
        for v in booked:
            try:
                nb = datetime.strptime(v["next"], "%Y-%m-%d").strftime("%d %b")
            except:
                nb = v["next"]
            booked_lines += f"• *{v['name']}*  `{v['plate']}`  →  {nb}\n"
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": booked_lines.rstrip()}})

    # No upcoming booking — split into chunks if needed
    if idle:
        idle_header = f"*💤 No upcoming booking ({len(idle)}):*\n"
        idle_lines  = ""
        chunks      = []
        for v in idle:
            idle_lines += f"• *{v['name']}*  `{v['plate']}`\n"
        # Split if too long
        full = idle_header + idle_lines.rstrip()
        if len(full) <= 2800:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": full}})
        else:
            # First chunk with header
            chunk, first = "", True
            for line in idle_lines.splitlines():
                candidate = chunk + line + "\n"
                if len(candidate) > 2700:
                    hdr = idle_header if first else ""
                    blocks.append({"type": "section", "text": {"type": "mrkdwn",
                        "text": hdr + chunk.rstrip()}})
                    chunk = line + "\n"
                    first = False
                else:
                    chunk = candidate
            if chunk.strip():
                hdr = idle_header if first else ""
                blocks.append({"type": "section", "text": {"type": "mrkdwn",
                    "text": hdr + chunk.rstrip()}})

    blocks.append({"type": "divider"})

    # ── DELIVERY TODAY ─────────────────────────────────────
    if to_deliver:
        lines = "\n".join(
            f"• *{e['vehicle']}*  `{e['plate']}`  ·  {e['customer']}  ·  {e['time']}"
            for e in sorted(to_deliver, key=lambda x: x["time"]))
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"*🚗 DELIVERY TODAY ({len(to_deliver)})*\n{lines}"}})
    else:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": "*🚗 DELIVERY TODAY*\nNone scheduled."}})

    blocks.append({"type": "divider"})

    # ── RETURN TODAY ───────────────────────────────────────
    if to_return:
        lines = "\n".join(
            f"• *{e['vehicle']}*  `{e['plate']}`  ·  {e['customer']}  ·  Due {e['time']}"
            for e in sorted(to_return, key=lambda x: x["time"]))
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"*🔑 RETURN TODAY ({len(to_return)})*\n{lines}"}})
    else:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": "*🔑 RETURN TODAY*\nNone due today."}})

    blocks.append({"type": "divider"})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": f"MKV Car Rental  ·  Auto-posted 10AM Dubai time  ·  {date_str}"}]})

    return {"blocks": blocks, "text": f"MKV Fleet Status — {date_str} {day_str}"}

# ─────────────────────────────────────────────
#  POST TO SLACK
# ─────────────────────────────────────────────
def post_slack(message):
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"},
        data=json.dumps({"channel": SLACK_CHANNEL, "username": "MKV Fleet Status",
                         "icon_emoji": ":car:", "unfurl_links": False,
                         "unfurl_media": False, **message}),
        timeout=15)
    res = r.json()
    if not res.get("ok"):
        print(f"  Slack error: {res.get('error')}")
        raise SystemExit(1)
    print("  Posted to Slack")

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("  MKV FLEET AVAILABILITY")
    print(f"  {now_dubai().strftime('%d %b %Y | %I:%M %p Dubai Time')}")
    print("=" * 55)

    if DEBUG_MODE:
        debug_dump()
        raise SystemExit(0)

    print("\n[1] Fetching fleet data ...")
    fleet_data = fetch_fleet_data()
    counts     = fleet_data["counts"]

    print("\n[2] Generating header image ...")
    date_str  = now_dubai().strftime("%d %b %Y")
    img_bytes = generate_fleet_image(
        counts["total"], counts["rented_lease"],
        counts["rented_ltr"], counts["rented_str"],
        counts["available"], date_str)
    if img_bytes:
        upload_image_to_slack(img_bytes,
            f"fleet_{date_str.replace(' ','_')}.png", SLACK_CHANNEL)

    print("\n[3] Building message ...")
    msg = build_message(fleet_data)

    print("\n[4] Posting to Slack ...")
    post_slack(msg)

    print("=" * 55)
    print("  Done")
    print("=" * 55)
