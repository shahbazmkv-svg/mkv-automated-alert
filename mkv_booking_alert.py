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
CHANNEL_TEST       = "C0B0TGBDCDU"   # #mkv-test-automation

TEST_MODE          = False
TARGET_CHANNEL     = CHANNEL_TEST if TEST_MODE else CHANNEL_BOOKINGS

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
    try:
        f = float(v or 0)
        return f"AED {f:,.0f}" if f > 0 else "—"
    except:
        return "—"

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
    try:
        dur     = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days
        dur_str = f"{dur} day{'s' if dur != 1 else ''}"
    except:
        dur_str = "N/A"
    try:
        amt_val       = float(b.get("baseRental", 0) or b.get("rentalAmount", 0) or b.get("amount", 0) or 0)
        zero_dep_v    = float(b.get("zeroDepositFee", 0)  or 0)
        delivery_v    = float(b.get("dropoffCharge", 0)   or b.get("deliveryCharges", 0) or 0)
        pickup_v      = float(b.get("pickupCharge", 0)    or b.get("pickupCharges", 0)   or 0)
        babyseat_v    = float(b.get("babyseatCharge", 0)  or b.get("babySeatCharges", 0) or 0)
        insurance_v   = float(b.get("insurance", 0)       or 0)
        chauffeur_v   = float(b.get("chauffeurCharge", 0) or 0)
        api_vat       = float(b.get("vatAmount", 0)       or 0)
        grand_total   = float(b.get("grandTotal", 0)      or 0)
        excl_vat      = float(b.get("amountWithoutVat", 0)or 0)
        advance       = float(b.get("advanceReceived", 0) or 0)

        # Totals — use Appic values directly
        total         = grand_total if grand_total > 0 else (amt_val + zero_dep_v + delivery_v + pickup_v + babyseat_v + insurance_v + chauffeur_v + api_vat)
        balance       = total - advance

        def fmt(v): return f"AED {v:,.0f}" if v > 0 else "—"

        rental_amt    = fmt(amt_val)     if amt_val    > 0 else "TBC"
        zero_dep_amt  = fmt(zero_dep_v)
        delivery_amt  = fmt(delivery_v)
        pickup_amt    = fmt(pickup_v)
        babyseat_amt  = fmt(babyseat_v)
        insurance_amt = fmt(insurance_v)
        chauffeur_amt = fmt(chauffeur_v)
        vat_amt       = fmt(api_vat)
        excl_vat_amt  = fmt(excl_vat)
        total_amt     = fmt(total)       if total      > 0 else "TBC"
        advance_amt   = fmt(advance)
        balance_amt   = fmt(balance)     if balance    > 0 else "—"
    except:
        rental_amt = zero_dep_amt = delivery_amt = pickup_amt = babyseat_amt = "—"
        insurance_amt = chauffeur_amt = vat_amt = excl_vat_amt = advance_amt = balance_amt = "—"
        total_amt = "TBC"

    pickup_loc  = (b.get("pickupLocation")  or "").strip()
    dropoff_loc = (b.get("dropoffLocation") or "").strip()
    # Try to extract location from remarks if API fields empty
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
    status      = (b.get("status") or "confirmed").lower()

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
        "rental_amt":   rental_amt,
        "zero_dep":     zero_dep_amt,
        "delivery":     delivery_amt,
        "pickup_chg":   pickup_amt,
        "babyseat":     babyseat_amt,
        "insurance":    insurance_amt,
        "chauffeur":    chauffeur_amt,
        "vat":          vat_amt,
        "excl_vat":     excl_vat_amt,
        "total_amt":    total_amt,
        "advance":      advance_amt,
        "balance":      balance_amt,
        "pay_mode":     (b.get("paymentMode")    or "—").strip(),
        "km_allowed":   (lambda r: next(
                            (w.rstrip("S") + " KM" for w in r.upper().replace("KMS","KM").replace("KM"," KM ").split()
                             if w.rstrip("S").isdigit() and 50 < int(w.rstrip("S")) <= 1000),
                            "—"
                        ))(b.get("remarks", "") or ""),
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
        f"{'Rental':<14}: {f['rental_amt']}\n"
        + (f"{'Zero Deposit':<14}: {f['zero_dep']}\n"   if f['zero_dep']   != '—' else "")
        + (f"{'Delivery':<14}: {f['delivery']}\n"       if f['delivery']   != '—' else "")
        + (f"{'Pickup':<14}: {f['pickup_chg']}\n"       if f['pickup_chg'] != '—' else "")
        + (f"{'Baby Seat':<14}: {f['babyseat']}\n"      if f['babyseat']   != '—' else "")
        + (f"{'Insurance':<14}: {f['insurance']}\n"     if f['insurance']  != '—' else "")
        + (f"{'Chauffeur':<14}: {f['chauffeur']}\n"     if f['chauffeur']  != '—' else "")
        + f"{'─' * 36}\n"
        f"{'Total w/o VAT':<14}: {f['excl_vat']}\n"
        f"{'VAT 5%':<14}: {f['vat']}\n"
        f"{'Grand Total':<14}: {f['total_amt']}\n"
        f"{'─' * 36}\n"
        f"{'Advance':<14}: {f['advance']}\n"
        f"{'Balance':<14}: {f['balance']}\n"
        f"{'Payment Mode':<14}: {f['pay_mode']}\n"
        f"{'KM Allowed':<14}: {f['km_allowed']}\n"
        f"{'─' * 36}\n"
        f"{'Remarks':<14}: {f['remarks']}\n"
        f"{'─' * 36}\n"
        f"{'Delivery':<14}: PENDING\n"
        f"{'Pickup':<14}: PENDING\n"
        f"```"
    )

    # Booking data for interactive buttons
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

    # Build direct link to booking thread in Slack
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
        f"{'Total':<14}: {f['total_amt']}\n"
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
#  MAIN
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
                bookings[key]["end_date"]       = end
                bookings[key]["pickup_alerted"] = False
                if thread_ts:
                    try:
                        import datetime as _dt
                        days = (_dt.datetime.strptime(end, "%Y-%m-%d") - _dt.datetime.strptime(old_end, "%Y-%m-%d")).days
                        day_label = "days" if days != 1 else "day"
                        ext_text = (
                            "📋 *CONTRACT EXTENDED*\n"
                            + f"Previous End: {fmt_date(old_end)} - New End: {fmt_date(end)} (+{days} {day_label})\n"
                            + f"Detected: {now_str}"
                        )
                        post_message(TARGET_CHANNEL, [
                            {"type": "section", "text": {"type": "mrkdwn", "text": ext_text}}
                        ], ext_text, thread_ts=thread_ts)
                        print(f"  Extension note posted in thread")
                    except Exception as ex:
                        print(f"  Extension note error: {ex}")

            if end == tomorrow and not stored.get("pickup_alerted") and thread_ts:
                print(f"  PICKUP CHECKLIST: {customer} | {plate} | due {end}")
                p_blocks, p_text = build_pickup_checklist(f, now_str, TARGET_CHANNEL, thread_ts)
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
