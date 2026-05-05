import requests
import json
import os
import re
from datetime import datetime, timezone, timedelta

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
APPIC_KEY            = os.environ["APPIC_KEY"]
SLACK_BOT_TOKEN      = os.environ["SLACK_BOT_TOKEN"]

CHANNEL_BOOKINGS     = "C0ABPC606F7"   # #mkv-bookings
CHANNEL_DELIVERY     = "C0ACB9C8J01"   # #mkv-schedule-for-delivery
CHANNEL_PICKUP       = "REPLACE_ME"    # #mkv-schedule-for-pickup  ← paste real channel ID
CHANNEL_TEST         = "C0B0TGBDCDU"   # #mkvtest

TEST_MODE            = False
TARGET_BOOKINGS      = CHANNEL_TEST if TEST_MODE else CHANNEL_BOOKINGS
TARGET_DELIVERY      = CHANNEL_TEST if TEST_MODE else CHANNEL_DELIVERY
TARGET_PICKUP        = CHANNEL_TEST if TEST_MODE else CHANNEL_PICKUP

APPIC_BOOKINGS_URL   = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-bookings.php"
STORE_FILE           = "booking_thread_store.json"
DUBAI_TZ             = timezone(timedelta(hours=4))
SLACK_HEADERS        = {
    "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    "Content-Type": "application/json"
}

# ─────────────────────────────────────────────
#  FLOW
#  1. New booking  →  #mkv-bookings: full card + [Delivery] + [Pickup] buttons
#  2. [Delivery] clicked  →  #mkv-delivery: delivery card + [Pickup] button
#  3. [Pickup] clicked  →  #mkv-pickup: pickup card (no further buttons)
#  4. Auto scheduled (day before):
#       start date  →  #mkv-schedule-for-delivery: info card with thread
#       end date    →  #mkv-schedule-for-pickup:   info card with thread
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

def extract(b):
    start  = (b.get("startDate") or "").strip()
    end    = (b.get("endDate")   or "").strip()
    s_time = (b.get("startTime") or "")[:5]
    e_time = (b.get("endTime")   or "")[:5]
    dur = 0
    try:
        dur     = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days
        dur_str = f"{dur} day{'s' if dur != 1 else ''}"
    except:
        dur_str = "N/A"

    try:
        amt_val     = float(b.get("amount", 0) or 0)
        zero_dep_v  = float(b.get("zeroDepositFee", 0) or 0)
        addon_val   = float(b.get("addOnCharges", 0) or 0)
        api_vat     = float(b.get("vatAmount", 0) or 0)
        vat_base    = amt_val + zero_dep_v + addon_val
        vat_val     = api_vat if api_vat > 0 else round(vat_base * 0.05, 2)
        grand_total = float(b.get("grandTotal", 0) or 0)
        advance     = float(b.get("advanceReceived", 0) or 0)
        total       = grand_total if grand_total > 0 else (amt_val + zero_dep_v + addon_val + vat_val)
        balance     = total - advance

        rental_amt   = f"AED {amt_val:,.0f}"    if amt_val    > 0 else "TBC"
        zero_dep_amt = f"AED {zero_dep_v:,.0f}" if zero_dep_v > 0 else "—"
        addon_amt    = f"AED {addon_val:,.0f}"  if addon_val  > 0 else "—"
        vat_amt      = f"AED {vat_val:,.0f}"    if vat_val    > 0 else "—"
        advance_amt  = f"AED {advance:,.0f}"    if advance    > 0 else "—"
        balance_amt  = f"AED {balance:,.0f}"    if balance    > 0 else "—"
        total_amt    = f"AED {total:,.0f}"      if total      > 0 else "TBC"
    except:
        rental_amt = zero_dep_amt = addon_amt = vat_amt = advance_amt = balance_amt = "—"
        total_amt  = "TBC"

    pickup_loc  = (b.get("pickupLocation")  or "").strip()
    dropoff_loc = (b.get("dropoffLocation") or "").strip()
    remarks_raw = (b.get("remarks") or "").strip()
    loc_from_remarks = "—"
    if not pickup_loc and not dropoff_loc and remarks_raw:
        loc_match = re.search(
            r'(?:DELIVERY\s+)?LOCATION\s*[:;]\s*([^\r\n]+)',
            remarks_raw, re.IGNORECASE
        )
        if loc_match:
            loc_from_remarks = loc_match.group(1).strip()
    location = pickup_loc or dropoff_loc or loc_from_remarks
    status   = (b.get("status") or "confirmed").lower()

    def resolve_km(api_km, remarks):
        if api_km:
            try:
                v = float(api_km)
                if v > 0:
                    return f"{int(v)} KM"
            except:
                pass
        r = remarks.upper()
        m = re.search(r'(\d+)\s*KM[S]?\s*PER\s*DAY', r)
        if m:
            return f"{m.group(1)} KM"
        m2 = re.search(r'(\d+)\s*KM[S]?\b', r)
        if m2:
            return f"{m2.group(1)} KM"
        return "—"

    def resolve_extra_km(val):
        try:
            v = float(val or 0)
            return f"AED {int(v)}/KM" if v > 0 else "—"
        except:
            return "—"

    api_km_field   = b.get("dailyKmsLimit") or b.get("dailyKmLimit") or b.get("kmLimit") or ""
    extra_km_field = b.get("extraKmCharge") or b.get("extraKmRate") or b.get("extraKmFee") or 0

    return {
        "agr_no":          (b.get("contractID")   or "—").strip(),
        "customer":        (b.get("customerName") or "N/A").strip().title(),
        "mobile":          (b.get("mobile")       or "N/A").strip(),
        "email":           (b.get("clientEmail")  or "—").strip(),
        "lead_source":     (b.get("leadSource")   or "—").strip(),
        "agent":           (b.get("salesAgent")   or "—").strip(),
        "vehicle":         (b.get("vehicleName")  or "N/A").strip().title(),
        "plate":           (b.get("vehiclePlate") or "N/A").strip(),
        "start":           start,
        "end":             end,
        "s_time":          s_time,
        "e_time":          e_time,
        "dur_str":         dur_str,
        "location":        location,
        "rental_amt":      rental_amt,
        "zero_dep":        zero_dep_amt,
        "addon":           addon_amt,
        "vat":             vat_amt,
        "total_amt":       total_amt,
        "advance":         advance_amt,
        "balance":         balance_amt,
        "pay_mode":        (b.get("paymentMode")  or "—").strip(),
        "km_allowed":      resolve_km(api_km_field, remarks_raw),
        "extra_km_charge": resolve_extra_km(extra_km_field),
        "remarks":         remarks_raw or "—",
        "status":          status,
        "status_label":    "DRAFT" if status == "draft" else "CONFIRMED",
    }


# ─────────────────────────────────────────────
#  MESSAGE BUILDERS
# ─────────────────────────────────────────────

def _booking_payload(f):
    return json.dumps({
        "agr_no":   f["agr_no"],
        "car":      f"{f['vehicle']} [{f['plate']}]",
        "customer": f["customer"],
        "mobile":   f["mobile"],
        "start":    fmt_date(f["start"]),
        "s_time":   f["s_time"],
        "end":      fmt_date(f["end"]),
        "e_time":   f["e_time"],
        "location": f["location"],
        "total":    f["total_amt"],
        "balance":  f["balance"],
        "km":       f["km_allowed"],
        "driver":   "",
        "out_km":   "",
    })


def build_booking_card(f, now_str):
    """#mkv-bookings — auto posted on new booking. Has [Delivery] + [Pickup] buttons."""
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
        f"{'Zero Deposit':<14}: {f['zero_dep']}\n"
        f"{'Add-on':<14}: {f['addon']}\n"
        f"{'VAT (5%)':<14}: {f['vat']}\n"
        f"{'Total':<14}: {f['total_amt']}\n"
        f"{'Advance':<14}: {f['advance']}\n"
        f"{'Balance':<14}: {f['balance']}\n"
        f"{'Payment Mode':<14}: {f['pay_mode']}\n"
        f"{'KM Allowed':<14}: {f['km_allowed']}\n"
        f"{'Extra KM':<14}: {f['extra_km_charge']}\n"
        f"{'─' * 36}\n"
        f"{'Remarks':<14}: {f['remarks']}\n"
        f"{'─' * 36}\n"
        f"{'Delivery':<14}: PENDING\n"
        f"{'Pickup':<14}: PENDING\n"
        f"```"
    )
    bd = _booking_payload(f)
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "NEW BOOKING — MKV CAR RENTAL"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"Detected: {now_str}  |  Auto-alert via GitHub Actions"}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {"type": "divider"},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "🚗  Delivery"},
             "style": "primary", "action_id": "open_delivery", "value": bd},
            {"type": "button", "text": {"type": "plain_text", "text": "🔑  Pickup"},
             "action_id": "open_pickup", "value": bd},
        ]},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "Click Delivery when vehicle goes out · Click Pickup when vehicle returns"}]},
    ]
    return blocks, f"New Booking: {f['customer']} | {f['vehicle']} ({f['plate']}) | {fmt_date(f['start'])} → {fmt_date(f['end'])} | {f['total_amt']}"


def build_delivery_card(f, now_str):
    """
    #mkv-delivery — posted when [Delivery] button is clicked in #mkv-bookings.
    Has [Pickup] button so staff can trigger pickup from here.
    """
    body = (
        f"```\n"
        f"{'AGR#':<14}: {f['agr_no']}\n"
        f"{'─' * 36}\n"
        f"{'Customer':<14}: {f['customer']}\n"
        f"{'Mobile':<14}: {f['mobile']}\n"
        f"{'─' * 36}\n"
        f"{'Vehicle':<14}: {f['vehicle']}\n"
        f"{'Plate':<14}: {f['plate']}\n"
        f"{'Delivery Date':<14}: {fmt_date(f['start'])}  {f['s_time']}\n"
        f"{'Return Date':<14}: {fmt_date(f['end'])}  {f['e_time']}\n"
        f"{'Duration':<14}: {f['dur_str']}\n"
        f"{'Delivery To':<14}: {f['location']}\n"
        f"{'─' * 36}\n"
        f"{'Balance':<14}: {f['balance']}\n"
        f"{'KM Allowed':<14}: {f['km_allowed']}\n"
        f"{'Extra KM':<14}: {f['extra_km_charge']}\n"
        f"```"
    )
    bd = _booking_payload(f)
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "🚗  DELIVERY — MKV CAR RENTAL"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"Logged: {now_str}"}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {"type": "divider"},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "🔑  Pickup"},
             "style": "primary", "action_id": "open_pickup", "value": bd},
        ]},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "Click Pickup when vehicle is returned"}]},
    ]
    return blocks, f"Delivery: {f['customer']} | {f['vehicle']} ({f['plate']}) | Return: {fmt_date(f['end'])} {f['e_time']}"


def build_pickup_card(f, now_str):
    """
    #mkv-pickup — posted when [Pickup] button is clicked in #mkv-delivery.
    No further action buttons.
    """
    body = (
        f"```\n"
        f"{'AGR#':<14}: {f['agr_no']}\n"
        f"{'─' * 36}\n"
        f"{'Customer':<14}: {f['customer']}\n"
        f"{'Mobile':<14}: {f['mobile']}\n"
        f"{'─' * 36}\n"
        f"{'Vehicle':<14}: {f['vehicle']}\n"
        f"{'Plate':<14}: {f['plate']}\n"
        f"{'Delivered':<14}: {fmt_date(f['start'])}  {f['s_time']}\n"
        f"{'Return Due':<14}: {fmt_date(f['end'])}  {f['e_time']}\n"
        f"{'KM Allowed':<14}: {f['km_allowed']}\n"
        f"{'Extra KM':<14}: {f['extra_km_charge']}\n"
        f"{'Balance':<14}: {f['balance']}\n"
        f"```"
    )
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "🔑  PICKUP — MKV CAR RENTAL"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"Logged: {now_str}"}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "Vehicle returned — verify KM and condition"}]},
    ]
    return blocks, f"Pickup: {f['customer']} | {f['vehicle']} ({f['plate']}) | Due: {fmt_date(f['end'])} {f['e_time']}"


def build_scheduled_delivery(f, now_str):
    """
    #mkv-schedule-for-delivery — auto posted day before start date.
    Informational only, no buttons. Posts with thread so updates stay grouped.
    """
    body = (
        f"```\n"
        f"{'AGR#':<14}: {f['agr_no']}\n"
        f"{'Customer':<14}: {f['customer']}\n"
        f"{'Mobile':<14}: {f['mobile']}\n"
        f"{'Vehicle':<14}: {f['vehicle']} ({f['plate']})\n"
        f"{'Delivery':<14}: {fmt_date(f['start'])}  {f['s_time']}\n"
        f"{'Return':<14}: {fmt_date(f['end'])}  {f['e_time']}\n"
        f"{'Duration':<14}: {f['dur_str']}\n"
        f"{'Location':<14}: {f['location']}\n"
        f"{'Balance':<14}: {f['balance']}\n"
        f"{'KM Allowed':<14}: {f['km_allowed']}\n"
        f"```"
    )
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "📅  DELIVERY TOMORROW"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"Auto-scheduled: {now_str}"}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
    ]
    return blocks, f"Delivery Tomorrow: {f['customer']} | {f['vehicle']} ({f['plate']}) | {fmt_date(f['start'])} {f['s_time']}"


def build_scheduled_pickup(f, now_str):
    """
    #mkv-schedule-for-pickup — auto posted day before end date.
    Informational only, no buttons. Posts with thread so updates stay grouped.
    """
    body = (
        f"```\n"
        f"{'AGR#':<14}: {f['agr_no']}\n"
        f"{'Customer':<14}: {f['customer']}\n"
        f"{'Mobile':<14}: {f['mobile']}\n"
        f"{'Vehicle':<14}: {f['vehicle']} ({f['plate']})\n"
        f"{'Delivered':<14}: {fmt_date(f['start'])}  {f['s_time']}\n"
        f"{'Return Due':<14}: {fmt_date(f['end'])}  {f['e_time']}\n"
        f"{'KM Allowed':<14}: {f['km_allowed']}\n"
        f"{'Extra KM':<14}: {f['extra_km_charge']}\n"
        f"{'Balance':<14}: {f['balance']}\n"
        f"```"
    )
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "📅  PICKUP TOMORROW"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"Auto-scheduled: {now_str}"}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
    ]
    return blocks, f"Pickup Tomorrow: {f['customer']} | {f['vehicle']} ({f['plate']}) | Due: {fmt_date(f['end'])} {f['e_time']}"


def build_contract_closed(f, now_str):
    """#mkv-bookings thread — auto posted when Appic status = closed."""
    body = (
        f"```\n"
        f"{'AGR#':<14}: {f['agr_no']}\n"
        f"{'Customer':<14}: {f['customer']}\n"
        f"{'Vehicle':<14}: {f['vehicle']} ({f['plate']})\n"
        f"{'Start':<14}: {fmt_date(f['start'])}  {f['s_time']}\n"
        f"{'End':<14}: {fmt_date(f['end'])}  {f['e_time']}\n"
        f"{'Total':<14}: {f['total_amt']}\n"
        f"{'─' * 36}\n"
        f"CONTRACT CLOSED — NO FURTHER ACTION REQUIRED\n"
        f"```"
    )
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "CONTRACT CLOSED"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {"type": "context", "elements": [{"type": "mrkdwn",
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

    # Scheduled delivery/pickup alerts fire only at 7 PM Dubai time (19:00–19:59)
    is_7pm_run = (now.hour == 19)

    SEED_MODE = True

    print("=" * 56)
    print("  MKV BOOKING BOT")
    print(f"  {now_str}")
    print(f"  SEED MODE: {SEED_MODE}")
    print(f"  TEST MODE: {TEST_MODE}")
    print(f"  7PM RUN (scheduled alerts): {is_7pm_run}")
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
        start    = f["start"]
        customer = f["customer"]
        plate    = f["plate"]
        status   = f["status"]

        # ── SEED MODE: silently register all existing bookings ───────────────
        if SEED_MODE:
            if key not in bookings:
                bookings[key] = {
                    "booking_ts":          None,
                    "sched_delivery_ts":   None,
                    "sched_pickup_ts":     None,
                    "end_date":            end,
                    "start_date":          start,
                    "plate":               plate,
                    "customer":            customer,
                    "vehicle":             f["vehicle"],
                    "sched_delivery_done": True,   # suppress for existing bookings
                    "sched_pickup_done":   True,   # suppress for existing bookings
                    "closed":              False,
                }
            continue

        # ── NEW BOOKING ──────────────────────────────────────────────────────
        # Posts to #mkv-bookings only, with [Delivery] + [Pickup] buttons
        if key not in bookings:
            print(f"  NEW: {customer} | {plate} | {start} → {end}")

            blocks, text = build_booking_card(f, now_str)
            booking_ts = post_message(TARGET_BOOKINGS, blocks, text)
            if booking_ts:
                print(f"  Booking card → #mkv-bookings (ts: {booking_ts})")

            bookings[key] = {
                "booking_ts":          booking_ts,
                "sched_delivery_ts":   None,
                "sched_pickup_ts":     None,
                "end_date":            end,
                "start_date":          start,
                "plate":               plate,
                "customer":            customer,
                "vehicle":             f["vehicle"],
                "sched_delivery_done": False,
                "sched_pickup_done":   False,
                "closed":              False,
            }

        # ── EXISTING BOOKING ─────────────────────────────────────────────────
        else:
            stored     = bookings.get(key, {})
            if not isinstance(stored, dict):
                continue

            booking_ts = stored.get("booking_ts")
            old_end    = stored.get("end_date", "")
            closed     = stored.get("closed", False)

            if closed:
                continue

            # Extension detected — always runs, not gated to 7PM
            if end and old_end and end != old_end and end > old_end:
                print(f"  EXTENSION: {customer} | {plate} | {old_end} → {end}")
                bookings[key]["end_date"]          = end
                bookings[key]["sched_pickup_done"] = False
                if booking_ts:
                    try:
                        import datetime as _dt
                        days = (_dt.datetime.strptime(end, "%Y-%m-%d") - _dt.datetime.strptime(old_end, "%Y-%m-%d")).days
                        day_label = "day" if days == 1 else "days"
                        ext_text = (
                            f"*CONTRACT EXTENDED*\n"
                            f"Previous End: {fmt_date(old_end)}  →  New End: {fmt_date(end)} (+{days} {day_label})\n"
                            f"Detected: {now_str}"
                        )
                        post_message(TARGET_BOOKINGS, [
                            {"type": "section", "text": {"type": "mrkdwn", "text": ext_text}}
                        ], ext_text, thread_ts=booking_ts)
                        print(f"  Extension note → #mkv-bookings thread")
                    except Exception as ex:
                        print(f"  Extension note error: {ex}")

            # Scheduled delivery — 7PM only, day before start date, with thread
            if is_7pm_run and start == tomorrow and not stored.get("sched_delivery_done"):
                print(f"  SCHED DELIVERY: {customer} | {plate} | {start}")
                sd_blocks, sd_text = build_scheduled_delivery(f, now_str)
                sd_ts = post_message(TARGET_DELIVERY, sd_blocks, sd_text)
                if sd_ts:
                    bookings[key]["sched_delivery_done"] = True
                    bookings[key]["sched_delivery_ts"]   = sd_ts
                    print(f"  Scheduled delivery → #mkv-schedule-for-delivery (ts: {sd_ts})")

            # Scheduled pickup — 7PM only, day before end date, with thread
            if is_7pm_run and end == tomorrow and not stored.get("sched_pickup_done"):
                print(f"  SCHED PICKUP: {customer} | {plate} | {end}")
                sp_blocks, sp_text = build_scheduled_pickup(f, now_str)
                sp_ts = post_message(TARGET_PICKUP, sp_blocks, sp_text)
                if sp_ts:
                    bookings[key]["sched_pickup_done"] = True
                    bookings[key]["sched_pickup_ts"]   = sp_ts
                    print(f"  Scheduled pickup → #mkv-schedule-for-pickup (ts: {sp_ts})")

            # Contract closed — always runs, not gated to 7PM
            if status == "closed" and not closed and booking_ts:
                print(f"  CONTRACT CLOSED: {customer} | {plate}")
                c_blocks, c_text = build_contract_closed(f, now_str)
                c_ts = post_message(TARGET_BOOKINGS, c_blocks, c_text, thread_ts=booking_ts)
                if c_ts:
                    bookings[key]["closed"] = True
                    print(f"  Closed note → #mkv-bookings thread")

    store["bookings"] = bookings
    save_store(store)

    if SEED_MODE:
        print(f"  SEED MODE ON — {len(bookings)} bookings registered silently, no alerts sent")

    print("=" * 56)
    print("  Done")
    print("=" * 56)

if __name__ == "__main__":
    main()
