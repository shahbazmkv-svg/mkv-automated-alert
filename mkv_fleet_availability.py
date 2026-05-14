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
SLACK_CHANNEL = "C0ABPC606F7"   # #mkv-fleet (live) — update if needed
DUBAI_TZ      = timezone(timedelta(hours=4))

APPIC_BOOKINGS_URL = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-bookings.php"
SKIP_STATUSES      = {"cancelled", "canceled", "voided", "void", "deleted", "closed"}

DEBUG_MODE = False  # set True to dump API fields to logs, then back to False

# ─────────────────────────────────────────────
#  MASTER FLEET — 62 vehicles
#  STR=Short Term Rental, LEASE=Lease, LTR=Long Term Rental, NRV=Not Road Worthy
# ─────────────────────────────────────────────
MASTER_FLEET = {
    # STR — 36 vehicles
    "I47203":  ("MORGAN SUPERSPORT", "STR"),
    "X55789":  ("MERCEDES S500", "STR"),
    "L94545":  ("RANGE ROVER SPORT GRAY", "STR"),
    "T55789":  ("RANGE ROVER SPORT BLACK", "STR"),
    "J77540":  ("RANGE ROVER SVR BLACK", "STR"),
    "AA68620": ("RANGE ROVER VELAR", "STR"),
    "CC83762": ("LAND ROVER DEFENDER V8", "STR"),
    "AA78043": ("LAND ROVER DEFENDER 130 V6", "STR"),
    "AA77491": ("FORD MUSTANG CONVERTIBLE RED", "STR"),
    "AA77490": ("FORD MUSTANG COUPE WHITE", "STR"),
    "B15789":  ("FORD MUSTANG BLACK/YELLOW", "STR"),
    "Y72712":  ("CHEVROLET CORVETTE", "STR"),
    "E23652":  ("LOTUS EMIRA", "STR"),
    "AA78051": ("BMW 735I", "STR"),
    "K70691":  ("BMW 520I", "STR"),
    "D70688":  ("BMW 420I", "STR"),
    "K19443":  ("MERCEDES GLB 250", "STR"),
    "X44789":  ("CADILLAC ESCALADE", "STR"),
    "AA78042": ("CHEVROLET TAHOE", "STR"),
    "W46015":  ("AUDI RS Q3", "STR"),
    "Z89438":  ("AUDI A6", "STR"),
    "Z92156":  ("AUDI A6", "STR"),
    "Z90158":  ("AUDI A3", "STR"),
    "X33789":  ("BENTLEY BENTAYGA MANSORY", "STR"),
    "Y97019":  ("FERRARI PUROSANGUE", "STR"),
    "U24545":  ("MERCEDES G63 BLACK 2025", "STR"),
    "O66789":  ("MERCEDES G63 BRABUS", "STR"),
    "Y97018":  ("KIA CERATO", "STR"),
    "BB60137": ("MERCEDES G63 2026 RETRO", "STR"),
    "O94545":  ("LAMBORGHINI URUS YELLOW", "STR"),
    "CC69367": ("GMC YUKON", "STR"),
    "J47041":  ("MCLAREN ARTURA", "STR"),
    "EE42165": ("PORSCHE 911", "STR"),
    "T64545":  ("PORSCHE GT4 RS", "STR"),
    "H75037":  ("RANGE ROVER SVR GRAY/BLUE", "STR"),
    "W97521":  ("LAMBORGHINI HURACAN EVO SPYDER", "STR"),
    # LEASE — 15 vehicles
    "U74545":  ("FERRARI 296 GTB", "LEASE"),
    "S66789":  ("MERCEDES G63 WHITE 2025", "LEASE"),
    "T3660":   ("MERCEDES G63 BLUE", "LEASE"),
    "AA78067": ("MERCEDES C200", "LEASE"),
    "CC94084": ("RANGE ROVER SPORT WHITE", "LEASE"),
    "C69703":  ("NISSAN PATROL WHITE", "LEASE"),
    "X33567":  ("FORD BRONCO", "LEASE"),
    "N27852":  ("AUDI Q3", "LEASE"),
    "Z90154":  ("AUDI A3", "LEASE"),
    "F98103":  ("KIA SPORTAGE WHITE", "LEASE"),
    "F98438":  ("KIA SORENTO", "LEASE"),
    "W81946":  ("JETOUR T2 BLUE", "LEASE"),
    "D68539":  ("DONGFENG FORTHING S7", "LEASE"),
    "BB53403": ("GAC M8 2026", "LEASE"),
    "T78242":  ("JETOUR T2 BROWN", "LEASE"),
    # LTR — 6 vehicles
    "V1243":   ("ROLLS ROYCE GRAY", "LTR"),
    "S39810":  ("NISSAN PATROL", "LTR"),
    "H23155":  ("NISSAN PATROL", "LTR"),
    "F83209":  ("RANGE ROVER SPORT BLACK", "LTR"),
    "F97580":  ("CADILLAC ESCALADE SPORT", "LTR"),
    "K19503":  ("MERCEDES GLB 250", "LTR"),
    # NRV — 5 vehicles
    "Y97020":  ("KIA K5", "NRV"),
    "R26603":  ("SUZUKI SWIFT", "NRV"),
    "P38848":  ("MERCEDES G63 BLACK 2024", "NRV"),
    "Z66246":  ("GMC YUKON", "NRV"),
    "H31727":  ("TOYOTA LAND CRUISER", "NRV"),
}

TOTAL_FLEET  = len(MASTER_FLEET)
STR_PLATES   = {k for k, v in MASTER_FLEET.items() if v[1] == "STR"}
LEASE_PLATES = {k for k, v in MASTER_FLEET.items() if v[1] == "LEASE"}
LTR_PLATES   = {k for k, v in MASTER_FLEET.items() if v[1] == "LTR"}
NRV_PLATES   = {k for k, v in MASTER_FLEET.items() if v[1] == "NRV"}

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def now_dubai():
    return datetime.now(DUBAI_TZ)

def plate_key(plate: str) -> str:
    return re.sub(r"\s+", "", str(plate).upper())

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

        # Active today
        if start <= today <= end:
            if   pk in STR_PLATES:   rented_str.add(pk)
            elif pk in LEASE_PLATES: rented_lease.add(pk)
            elif pk in LTR_PLATES:   rented_ltr.add(pk)

        # Next booking per plate
        if start > today:
            if pk not in next_booking or start < next_booking[pk]:
                next_booking[pk] = start

        # Today deliveries
        if start == today:
            to_deliver.append({"vehicle": veh, "plate": raw, "customer": cust, "time": st})

        # Today returns
        if end == today and start < today:
            to_return.append({"vehicle": veh, "plate": raw, "customer": cust, "time": et})

    # Available = STR plates not rented
    available = []
    for pk in STR_PLATES - rented_str:
        name, _ = MASTER_FLEET[pk]
        available.append({"name": name, "plate": pk, "next": next_booking.get(pk)})
    available.sort(key=lambda v: v["next"] or "9999-99-99")

    counts = {
        "total":        TOTAL_FLEET,
        "str_total":    len(STR_PLATES),
        "rented_str":   len(rented_str),
        "lease_total":  len(LEASE_PLATES),
        "rented_lease": len(rented_lease),
        "ltr_total":    len(LTR_PLATES),
        "rented_ltr":   len(rented_ltr),
        "nrv":          len(NRV_PLATES),
        "available":    len(available),
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
    date_str     = now_dubai().strftime("%d %b %Y")
    counts       = fleet_data["counts"]
    available    = fleet_data["available"]
    to_deliver   = fleet_data["to_deliver"]
    to_return    = fleet_data["to_return"]

    total        = counts["total"]
    str_total    = counts["str_total"]
    rented_str   = counts["rented_str"]
    lease_total  = counts["lease_total"]
    rented_lease = counts["rented_lease"]
    ltr_total    = counts["ltr_total"]
    rented_ltr   = counts["rented_ltr"]
    nrv          = counts["nrv"]
    avail_c      = counts["available"]

    blocks = []

    blocks.append({"type": "header",
        "text": {"type": "plain_text", "text": f"MKV Fleet Status — {date_str}", "emoji": True}})

    blocks.append({"type": "section", "fields": [
        {"type": "mrkdwn", "text": f"*Total Fleet*\n{total}"},
        {"type": "mrkdwn", "text": f"*Available STR*\n{avail_c}"},
    ]})
    blocks.append({"type": "section", "fields": [
        {"type": "mrkdwn", "text": f"*Rented STR*\n{rented_str} / {str_total}"},
        {"type": "mrkdwn", "text": f"*Lease*\n{rented_lease} / {lease_total}"},
    ]})
    blocks.append({"type": "section", "fields": [
        {"type": "mrkdwn", "text": f"*LTR*\n{rented_ltr} / {ltr_total}"},
        {"type": "mrkdwn", "text": f"*NRV*\n{nrv}"},
    ]})

    blocks.append({"type": "divider"})

    if available:
        lines, chunk, chunks = [], "", []
        for v in available:
            nb = v.get("next")
            try: nb_str = f"Next: {datetime.strptime(nb, '%Y-%m-%d').strftime('%d %b')}" if nb else "No upcoming booking"
            except: nb_str = f"Next: {nb}"
            lines.append(f"*{v['name']}*  `{v['plate']}`  ·  _{nb_str}_")

        for line in lines:
            cand = chunk + line + "\n"
            if len(cand) > 2800:
                chunks.append(chunk.rstrip())
                chunk = line + "\n"
            else:
                chunk = cand
        if chunk.strip(): chunks.append(chunk.rstrip())

        for i, ch in enumerate(chunks):
            hdr = f"*AVAILABLE STR ({avail_c})*\n" if i == 0 else ""
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": hdr + ch}})
    else:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": "*AVAILABLE STR*\nNo vehicles available."}})

    blocks.append({"type": "divider"})

    if to_deliver:
        txt = "\n".join(
            f"• *{e['vehicle']}*  `{e['plate']}`  ·  {e['customer']}  ·  {e['time']}"
            for e in sorted(to_deliver, key=lambda x: x["time"]))
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"*DELIVERY TODAY ({len(to_deliver)})*\n{txt}"}})
    else:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": "*DELIVERY TODAY*\nNone scheduled."}})

    blocks.append({"type": "divider"})

    if to_return:
        txt = "\n".join(
            f"• *{e['vehicle']}*  `{e['plate']}`  ·  {e['customer']}  ·  Due {e['time']}"
            for e in sorted(to_return, key=lambda x: x["time"]))
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"*RETURN TODAY ({len(to_return)})*\n{txt}"}})
    else:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": "*RETURN TODAY*\nNone due today."}})

    blocks.append({"type": "divider"})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": f"MKV Fleet · {date_str} · Auto-posted 10AM Dubai time"}]})

    return {"blocks": blocks, "text": f"MKV Fleet Status — {date_str}"}

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
