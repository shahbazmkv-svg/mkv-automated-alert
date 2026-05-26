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
CHANNEL_TEST       = "C0B0TGBDCDU"   # #mkv-test-automation

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

def dubai_now():
    return datetime.now(DUBAI_TZ)

def fmt_date(d):
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%d %b %Y")
    except:
        return d or "N/A"

def fmt_amount(v):
    try:
        f = float(v)
        return f"AED {f:,.0f}" if f > 0 else "—"
    except:
        return "—"

def fmt_amount_zero(v):
    """Always show AED value including zero"""
    try:
        return f"AED {float(v or 0):,.0f}"
    except:
        return "AED 0"

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
            print(f"  Slack error: {data.get('error')}")
            return None
    except Exception as e:
        print(f"  Slack request failed: {e}")
        return None

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
        # Appic API returns issuccess OR isSuccess depending on version
        bookings = data.get("bookings", [])
        success = (
            data.get("issuccess") or
            data.get("isSuccess") or
            data.get("success") or
            isinstance(bookings, list)
        )
        if success:
            print(f"  Appic returned {len(bookings)} bookings")
            return bookings
        else:
            print(f"  Appic error: {data.get('message') or data.get('error') or 'unknown'}")
            return []
    except Exception as e:
        print(f"  API error: {e}")
        return []

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

    # ── FINANCIALS (confirmed Appic fields) ─────────────────────────
    try:
        rental_v    = float(b.get("amount", 0)            or 0)
        zero_dep_v  = float(b.get("zeroDepositFee", 0)    or 0)
        addon_v     = float(b.get("addOnCharges", 0)       or 0)
        vat_v       = float(b.get("vatAmount", 0)          or 0)
        grand_total = float(b.get("grandTotal", 0)         or 0)
        excl_vat    = float(b.get("amountWithoutVat", 0)   or 0)
        advance_v   = float(b.get("advanceReceived", 0)    or 0)

        # Use grandTotal if available, else sum components
        total   = grand_total if grand_total > 0 else (rental_v + zero_dep_v + addon_v + vat_v)
        balance = total - advance_v

        def fmt(v): return f"AED {v:,.0f}" if v > 0 else "—"

        rental_amt   = fmt(rental_v)
        zero_dep_amt = fmt(zero_dep_v)
        addon_amt    = fmt(addon_v)
        vat_amt      = fmt(vat_v)
        excl_vat_amt = fmt(excl_vat)
        total_amt    = f"AED {total:,.0f}" if total > 0 else "TBC"
        advance_amt  = fmt(advance_v)
        balance_amt  = fmt(balance) if balance > 0 else "—"
    except:
        rental_amt = zero_dep_amt = addon_amt = vat_amt = excl_vat_amt = advance_amt = balance_amt = "—"
        total_amt  = "TBC"

    # ── KM (confirmed Appic fields) ──────────────────────────────────
    km_raw = b.get("dailyKmsLimit", "") or ""
    try:
        km_val = int(float(km_raw))
        km_allowed = f"{km_val} KM/day" if km_val > 0 else "—"
    except:
        km_allowed = "—"
    try:
        ekm = float(b.get("extraKmCharge", 0) or 0)
        extra_km_rate = f"AED {ekm:,.0f}/KM" if ekm > 0 else "—"
    except:
        extra_km_rate = "—"

    # ── LOCATION ────────────────────────────────────────────────────
    pickup_loc  = (b.get("pickupLocation")  or "").strip()
    dropoff_loc = (b.get("dropoffLocation") or "").strip()
    remarks_raw = (b.get("remarks") or "").strip()
    loc_from_remarks = "—"
    if not pickup_loc and not dropoff_loc and remarks_raw:
        import re as _re
        m = _re.search(r'(?:DELIVERY\s+)?LOCATION\s*[:;]\s*([^\r\n]+)', remarks_raw, _re.IGNORECASE)
        if m:
            loc_from_remarks = m.group(1).strip()
    location = pickup_loc or dropoff_loc or loc_from_remarks

    status = (b.get("status") or "confirmed").lower()

    return {
        "agr_no":       (b.get("contractID")   or "—").strip(),
        "customer":     (b.get("customerName") or "N/A").strip().title(),
        "mobile":       (b.get("mobile")       or "N/A").strip(),
        "email":        (b.get("clientEmail")  or "—").strip(),
        "lead_source":  (b.get("leadSource")   or "—").strip(),
        "agent":        (b.get("salesAgent")   or "—").strip(),
        "vehicle":      (b.get("vehicleName")  or "N/A").strip().title(),
        "plate":        (b.get("vehiclePlate") or "N/A").strip(),
        "start":        start,
        "end":          end,
        "s_time":       s_time,
        "e_time":       e_time,
        "dur_str":      dur_str,
        "location":     location,
        "rental_amt":   rental_amt,
        "zero_dep":     zero_dep_amt,
        "addon":        addon_amt,
        "vat":          vat_amt,
        "excl_vat":     excl_vat_amt,
        "total_amt":    total_amt,
        "advance":      advance_amt,
        "balance":      balance_amt,
        "pay_mode":     (b.get("paymentMode") or "—").strip(),
        "km_allowed":   km_allowed,
        "extra_km_rate": extra_km_rate,
        "remarks":      remarks_raw or "—",
        "status":       status,
        "status_label": "DRAFT" if status == "draft" else "CONFIRMED",
    }

def upload_file_to_slack(channel, thread_ts, filename, file_url, title):
    """3-step Slack file upload: get URL → PUT file → complete"""
    try:
        r1 = requests.post(
            "https://slack.com/api/files.getUploadURLExternal",
            headers=SLACK_HEADERS,
            json={"filename": filename, "length": 1},
            timeout=10
        )
        d1 = r1.json()
        if not d1.get("ok"):
            print(f"  Upload URL error: {d1.get('error')}")
            return False

        upload_url = d1["upload_url"]
        file_id    = d1["file_id"]

        # Download the actual file from Appic
        fr = requests.get(file_url, timeout=15)
        if fr.status_code != 200:
            print(f"  File download failed: {fr.status_code} — {file_url}")
            return False

        # PUT file to Slack
        requests.put(upload_url, data=fr.content,
                     headers={"Content-Type": "application/octet-stream"}, timeout=15)

        # Complete upload
        r3 = requests.post(
            "https://slack.com/api/files.completeUploadExternal",
            headers=SLACK_HEADERS,
            json={"files": [{"id": file_id, "title": title}],
                  "channel_id": channel, "thread_ts": thread_ts},
            timeout=10
        )
        d3 = r3.json()
        if d3.get("ok"):
            print(f"  ✅ Uploaded: {title}")
            return True
        else:
            print(f"  Complete error: {d3.get('error')} — {title}")
            return False
    except Exception as e:
        print(f"  Upload exception: {e} — {title}")
        return False


def post_documents(b, channel, thread_ts):
    """Upload all available documents from Appic booking into Slack thread"""
    docs = [
        ("passportImg",      "passport.jpg",       "Passport"),
        ("passportExpImg",   "passport_exp.jpg",   "Passport Expiry"),
        ("licenseImg",       "licence.jpg",         "Driving Licence"),
        ("licenseExpiryImg", "licence_exp.jpg",    "Licence Expiry"),
        ("emiratesIdImg",    "emirates_id.jpg",    "Emirates ID"),
        ("visaImg",          "visa.jpg",            "Visa"),
    ]
    uploaded = 0
    for field, fname, title in docs:
        url = (b.get(field) or "").strip()
        if url and url.startswith("http"):
            if upload_file_to_slack(channel, thread_ts, fname, url, title):
                uploaded += 1
    if uploaded == 0:
        print("  No documents found in Appic booking")
    else:
        print(f"  {uploaded} document(s) uploaded to thread")



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
        f"{'Rental':<14}: {f['rental_amt']}\n"
        + (f"{'Zero Deposit':<14}: {f['zero_dep']}\n" if f['zero_dep'] != '—' else "")
        + (f"{'Add-on':<14}: {f['addon']}\n"          if f['addon']    != '—' else "")
        + f"{'VAT (5%)':<14}: {f['vat']}\n"
        + f"{'Grand Total':<14}: {f['total_amt']}\n"
        + f"{'─' * 36}\n"
        + f"{'Advance':<14}: {f['advance']}\n"
        + f"{'Balance':<14}: {f['balance']}\n"
        + f"{'Payment Mode':<14}: {f['pay_mode']}\n"
        + f"{'KM Allowed':<14}: {f['km_allowed']}\n"
        + (f"{'Extra KM':<14}: {f['extra_km_rate']}\n" if f['extra_km_rate'] != '—' else "")
        + f"{'─' * 36}\n"
        + f"{'Remarks':<14}: {f['remarks']}\n"
        + f"{'─' * 36}\n"
        + f"{'Delivery':<14}: PENDING\n"
        + f"{'Pickup':<14}: PENDING\n"
        + f"```"
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
        {"type": "header",
         "text": {"type": "plain_text", "text": "🚘 NEW BOOKING — MKV CAR RENTAL"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": f"Detected: {now_str}  |  Auto-alert via GitHub Actions"}]},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": body}},
        {"type": "actions",
         "elements": [
             {"type": "button", "text": {"type": "plain_text", "text": "🚗  Delivery"},
              "style": "primary", "action_id": "open_delivery", "value": booking_data},
             {"type": "button", "text": {"type": "plain_text", "text": "🔑  Pickup"},
              "action_id": "open_pickup", "value": booking_data},
         ]},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": "All updates will appear in this thread"}]},
    ]
    return blocks, f"New Booking: {f['customer']} | {f['vehicle']} ({f['plate']}) | {fmt_date(f['start'])} → {fmt_date(f['end'])} | {f['total_amt']}"


def build_delivery_checklist(f, now_str):
    # Message 1 - Info block (collapsed via context)
    info = (
        f"{'AGR#':<14}: {f['agr_no']} | "
        f"{'Customer'}: {f['customer']} | "
        f"{'Plate'}: {f['plate']} | "
        f"{'Date'}: {fmt_date(f['start'])} {f['s_time']} | "
        f"{'Amount'}: {f['total_amt']}"
    )
    # Message 2 - Driver reply template (prominent, easy to copy)
    reply = (
        f"```\n"
        f"AGR#: {f['agr_no']}\n"
        f"Out KM:\n"
        f"Fuel Level:\n"
        f"Driver Name:\n"
        f"Remarks:\n"
        f"```"
    )
    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "DELIVERY CHECKLIST"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn", "text": info}]},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn",
             "text": "*Copy and reply with delivery details:*\n" + reply}},
        {"type": "section",
         "text": {"type": "mrkdwn",
             "text": "Attach: Contract PDF + Car Photos + Emirates ID"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": f"Posted: {now_str}  |  Status: PENDING DELIVERY"}]},
    ]
    return blocks, f"Delivery: {f['customer']} | {f['vehicle']} ({f['plate']}) | {fmt_date(f['start'])} {f['s_time']}"
    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "DELIVERY CHECKLIST"}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": body}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": f"Posted: {now_str}  |  Status: PENDING DELIVERY"}]},
    ]
    return blocks, f"Delivery: {f['customer']} | {f['vehicle']} ({f['plate']}) | {fmt_date(f['start'])} {f['s_time']}"


def build_extension_checklist(f, now_str, old_end, new_end):
    try:
        extra   = (datetime.strptime(new_end, "%Y-%m-%d") - datetime.strptime(old_end, "%Y-%m-%d")).days
        ext_str = f"+{extra} day{'s' if extra != 1 else ''}"
    except:
        ext_str = "Extended"

    info = (
        f"AGR#: {f['agr_no']} | "
        f"Customer: {f['customer']} | "
        f"Vehicle: {f['vehicle']} ({f['plate']}) | "
        f"Previous End: {fmt_date(old_end)} | "
        f"New End: {fmt_date(new_end)} ({ext_str})"
    )
    reply = (
        f"```\n"
        f"AGR#: {f['agr_no']}\n"
        f"Ext Amount: AED\n"
        f"Payment Mode:\n"
        f"Pay Status:\n"
        f"Remarks:\n"
        f"```"
    )
    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "CONTRACT EXTENSION"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn", "text": info}]},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn",
             "text": "*Copy and reply with extension details:*\n" + reply}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": f"Detected: {now_str}  |  Status: EXTENDED"}]},
    ]
    return blocks, f"Extension: {f['customer']} | {f['vehicle']} | New end: {fmt_date(new_end)} ({ext_str})"


def build_pickup_checklist(f, now_str):
    info = (
        f"AGR#: {f['agr_no']} | "
        f"Customer: {f['customer']} | "
        f"Mobile: {f['mobile']} | "
        f"Vehicle: {f['vehicle']} ({f['plate']}) | "
        f"Delivered: {fmt_date(f['start'])} {f['s_time']} | "
        f"Return Due: {fmt_date(f['end'])} {f['e_time']}"
    )
    reply = (
        f"```\n"
        f"AGR#: {f['agr_no']}\n"
        f"In KM:\n"
        f"Extra KM:\n"
        f"Fuel Charge: AED\n"
        f"Salik: AED\n"
        f"Fines: AED\n"
        f"Damage: AED\n"
        f"Amt Collected: AED\n"
        f"Payment Mode:\n"
        f"Remarks:\n"
        f"```"
    )
    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "PICKUP CHECKLIST — DUE TOMORROW"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn", "text": info}]},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn",
             "text": "*Copy and reply with pickup details:*\n" + reply}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": f"Posted: {now_str}  |  Status: PENDING PICKUP"}]},
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
        f"{'Total':<14}: {f['total_amt']}\n"
        f"{'─' * 36}\n"
        f"CONTRACT CLOSED — NO FURTHER ACTION REQUIRED\n"
        f"```"
    )
    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "CONTRACT CLOSED"}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": body}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": f"Closed: {now_str}  |  Auto-detected from Appic"}]},
    ]
    return blocks, f"Closed: {f['customer']} | {f['vehicle']} ({f['plate']})"


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    now      = dubai_now()
    now_str  = now.strftime("%d %b %Y | %I:%M %p Dubai Time")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    # ── SEED MODE ──────────────────────────────
    # True  = stores all bookings silently (first run)
    # False = normal mode, posts new bookings
    SEED_MODE = False
    # ───────────────────────────────────────────

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

        if SEED_MODE:
            if key not in bookings:
                bookings[key] = {
                    "thread_ts":        None,
                    "end_date":         end,
                    "plate":            plate,
                    "customer":         customer,
                    "vehicle":          f["vehicle"],
                    "delivery_alerted": True,
                    "pickup_alerted":   False,
                    "closed":           False,
                    "start_date":       start,
                }
            continue

        if key not in bookings:
            print(f"  NEW: {customer} | {plate} | {start} | {f['status_label']}")
            blocks, text = build_booking_card(f, now_str)
            ts = post_message(TARGET_CHANNEL, blocks, text)
            if ts:
                bookings[key] = {
                    "thread_ts":        ts,
                    "end_date":         end,
                    "plate":            plate,
                    "customer":         customer,
                    "vehicle":          f["vehicle"],
                    "delivery_alerted": False,
                    "pickup_alerted":   False,
                    "closed":           False,
                    "start_date":       start,
                }
                print(f"  Booking card posted — thread: {ts}")

                # Post documents in thread
                post_documents(b, TARGET_CHANNEL, ts)

                # Same-day booking → also post to #mkv-schedule-for-delivery
                today = now.strftime("%Y-%m-%d")
                if start == today:
                    post_message(TARGET_DELIVERY, blocks, text)
                    print(f"  Same-day booking — also posted to delivery channel")

                bookings[key]["delivery_alerted"] = True

        else:
            stored    = bookings.get(key, {})
            if not isinstance(stored, dict):
                continue
            thread_ts = stored.get("thread_ts")
            old_end   = stored.get("end_date", "")
            closed    = stored.get("closed", False)

            if closed:
                continue

            if end and old_end and end != old_end and end > old_end:
                print(f"  EXTENSION: {customer} | {plate} | {old_end} -> {end}")
                if thread_ts:
                    e_blocks, e_text = build_extension_checklist(f, now_str, old_end, end)
                    e_ts = post_message(TARGET_CHANNEL, e_blocks, e_text, thread_ts=thread_ts)
                    if e_ts:
                        bookings[key]["end_date"]       = end
                        bookings[key]["pickup_alerted"] = False
                        print(f"  Extension checklist posted in thread")
                else:
                    bookings[key]["end_date"] = end

            if end == tomorrow and not stored.get("pickup_alerted") and thread_ts:
                print(f"  PICKUP CHECKLIST: {customer} | {plate} | due {end}")
                p_blocks, p_text = build_pickup_checklist(f, now_str)
                p_ts = post_message(TARGET_CHANNEL, p_blocks, p_text, thread_ts=thread_ts)
                if p_ts:
                    bookings[key]["pickup_alerted"] = True
                    print(f"  Pickup checklist posted in thread")

            if status == "closed" and not closed and thread_ts:
                print(f"  CONTRACT CLOSED: {customer} | {plate}")
                c_blocks, c_text = build_contract_closed(f, now_str)
                c_ts = post_message(TARGET_CHANNEL, c_blocks, c_text, thread_ts=thread_ts)
                if c_ts:
                    bookings[key]["closed"] = True
                    print(f"  Contract closed posted in thread")

    store["bookings"] = bookings
    save_store(store)

    if SEED_MODE:
        print(f"  SEED MODE ON — {len(bookings)} bookings stored silently")

    print("=" * 56)
    print("  Done")
    print("=" * 56)

if __name__ == "__main__":
    main()
