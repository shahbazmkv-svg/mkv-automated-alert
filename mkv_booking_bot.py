import requests
import json
import os
from datetime import datetime, timezone, timedelta

# ---------------------------------------------
#  CONFIG
# ---------------------------------------------
APPIC_KEY          = os.environ["APPIC_KEY"]
SLACK_BOT_TOKEN    = os.environ["SLACK_BOT_TOKEN"]

CHANNEL_BOOKINGS   = "C0ABPC606F7"   # #mkv-bookings (ROOT)
CHANNEL_SCHEDULE   = "C0ACB9C8J01"   # #mkv-schedule-for-delivery
CHANNEL_TEST       = "C0B0TGBDCDU"   # #mkvtest

TEST_MODE          = False
TARGET_CHANNEL     = CHANNEL_TEST if TEST_MODE else CHANNEL_BOOKINGS

APPIC_BOOKINGS_URL = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-bookings.php"
APPIC_CHECK_URL    = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-checkin-checkout.php"
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
        return f"AED {f:,.0f}" if f > 0 else "-"
    except:
        return "-"

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
    payload = {
        "channel": channel,
        "text": text,
        "blocks": blocks,
        "unfurl_links": False,
        "unfurl_media": False,
    }
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

def upload_file_to_slack(channel, thread_ts, filename, content):
    try:
        r1 = requests.get(
            "https://slack.com/api/files.getUploadURLExternal",
            headers=SLACK_HEADERS,
            params={"filename": filename, "length": len(content)},
            timeout=10
        )
        d1 = r1.json()
        if not d1.get("ok"):
            print(f"  getUploadURL failed: {d1.get('error')}")
            return False

        upload_url = d1["upload_url"]
        file_id = d1["file_id"]

        r2 = requests.post(upload_url, data=content, timeout=30)
        if r2.status_code != 200:
            print(f"  Upload failed: {r2.status_code}")
            return False

        r3 = requests.post(
            "https://slack.com/api/files.completeUploadExternal",
            headers=SLACK_HEADERS,
            json={"files": [{"id": file_id}], "channel_id": channel, "thread_ts": thread_ts},
            timeout=10
        )
        d3 = r3.json()
        if not d3.get("ok"):
            print(f"  completeUpload failed: {d3.get('error')}")
            return False
        return True
    except Exception as e:
        print(f"  upload_file_to_slack error: {e}")
        return False

def find_first_url(item, names):
    if not isinstance(item, dict):
        return ""
    wanted = {n.lower() for n in names}
    for key, value in item.items():
        key_norm = str(key).lower().replace("_", "").replace("-", "")
        if key_norm in wanted:
            url = str(value or "").strip()
            if url.startswith("http"):
                return url
    for value in item.values():
        if isinstance(value, dict):
            url = find_first_url(value, names)
            if url:
                return url
    return ""

def post_documents(b, check_record, agr_no, customer, channel, thread_ts):
    doc_fields = [
        ("Passport", ["passportimg", "passportimage", "passport", "passporturl"]),
        ("Passport Expiry", ["passportexpimg", "passportexpiryimg", "passportexpiryimage", "passportexpiry"]),
        ("Driving Licence", ["licenseimg", "licenceimg", "drivinglicenseimg", "drivinglicenceimg", "licenseimage"]),
        ("Licence Expiry", ["licenseexpiryimg", "licenceexpiryimg", "licenseexpimg", "licenceexpimg"]),
        ("Trade Licence", ["tradelicenseimg", "tradelicenceimg", "tradelicense"]),
        ("Emirates ID", ["emiratesidimg", "emiratesidimage", "emiratesid"]),
        ("Visa", ["visaimg", "visaimage", "visa"]),
    ]
    docs = []
    for label, names in doc_fields:
        url = find_first_url(check_record, names) or find_first_url(b, names)
        if url:
            docs.append((label, url))

    if not docs:
        print(f"  No documents found for {agr_no}")
        return

    post_message(channel, [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*DOCUMENTS*\nAGR#: {agr_no} | {customer}"}}
    ], f"Documents: {agr_no}", thread_ts=thread_ts)

    for label, url in docs:
        try:
            print(f"  Downloading: {label}")
            r = requests.get(url, timeout=20)
            if r.status_code != 200:
                print(f"  Download failed ({r.status_code}): {url}")
                continue
            ct = r.headers.get("Content-Type", "")
            ext = ".pdf" if "pdf" in ct else ".png" if "png" in ct else ".jpg"
            filename = f"{agr_no}_{label.replace(' ', '_')}{ext}"
            ok = upload_file_to_slack(channel, thread_ts, filename, r.content)
            print(f"  Upload {'OK' if ok else 'FAILED'}: {filename}")
        except Exception as e:
            print(f"  Doc upload error {label}: {e}")

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

def fetch_checkin_checkout(direction):
    now = dubai_now()
    start_date = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    end_date = (now + timedelta(days=60)).strftime("%Y-%m-%d")
    try:
        r = requests.post(
            APPIC_CHECK_URL,
            data={
                "key": APPIC_KEY,
                "startDate": start_date,
                "endDate": end_date,
                "direction": direction,
            },
            timeout=20
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            records = (
                data.get("data") or
                data.get("bookings") or
                data.get("records") or
                data.get("checkinCheckout") or
                data.get("checkins") or
                data.get("checkouts") or
                []
            )
        elif isinstance(data, list):
            records = data
        else:
            records = []
        print(f"  Appic {direction} returned {len(records)} records")
        return records if isinstance(records, list) else []
    except Exception as e:
        print(f"  {direction} API error: {e}")
        return []

def match_check_record(records, f):
    agr = str(f.get("agr_no") or "").strip().lower()
    plate = str(f.get("plate") or "").strip().lower()
    customer = str(f.get("customer") or "").strip().lower()
    for item in records:
        if not isinstance(item, dict):
            continue
        hay = " ".join(str(v or "") for v in item.values()).lower()
        if agr and agr in hay:
            return item
        if plate and plate in hay:
            return item
        if customer and customer in hay:
            return item
    return {}

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

    # FINANCIALS
    try:
        rental_val   = float(b.get("baseRental")       or b.get("amount")          or 0)
        zero_dep_val = float(b.get("cardooAmount")     or b.get("zeroDepositFee")  or 0)
        delivery_val = float(b.get("deliveryCharges")  or b.get("dropoffCharge")   or 0)
        pickup_val   = float(b.get("pickupCharges")    or b.get("pickupCharge")    or 0)
        babyseat_val = float(b.get("babySeatCharges")  or b.get("babyseatCharge")  or 0)
        addon_val    = float(b.get("addOnCharges")     or 0)

        # Grand total and VAT - direct from Appic
        grand_total  = float(b.get("total")            or b.get("grandTotal")      or b.get("amount") or 0)
        vat_val      = float(b.get("vatAmount")        or round(grand_total * 5 / 105, 0))
        wo_vat_val   = float(b.get("amountWithoutVat") or round(grand_total / 1.05, 0))
        advance_val  = float(b.get("advancePayment")   or b.get("advanceReceived") or 0)
        deposit_val  = float(b.get("deposit")          or 0)
        paid_val     = float(b.get("paidInTotal")      or 0)
        balance_val  = grand_total - paid_val if paid_val > 0 else grand_total - advance_val

        def amt(v): return f"AED {v:,.0f}" if v > 0 else "-"

        rental_str       = amt(rental_val)
        zero_dep_str     = amt(zero_dep_val)
        delivery_str     = amt(delivery_val)
        pickup_str       = amt(pickup_val)
        babyseat_str     = amt(babyseat_val)
        addon_str        = amt(addon_val)
        wo_vat_str       = f"AED {wo_vat_val:,.0f}"   if wo_vat_val   > 0 else "TBC"
        vat_str          = f"AED {vat_val:,.0f}"       if vat_val      > 0 else "-"
        grand_str        = f"AED {grand_total:,.0f}"   if grand_total  > 0 else "TBC"
        advance_str      = amt(advance_val)
        deposit_str      = amt(deposit_val)
        balance_str      = f"AED {balance_val:,.0f}"   if balance_val  > 0 else "-"
    except:
        rental_str = zero_dep_str = delivery_str = pickup_str = "-"
        babyseat_str = addon_str = wo_vat_str = vat_str = "-"
        grand_str = advance_str = deposit_str = balance_str = "TBC"

    # LOCATION
    location = (
        b.get("deliveryLocation") or
        b.get("pickupLocation")   or
        b.get("dropoffLocation")  or
        b.get("address")          or "-"
    ).strip() or "-"

    # KM ALLOWED
    import re as _re
    # Try Appic field first (dailyKmsLimit from UI)
    daily_km_api = str(b.get("dailyKmsLimit") or b.get("kmAllowed") or b.get("allowedKm") or "").strip()
    remarks_raw  = (b.get("remarks") or "").upper()
    km_match     = _re.search(r'(\d+)\s*KM\s*(?:PER\s*DAY|\/DAY)?', remarks_raw)

    if daily_km_api and daily_km_api not in ("0", ""):
        try:
            km_allowed = f"{int(daily_km_api) * max(dur, 1)} KM  ({daily_km_api} KM/day)"
        except:
            km_allowed = f"{daily_km_api} KM/day"
    elif km_match:
        try:
            daily_km   = int(km_match.group(1))
            km_allowed = f"{daily_km * max(dur, 1)} KM  ({daily_km} KM/day)"
        except:
            km_allowed = f"{km_match.group(1)} KM/day"
    else:
        km_allowed = "-"

    # EXTRA KM CHARGE
    extra_km_rate = str(b.get("extraKmCharge") or b.get("extraKmRate") or "").strip()
    extra_km_str  = f"AED {float(extra_km_rate):,.0f}/KM" if extra_km_rate and extra_km_rate != "0" else "-"

    # STATUS
    status = (b.get("status") or "confirmed").lower().strip()

    return {
        "agr_no":        (b.get("contractID")     or "-").strip(),
        "customer":      (b.get("customerName")   or "N/A").strip().title(),
        "mobile":        (b.get("mobile")         or b.get("phone") or "N/A").strip(),
        "email":         (b.get("clientEmail")    or b.get("email") or "-").strip(),
        "lead_source":   (b.get("source")         or b.get("leadSource") or "-").strip(),
        "agent":         (b.get("salesAgent")     or b.get("salesAgentName") or "-").strip(),
        "vehicle":       (b.get("vehicleName")    or "N/A").strip().title(),
        "plate":         (b.get("vehiclePlate")   or "N/A").strip(),
        "start":         start,
        "end":           end,
        "s_time":        s_time,
        "e_time":        e_time,
        "dur_str":       dur_str,
        "location":      location,
        "rental_amt":    rental_str,
        "zero_dep":      zero_dep_str,
        "delivery":      delivery_str,
        "pickup_fee":    pickup_str,
        "babyseat":      babyseat_str,
        "addon":         addon_str,
        "wo_vat":        wo_vat_str,
        "vat":           vat_str,
        "grand_total":   grand_str,
        "advance":       advance_str,
        "deposit":       deposit_str,
        "balance":       balance_str,
        "pay_mode":      (b.get("paymentMode")    or "-").strip(),
        "km_allowed":    km_allowed,
        "extra_km_rate": extra_km_str,
        "remarks":       (b.get("remarks")        or "-").strip() or "-",
        "status":        status,
        "status_label":  "DRAFT" if status == "draft" else "CONFIRMED",
    }

# ---------------------------------------------
#  MESSAGE BUILDERS
# ---------------------------------------------
def build_booking_card(f, now_str):
    body = (
        f"```\n"
        f"{'AGR#':<14}: {f['agr_no']}\n"
        f"{'Status':<14}: {f['status_label']}\n"
        f"{'-' * 36}\n"
        f"{'Customer':<14}: {f['customer']}\n"
        f"{'Mobile':<14}: {f['mobile']}\n"
        f"{'Email':<14}: {f['email']}\n"
        f"{'Lead Source':<14}: {f['lead_source']}\n"
        f"{'Sales Agent':<14}: {f['agent']}\n"
        f"{'-' * 36}\n"
        f"{'Vehicle':<14}: {f['vehicle']}\n"
        f"{'Plate':<14}: {f['plate']}\n"
        f"{'Start':<14}: {fmt_date(f['start'])}  {f['s_time']}\n"
        f"{'End':<14}: {fmt_date(f['end'])}  {f['e_time']}\n"
        f"{'Duration':<14}: {f['dur_str']}\n"
        f"{'Delivery To':<14}: {f['location']}\n"
        f"{'-' * 36}\n"
        + (f"{'Rental':<14}: {f['rental_amt']}\n"       if f['rental_amt']   != '-' else "")
        + (f"{'Zero Deposit':<14}: {f['zero_dep']}\n"   if f['zero_dep']     != '-' else "")
        + (f"{'Delivery':<14}: {f['delivery']}\n"       if f['delivery']     != '-' else "")
        + (f"{'Pickup Fee':<14}: {f['pickup_fee']}\n"   if f['pickup_fee']   != '-' else "")
        + (f"{'Baby Seat':<14}: {f['babyseat']}\n"      if f['babyseat']     != '-' else "")
        + (f"{'Add-ons':<14}: {f['addon']}\n"           if f['addon']        != '-' else "")
        +
        f"{'-' * 36}\n"
        f"{'Amt w/o VAT':<14}: {f['wo_vat']}\n"
        f"{'VAT 5%':<14}: {f['vat']}\n"
        f"{'Grand Total':<14}: {f['grand_total']}\n"
        f"{'-' * 36}\n"
        f"{'Advance':<14}: {f['advance']}\n"
        + (f"{'Deposit':<14}: {f['deposit']}\n"         if f['deposit']      != '-' else "")
        +
        f"{'Balance':<14}: {f['balance']}\n"
        f"{'Payment Mode':<14}: {f['pay_mode']}\n"
        f"{'KM Allowed':<14}: {f['km_allowed']}\n"
        + (f"{'Extra KM Rate':<14}: {f['extra_km_rate']}\n" if f['extra_km_rate'] != '-' else "")
        +
        f"{'-' * 36}\n"
        f"{'Remarks':<14}: {f['remarks']}\n"
        f"{'-' * 36}\n"
        f"{'Delivery':<14}: PENDING\n"
        f"{'Pickup':<14}: PENDING\n"
        f"```"
    )
    booking_data = json.dumps({
        "id": f["agr_no"],
        "car": f"{f['vehicle']} [{f['plate']}]",
        "date": fmt_date(f["start"]),
        "time": f["s_time"],
        "location": f["location"],
        "driver": "",
        "out_km": "",
    })
    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "NEW BOOKING - MKV CAR RENTAL"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": f"Detected: {now_str}  |  Auto-alert via GitHub Actions"}]},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": body}},
        {"type": "divider"},
        {"type": "actions",
         "elements": [
             {"type": "button",
              "text": {"type": "plain_text", "text": "Delivery"},
              "style": "primary",
              "action_id": "open_delivery",
              "value": booking_data},
             {"type": "button",
              "text": {"type": "plain_text", "text": "Pickup"},
              "action_id": "open_pickup",
              "value": booking_data},
         ]},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": "All updates will appear in this thread"}]},
    ]
    return blocks, f"New Booking: {f['customer']} | {f['vehicle']} ({f['plate']}) | {fmt_date(f['start'])} to {fmt_date(f['end'])} | {f['grand_total']}"


def build_schedule_delivery_notice(f, now_str, booking_channel, booking_ts):
    thread_link = f"https://slack.com/app_redirect?channel={booking_channel}&message_ts={booking_ts}"
    booking_data = json.dumps({
        "id": f["agr_no"],
        "car": f"{f['vehicle']} [{f['plate']}]",
        "date": fmt_date(f["start"]),
        "time": f["s_time"],
        "location": f["location"],
        "driver": "",
        "out_km": "",
    })
    body = (
        f"```\n"
        f"{'AGR#':<14}: {f['agr_no']}\n"
        f"{'Customer':<14}: {f['customer']}\n"
        f"{'Mobile':<14}: {f['mobile']}\n"
        f"{'Vehicle':<14}: {f['vehicle']}\n"
        f"{'Plate':<14}: {f['plate']}\n"
        f"{'Delivery':<14}: {fmt_date(f['start'])} {f['s_time']}\n"
        f"{'Location':<14}: {f['location']}\n"
        f"```"
    )
    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "SCHEDULE FOR DELIVERY"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": f"Same-day booking detected: {now_str}"}]},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": body}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"<{thread_link}|Open full booking thread>"}},
        {"type": "actions",
         "elements": [
             {"type": "button",
              "text": {"type": "plain_text", "text": "Delivery"},
              "style": "primary",
              "action_id": "open_delivery",
              "value": booking_data},
         ]},
    ]
    return blocks, f"Schedule delivery: {f['customer']} | {f['vehicle']} ({f['plate']})"


def build_delivery_checklist(f, now_str):
    # Message 1 - Info block (collapsed via context)
    info = (
        f"{'AGR#':<14}: {f['agr_no']} | "
        f"{'Customer'}: {f['customer']} | "
        f"{'Plate'}: {f['plate']} | "
        f"{'Date'}: {fmt_date(f['start'])} {f['s_time']} | "
        f"{'Amount'}: {f['grand_total']}"
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
         "text": {"type": "plain_text", "text": "PICKUP CHECKLIST - DUE TOMORROW"}},
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
        f"{'Total':<14}: {f['grand_total']}\n"
        f"{'-' * 36}\n"
        f"CONTRACT CLOSED - NO FURTHER ACTION REQUIRED\n"
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


# ---------------------------------------------
#  MAIN
# ---------------------------------------------
def main():
    now      = dubai_now()
    now_str  = now.strftime("%d %b %Y | %I:%M %p Dubai Time")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    # SEED MODE
    # True  = stores all bookings silently (first run)
    # False = normal mode, posts new bookings
    SEED_MODE = False
    # -------------------------------------------

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
    check_out_records = fetch_checkin_checkout("Out")
    check_in_records = fetch_checkin_checkout("In")

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
                print(f"  Booking card posted - thread: {ts}")

                # Fetch and post Appic customer documents in the booking thread.
                check_record = match_check_record(check_out_records + check_in_records, f)
                post_documents(b, check_record, f["agr_no"], f["customer"], TARGET_CHANNEL, ts)

                # Same-day booking â†’ also post to #mkv-schedule-for-delivery
                today = dubai_now().strftime("%Y-%m-%d")
                if start == today and not TEST_MODE:
                    print(f"  Same-day booking â†’ posting to #mkv-schedule-for-delivery")
                    s_blocks, s_text = build_schedule_delivery_notice(f, now_str, TARGET_CHANNEL, ts)
                    post_message(CHANNEL_SCHEDULE, s_blocks, s_text)

                d_blocks, d_text = build_delivery_checklist(f, now_str)
                d_ts = post_message(TARGET_CHANNEL, d_blocks, d_text, thread_ts=ts)
                if d_ts:
                    bookings[key]["delivery_alerted"] = True
                    print(f"  Delivery checklist posted in thread")

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
        print(f"  SEED MODE ON - {len(bookings)} bookings stored silently")

    print("=" * 56)
    print("  Done")
    print("=" * 56)

if __name__ == "__main__":
    main()
