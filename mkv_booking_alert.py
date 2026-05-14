import requests
import json
import os
from datetime import datetime, timezone, timedelta

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
APPIC_KEY          = os.environ["APPIC_KEY"]
SLACK_BOT_TOKEN    = os.environ["SLACK_BOT_TOKEN"]

CHANNEL_BOOKINGS   = "C0ABPC606F7"   # #mkv-bookings (live)
CHANNEL_DELIVERY   = "C0ACB9C8J01"   # #mkv-schedule-for-delivery (live)
CHANNEL_TEST       = "C0B0TGBDCDU"   # #mkvtest

TEST_MODE          = False
TARGET_CHANNEL     = CHANNEL_TEST if TEST_MODE else CHANNEL_BOOKINGS
TARGET_DELIVERY    = CHANNEL_TEST if TEST_MODE else CHANNEL_DELIVERY

APPIC_BOOKINGS_URL = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-bookings.php"
STORE_FILE         = "booking_thread_store.json"
DUBAI_TZ           = timezone(timedelta(hours=4))
SLACK_HEADERS      = {
    "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    "Content-Type": "application/json"
}

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def dubai_now():
    return datetime.now(DUBAI_TZ)

def fmt_date(d):
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%d %b %Y")
    except:
        return d or "N/A"

def booking_key(b):
    cid = (b.get("contractID") or "").strip()
    if cid:
        return cid
    plate = (b.get("vehiclePlate") or "").strip()
    sdate = (b.get("startDate")    or "").strip()
    stime = (b.get("startTime")    or "").strip()
    return f"{plate}|{sdate}|{stime}"

def load_store():
    if os.path.exists(STORE_FILE):
        try:
            with open(STORE_FILE) as f:
                return json.load(f)
        except:
            pass
    return {"bookings": {}}

def save_store(store):
    with open(STORE_FILE, "w") as f:
        json.dump(store, f, indent=2)

# ─────────────────────────────────────────────
#  SLACK API
# ─────────────────────────────────────────────

def post_message(channel, blocks, text, thread_ts=None):
    payload = {"channel": channel, "text": text, "blocks": blocks}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        r = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers=SLACK_HEADERS,
            json=payload,
            timeout=10
        )
        data = r.json()
        if data.get("ok"):
            return data.get("ts")
        else:
            print(f"  Slack post error: {data.get('error')}")
            return None
    except Exception as e:
        print(f"  Slack request failed: {e}")
        return None

def delete_message(channel, ts):
    """Delete a single bot-posted message by channel + ts."""
    try:
        r = requests.post(
            "https://slack.com/api/chat.delete",
            headers=SLACK_HEADERS,
            json={"channel": channel, "ts": ts},
            timeout=10
        )
        data = r.json()
        if data.get("ok"):
            print(f"  Deleted message ts={ts}")
            return True
        else:
            print(f"  Delete error ts={ts}: {data.get('error')}")
            return False
    except Exception as e:
        print(f"  Delete request failed: {e}")
        return False

def delete_booking_thread(channel, stored):
    """
    Delete all bot-posted messages for a booking.
    Replies are deleted first, parent card last.

    Store keys used:
      thread_ts          — parent booking card
      delivery_ts        — delivery checklist reply
      pickup_ts          — pickup checklist reply
      closed_ts          — contract closed reply
      extension_ts_list  — list of extension note ts values
    """
    deleted = []

    # Delete replies first (newest to oldest)
    for ts_key in ["closed_ts", "pickup_ts", "delivery_ts"]:
        ts = stored.get(ts_key)
        if ts:
            if delete_message(channel, ts):
                deleted.append(ts_key)

    # Extension notes — may have multiple
    for ext_ts in stored.get("extension_ts_list", []):
        if ext_ts:
            if delete_message(channel, ext_ts):
                deleted.append(f"extension:{ext_ts}")

    # Delete parent card last
    parent_ts = stored.get("thread_ts")
    if parent_ts:
        if delete_message(channel, parent_ts):
            deleted.append("thread_ts")

    print(f"  Thread cleanup complete — deleted: {deleted}")
    return deleted

# ─────────────────────────────────────────────
#  APPIC
# ─────────────────────────────────────────────

def fetch_bookings():
    now        = dubai_now()
    start_date = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    end_date   = (now + timedelta(days=60)).strftime("%Y-%m-%d")
    try:
        r = requests.post(
            APPIC_BOOKINGS_URL,
            data={"key": APPIC_KEY, "startDate": start_date, "endDate": end_date},
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
        if data.get("issuccess"):
            bookings = data.get("bookings", [])
            print(f"  Appic returned {len(bookings)} bookings")
            return bookings
        else:
            print(f"  Appic error: {data.get('message')}")
            return []
    except Exception as e:
        print(f"  API error: {e}")
        return []

# ─────────────────────────────────────────────
#  EXTRACT
# ─────────────────────────────────────────────

def extract(b):
    start  = (b.get("startDate") or "").strip()
    end    = (b.get("endDate")   or "").strip()
    s_time = (b.get("startTime") or "")[:5]
    e_time = (b.get("endTime")   or "")[:5]

    try:
        dur     = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days
        dur_str = f"{dur} day{'s' if dur != 1 else ''}"
    except:
        dur_str = "N/A"

    try:
        # ── Confirmed Appic fields ─────────────────────────────────────────
        grand_total      = float(b.get("grandTotal", 0)        or 0)
        excl_vat         = float(b.get("amountWithoutVat", 0)  or 0)
        vat_amt          = float(b.get("vatAmount", 0)         or 0)
        zero_dep         = float(b.get("zeroDepositFee", 0)    or 0)
        addon            = float(b.get("addOnCharges", 0)      or 0)
        delivery_chg     = float(b.get("dropoffCharge", 0)     or 0)
        pickup_chg       = float(b.get("pickupCharge", 0)      or 0)
        babyseat_chg     = float(b.get("babyseatCharge", 0)    or 0)
        insurance_chg    = float(b.get("insurance", 0)         or 0)
        chauffeur_chg    = float(b.get("chauffeurCharge", 0)   or 0)
        advance          = float(b.get("advanceReceived", 0)   or 0)

        def fmt(v):
            return f"AED {v:,.0f}" if v > 0 else "—"

        # Base rental = excl_vat minus all add-on charges (excl VAT)
        addons_excl = round((zero_dep + delivery_chg + pickup_chg +
                             babyseat_chg + insurance_chg + chauffeur_chg + addon) / 1.05, 2)
        base_rental = round(excl_vat - addons_excl, 2) if excl_vat > 0 else round(grand_total / 1.05, 2)

        total_excl_str   = fmt(excl_vat)  if excl_vat  > 0 else fmt(round(grand_total / 1.05, 2))
        total_vat_str    = fmt(vat_amt)   if vat_amt   > 0 else fmt(round(grand_total - grand_total / 1.05, 2))

        base_rental_str  = fmt(base_rental)
        zero_dep_str     = fmt(zero_dep)
        delivery_str     = fmt(delivery_chg)
        pickup_str       = fmt(pickup_chg)
        babyseat_str     = fmt(babyseat_chg)
        insurance_str    = fmt(insurance_chg)
        chauffeur_str    = fmt(chauffeur_chg)
        addon_str        = fmt(addon)
        total_amt        = fmt(grand_total)
        advance_amt      = fmt(advance)

    except:
        base_rental_str = zero_dep_str = delivery_str = pickup_str = "—"
        babyseat_str = insurance_str = chauffeur_str = addon_str = "—"
        total_excl_str = total_vat_str = total_amt = advance_amt = "—"

    pickup_loc  = (b.get("pickupLocation")  or "").strip()
    dropoff_loc = (b.get("dropoffLocation") or "").strip()
    remarks_raw = (b.get("remarks") or "").strip()
    loc_from_remarks = "—"
    if not pickup_loc and not dropoff_loc and remarks_raw:
        import re as _re
        loc_match = _re.search(
            r'(?:DELIVERY\s+)?LOCATION\s*[:;]\s*([^\r\n]+)',
            remarks_raw, _re.IGNORECASE
        )
        if loc_match:
            loc_from_remarks = loc_match.group(1).strip()
    location = pickup_loc or dropoff_loc or loc_from_remarks
    status   = (b.get("status") or "confirmed").lower()

    return {
        "agr_no":       (b.get("contractID")     or "—").strip(),
        "customer":     (b.get("customerName")   or "N/A").strip().title(),
        "mobile":       (b.get("mobile")         or "N/A").strip(),
        "email":        (b.get("clientEmail")    or "—").strip(),
        "lead_source":  (b.get("leadSource")     or "—").strip(),
        "agent":        (b.get("salesAgent")     or "—").strip(),
        "vehicle":      (b.get("vehicleName")    or "N/A").strip().title(),
        "plate":        (b.get("vehiclePlate")   or "N/A").strip(),
        "start":        start,
        "end":          end,
        "s_time":       s_time,
        "e_time":       e_time,
        "dur_str":      dur_str,
        "location":     location,
        # cost — all confirmed Appic fields
        "base_rental":  base_rental_str,
        "zero_dep":     zero_dep_str,
        "insurance":    insurance_str,
        "delivery":     delivery_str,
        "pickup":       pickup_str,
        "babyseat":     babyseat_str,
        "chauffeur":    chauffeur_str,
        "addon":        addon_str,
        "total_excl":   total_excl_str,
        "total_vat":    total_vat_str,
        "total_amt":    total_amt,
        "advance":      advance_amt,
        "pay_mode":     (b.get("paymentMode")    or "—").strip(),
        "km_allowed":   (lambda: (
                            f"{int(float(b.get('dailyKmsLimit', 0) or 0))} KM/day"
                            if float(b.get("dailyKmsLimit", 0) or 0) > 0
                            else (lambda m: f"{m.group(1)} KM" if m else "—")(
                                __import__("re").search(
                                    r'\b(\d{2,4})\s*KM[S]?\b',
                                    (b.get("remarks", "") or "").upper()
                                )
                            )
                        ))(),
        "remarks":      (b.get("remarks")        or "—").strip() or "—",
        "status":       status,
        "status_label": "DRAFT" if status == "draft" else "CONFIRMED",
    }

# ─────────────────────────────────────────────
#  MESSAGE BUILDERS
# ─────────────────────────────────────────────

def build_booking_card(f, now_str):
    body = (
        f"```\n"
        f"{'AGR#':<14}: {f['agr_no']}\n"
        f"{'Status':<14}: {f['status_label']}\n"
        f"{'─' * 36}\n"
        f"{'Customer':<14}: {f['customer']}\n"
        f"{'Mobile':<14}: {f['mobile']}\n"
        f"{'Email':<14}: {f['email']}\n"
        f"{'Lead Source':<14}: {f['lead_source']}\n"
        f"{'Sales Agent':<14}: {f['agent']}\n"
        f"{'─' * 36}\n"
        f"{'Vehicle':<14}: {f['vehicle']}\n"
        f"{'Plate':<14}: {f['plate']}\n"
        f"{'Start':<14}: {fmt_date(f['start'])}  {f['s_time']}\n"
        f"{'End':<14}: {fmt_date(f['end'])}  {f['e_time']}\n"
        f"{'Duration':<14}: {f['dur_str']}\n"
        f"{'Delivery To':<14}: {f['location']}\n"
        f"{'─' * 36}\n"
        + (f"{'Base Rental':<14}: {f['base_rental']}\n" if f['base_rental'] != '—' else "")
        + (f"{'Zero Deposit':<14}: {f['zero_dep']}\n"   if f['zero_dep']    != '—' else "")
        + (f"{'Insurance':<14}: {f['insurance']}\n"     if f['insurance']   != '—' else "")
        + (f"{'Delivery':<14}: {f['delivery']}\n"       if f['delivery']    != '—' else "")
        + (f"{'Pickup':<14}: {f['pickup']}\n"           if f['pickup']      != '—' else "")
        + (f"{'Baby Seat':<14}: {f['babyseat']}\n"      if f['babyseat']    != '—' else "")
        + (f"{'Chauffeur':<14}: {f['chauffeur']}\n"     if f['chauffeur']   != '—' else "")
        + (f"{'Add-ons':<14}: {f['addon']}\n"           if f['addon']       != '—' else "")
        + f"{'─' * 36}\n"
        f"{'Total w/o VAT':<14}: {f['total_excl']}\n"
        f"{'VAT 5%':<14}: {f['total_vat']}\n"
        f"{'Grand Total':<14}: {f['total_amt']}\n"
        f"{'─' * 36}\n"
        f"{'Advance':<14}: {f['advance']}\n"
        f"{'Payment Mode':<14}: {f['pay_mode']}\n"
        f"{'KM Allowed':<14}: {f['km_allowed']}\n"
        f"{'─' * 36}\n"
        f"{'Remarks':<14}: {f['remarks']}\n"
        f"{'─' * 36}\n"
        f"{'Delivery':<14}: PENDING\n"
        f"{'Pickup':<14}: PENDING\n"
        f"```"
    )

    booking_data = json.dumps({
        "id":       f["agr_no"],
        "car":      f"{f['vehicle']} [{f['plate']}]",
        "date":     fmt_date(f["start"]),
        "time":     f["s_time"],
        "location": f["location"],
        "driver":   "",
        "out_km":   "",
    })

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "NEW BOOKING — MKV CAR RENTAL"}
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn",
                "text": f"Detected: {now_str}  |  Auto-alert via GitHub Actions"}]
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": body}
        },
        {
            "type": "divider"
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🚗  Delivery"},
                    "style": "primary",
                    "action_id": "open_delivery",
                    "value": booking_data
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔑  Pickup"},
                    "action_id": "open_pickup",
                    "value": booking_data
                },
            ]
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn",
                "text": "All updates will appear in this thread"}]
        },
    ]
    return blocks, f"New Booking: {f['customer']} | {f['vehicle']} ({f['plate']}) | {fmt_date(f['start'])} to {fmt_date(f['end'])} | {f['total_amt']}"


def build_delivery_checklist(f, now_str):
    info = (
        f"AGR#: {f['agr_no']} | "
        f"Customer: {f['customer']} | "
        f"Plate: {f['plate']} | "
        f"Date: {fmt_date(f['start'])} {f['s_time']} | "
        f"Amount: {f['total_amt']}"
    )

    booking_data = json.dumps({
        "id":       f["agr_no"],
        "car":      f"{f['vehicle']} [{f['plate']}]",
        "date":     fmt_date(f["start"]),
        "time":     f["s_time"],
        "location": f["location"],
        "driver":   "",
        "out_km":   "",
    })

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "DELIVERY CHECKLIST"}
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": info}]
        },
        {
            "type": "divider"
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🚗  Complete Delivery"},
                    "style": "primary",
                    "action_id": "open_delivery",
                    "value": booking_data
                },
            ]
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn",
                "text": f"Posted: {now_str}  |  Status: PENDING DELIVERY"}]
        },
    ]
    return blocks, f"Delivery: {f['customer']} | {f['vehicle']} ({f['plate']}) | {fmt_date(f['start'])} {f['s_time']}"


def build_pickup_checklist(f, now_str, channel=None, thread_ts=None):
    info = (
        f"AGR#: {f['agr_no']} | "
        f"Customer: {f['customer']} | "
        f"Mobile: {f['mobile']} | "
        f"Vehicle: {f['vehicle']} ({f['plate']}) | "
        f"Delivered: {fmt_date(f['start'])} {f['s_time']} | "
        f"Return Due: {fmt_date(f['end'])} {f['e_time']}"
    )

    booking_data = json.dumps({
        "id":       f["agr_no"],
        "car":      f"{f['vehicle']} [{f['plate']}]",
        "date":     fmt_date(f["start"]),
        "time":     f["s_time"],
        "location": f["location"],
        "driver":   "",
        "out_km":   "",
    })

    thread_link = ""
    if channel and thread_ts:
        thread_link = f" | <https://slack.com/app_redirect?channel={channel}&message_ts={thread_ts}|View Booking Thread>"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "PICKUP CHECKLIST — DUE TOMORROW"}
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": info}]
        },
        {
            "type": "divider"
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔑  Complete Pickup"},
                    "style": "primary",
                    "action_id": "open_pickup",
                    "value": booking_data
                },
            ]
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn",
                "text": f"Posted: {now_str}  |  Status: PENDING PICKUP{thread_link}"}]
        },
    ]
    return blocks, f"Pickup: {f['customer']} | {f['vehicle']} ({f['plate']}) | Return: {fmt_date(f['end'])} {f['e_time']}"


def build_contract_closed(f, now_str):
    body = (
        f"```\n"
        f"{'AGR#':<14}: {f['agr_no']}\n"
        f"{'Customer':<14}: {f['customer']}\n"
        f"{'Mobile':<14}: {f['mobile']}\n"
        f"{'Vehicle':<14}: {f['vehicle']}\n"
        f"{'Plate':<14}: {f['plate']}\n"
        f"{'Start':<14}: {fmt_date(f['start'])}  {f['s_time']}\n"
        f"{'End':<14}: {fmt_date(f['end'])}  {f['e_time']}\n"
        f"{'Duration':<14}: {f['dur_str']}\n"
        f"{'Delivery To':<14}: {f['location']}\n"
        f"{'Grand Total':<14}: {f['total_amt']}\n"
        f"{'─' * 36}\n"
        f"CONTRACT CLOSED — NO FURTHER ACTION REQUIRED\n"
        f"```"
    )
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "CONTRACT CLOSED"}
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": body}
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn",
                "text": f"Closed: {now_str}  |  Auto-detected from Appic"}]
        },
    ]
    return blocks, f"Closed: {f['customer']} | {f['vehicle']} ({f['plate']})"


# ─────────────────────────────────────────────
#  DOCUMENT UPLOAD
# ─────────────────────────────────────────────

def upload_file_to_slack(channel, thread_ts, filename, file_bytes):
    """Upload a file to Slack v2 API into a thread."""
    try:
        r1 = requests.post(
            "https://slack.com/api/files.getUploadURLExternal",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            data={"filename": filename, "length": len(file_bytes)},
            timeout=15
        )
        d1 = r1.json()
        if not d1.get("ok"):
            print(f"  getUploadURL error: {d1.get('error')}")
            return False
        upload_url = d1["upload_url"]
        file_id    = d1["file_id"]

        r2 = requests.post(upload_url, data=file_bytes, timeout=30)
        if r2.status_code not in (200, 201):
            print(f"  Upload failed: {r2.status_code}")
            return False

        r3 = requests.post(
            "https://slack.com/api/files.completeUploadExternal",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                     "Content-Type": "application/json"},
            json={
                "files":      [{"id": file_id, "title": filename}],
                "channel_id": channel,
                "thread_ts":  thread_ts,
            },
            timeout=15
        )
        d3 = r3.json()
        if not d3.get("ok"):
            print(f"  completeUpload error: {d3.get('error')}")
            return False
        return True
    except Exception as e:
        print(f"  upload_file_to_slack error: {e}")
        return False


def post_documents(channel, thread_ts, agr_no, customer, b):
    """
    Post customer documents as thread replies using URLs from the booking API.
    Fields: passportImg, passportExpImg, licenseImg, licenseExpiryImg, tradeLicenseImg
    """
    DOC_FIELDS = [
        ("passportImg",    "Passport"),
        ("passportExpImg", "Passport Expiry"),
        ("licenseImg",     "Driving Licence"),
        ("licenseExpiryImg", "Licence Expiry"),
        ("tradeLicenseImg",  "Trade Licence"),
    ]

    docs = []
    for field, label in DOC_FIELDS:
        url = str(b.get(field) or "").strip()
        if url and url.startswith("http"):
            docs.append((label, url))

    if not docs:
        print(f"  No documents found for {agr_no}")
        return

    # Header message
    post_message(channel, [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*DOCUMENTS*\nAGR#: {agr_no} | {customer}"}},
    ], f"Documents: {agr_no}", thread_ts=thread_ts)

    # Upload each doc
    for label, url in docs:
        try:
            print(f"  Downloading: {label}")
            r = requests.get(url, timeout=20)
            if r.status_code != 200:
                print(f"  Download failed ({r.status_code}): {url}")
                continue
            ct  = r.headers.get("Content-Type", "")
            ext = ".pdf" if "pdf" in ct else ".png" if "png" in ct else ".jpg"
            filename = f"{agr_no}_{label.replace(' ', '_')}{ext}"
            ok = upload_file_to_slack(channel, thread_ts, filename, r.content)
            print(f"  Upload {'OK' if ok else 'FAILED'}: {filename}")
        except Exception as e:
            print(f"  Doc upload error {label}: {e}")


# ─────────────────────────────────────────────
# ─────────────────────────────────────────────

def main():
    now      = dubai_now()
    now_str  = now.strftime("%d %b %Y | %I:%M %p Dubai Time")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    SEED_MODE = False

    print("=" * 56)
    print("  MKV BOOKING BOT")
    print(f"  {now_str}")
    print(f"  SEED MODE: {SEED_MODE}")
    print(f"  TEST MODE: {TEST_MODE}")
    print("=" * 56)

    store    = load_store()
    bookings = store.get("bookings", {})

    print("  Fetching bookings from Appic...")
    all_bookings = fetch_bookings()

    for b in all_bookings:
        key = booking_key(b)
        if not key:
            continue

        f        = extract(b)
        end      = f["end"]
        customer = f["customer"]
        plate    = f["plate"]
        start    = f["start"]
        status   = f["status"]

        # ── SEED MODE: register silently, no posts ────────────────────────
        if SEED_MODE:
            if key not in bookings:
                bookings[key] = {
                    "thread_ts":         None,
                    "delivery_ts":       None,
                    "pickup_ts":         None,
                    "closed_ts":         None,
                    "extension_ts_list": [],
                    "end_date":          end,
                    "plate":             plate,
                    "customer":          customer,
                    "vehicle":           f["vehicle"],
                    "delivery_alerted":  True,
                    "pickup_alerted":    False,
                    "closed":            False,
                    "start_date":        start,
                }
            continue

        # ── NEW BOOKING ───────────────────────────────────────────────────
        if key not in bookings:
            print(f"  NEW: {customer} | {plate} | {start} | {f['status_label']}")
            blocks, text = build_booking_card(f, now_str)
            ts = post_message(TARGET_CHANNEL, blocks, text)

            if ts:
                bookings[key] = {
                    "thread_ts":         ts,     # parent booking card
                    "delivery_ts":       None,   # filled below
                    "pickup_ts":         None,   # filled when pickup checklist posted
                    "closed_ts":         None,   # filled when contract closed
                    "extension_ts_list": [],     # appended on each extension
                    "end_date":          end,
                    "plate":             plate,
                    "customer":          customer,
                    "vehicle":           f["vehicle"],
                    "delivery_alerted":  False,
                    "pickup_alerted":    False,
                    "closed":            False,
                    "start_date":        start,
                }
                print(f"  Booking card posted — thread_ts: {ts}")

                # Post customer documents in thread
                post_documents(TARGET_CHANNEL, ts, f["agr_no"], f["customer"], b)

                # Delivery checklist reply — save ts
                d_blocks, d_text = build_delivery_checklist(f, now_str)
                d_ts = post_message(TARGET_CHANNEL, d_blocks, d_text, thread_ts=ts)
                if d_ts:
                    bookings[key]["delivery_ts"]     = d_ts
                    bookings[key]["delivery_alerted"] = True
                    print(f"  Delivery checklist posted — delivery_ts: {d_ts}")

        # ── EXISTING BOOKING ──────────────────────────────────────────────
        else:
            stored    = bookings.get(key, {})
            if not isinstance(stored, dict):
                continue
            thread_ts = stored.get("thread_ts")
            old_end   = stored.get("end_date", "")
            closed    = stored.get("closed", False)

            if closed:
                continue

            # Contract extension
            if end and old_end and end != old_end and end > old_end:
                print(f"  EXTENSION: {customer} | {plate} | {old_end} -> {end}")
                bookings[key]["end_date"]       = end
                bookings[key]["pickup_alerted"] = False
                if thread_ts:
                    try:
                        import datetime as _dt
                        days = (_dt.datetime.strptime(end, "%Y-%m-%d") - _dt.datetime.strptime(old_end, "%Y-%m-%d")).days
                        day_label = "days" if days != 1 else "day"
                        ext_text = (
                            "📋 *CONTRACT EXTENDED*\n"
                            + f"Previous End: {fmt_date(old_end)} — New End: {fmt_date(end)} (+{days} {day_label})\n"
                            + f"Detected: {now_str}"
                        )
                        ext_ts = post_message(TARGET_CHANNEL, [
                            {"type": "section", "text": {"type": "mrkdwn", "text": ext_text}}
                        ], ext_text, thread_ts=thread_ts)
                        if ext_ts:
                            if "extension_ts_list" not in bookings[key]:
                                bookings[key]["extension_ts_list"] = []
                            bookings[key]["extension_ts_list"].append(ext_ts)
                            print(f"  Extension note posted — ext_ts: {ext_ts}")
                    except Exception as ex:
                        print(f"  Extension note error: {ex}")

            # Pickup checklist — due tomorrow
            if end == tomorrow and not stored.get("pickup_alerted") and thread_ts:
                print(f"  PICKUP CHECKLIST: {customer} | {plate} | due {end}")
                p_blocks, p_text = build_pickup_checklist(f, now_str, TARGET_CHANNEL, thread_ts)
                p_ts = post_message(TARGET_CHANNEL, p_blocks, p_text, thread_ts=thread_ts)
                if p_ts:
                    bookings[key]["pickup_ts"]      = p_ts
                    bookings[key]["pickup_alerted"] = True
                    print(f"  Pickup checklist posted — pickup_ts: {p_ts}")

            # Contract closed
            if status == "closed" and not closed and thread_ts:
                print(f"  CONTRACT CLOSED: {customer} | {plate}")
                c_blocks, c_text = build_contract_closed(f, now_str)
                c_ts = post_message(TARGET_CHANNEL, c_blocks, c_text, thread_ts=thread_ts)
                if c_ts:
                    bookings[key]["closed_ts"] = c_ts
                    bookings[key]["closed"]    = True
                    print(f"  Contract closed posted — closed_ts: {c_ts}")

    store["bookings"] = bookings
    save_store(store)

    if SEED_MODE:
        print(f"  SEED MODE ON — {len(bookings)} bookings stored silently")

    print("=" * 56)
    print("  Done")
    print("=" * 56)


if __name__ == "__main__":
    main()
