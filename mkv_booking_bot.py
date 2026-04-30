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
CHANNEL_TEST       = "C0B0TGBDCDU"   # #mkvtest

TEST_MODE          = True
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
        amt_val    = float(b.get("amount", 0) or 0)
        zero_dep_v = float(b.get("zeroDepositFee", 0) or 0)
        addon_val  = float(b.get("addOnCharges", 0) or 0)
        # VAT = 5% of (Rental + Zero Deposit + Add-ons)
        vat_base   = amt_val + zero_dep_v + addon_val
        api_vat    = float(b.get("vatAmount", 0) or 0)
        vat_val    = api_vat if api_vat > 0 else round(vat_base * 0.05, 2)
        # Use Appic grandTotal directly
        grand_total = float(b.get("grandTotal", 0) or 0)
        advance     = float(b.get("advanceReceived", 0) or 0)
        # Recalculate components for display
        vat_base    = amt_val + zero_dep_v + addon_val
        api_vat     = float(b.get("vatAmount", 0) or 0)
        vat_val     = api_vat if api_vat > 0 else round(vat_base * 0.05, 2)
        # Use grandTotal if available, else calculate
        total       = grand_total if grand_total > 0 else (amt_val + zero_dep_v + addon_val + vat_val)
        balance     = total - advance

        rental_amt  = f"AED {amt_val:,.0f}"    if amt_val   > 0 else "TBC"
        zero_dep_amt= f"AED {zero_dep_v:,.0f}" if zero_dep_v> 0 else "—"
        addon_amt   = f"AED {addon_val:,.0f}"  if addon_val > 0 else "—"
        vat_amt     = f"AED {vat_val:,.0f}"    if vat_val   > 0 else "—"
        advance_amt = f"AED {advance:,.0f}"    if advance   > 0 else "—"
        balance_amt = f"AED {balance:,.0f}"    if balance   > 0 else "—"
        total_amt   = f"AED {total:,.0f}"      if total     > 0 else "TBC"
    except:
        rental_amt = zero_dep_amt = addon_amt = vat_amt = advance_amt = balance_amt = "—"
        total_amt  = "TBC"

    pickup_loc  = (b.get("pickupLocation")  or "").strip()
    dropoff_loc = (b.get("dropoffLocation") or "").strip()
    location    = pickup_loc or dropoff_loc or "—"
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
        "addon":        addon_amt,
        "vat":          vat_amt,
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
        f"{'─' * 36}\n"
        f"{'Remarks':<14}: {f['remarks']}\n"
        f"{'─' * 36}\n"
        f"{'Delivery':<14}: PENDING\n"
        f"{'Pickup':<14}: PENDING\n"
        f"```"
    )

    # Booking data for interactive buttons
    booking_data = json.dumps({
        "id":     f["agr_no"],
        "car":    f"{f['vehicle']} [{f['plate']}]",
        "date":   fmt_date(f["start"]),
        "time":   f["s_time"],
        "driver": "",
        "out_km": "",
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
        "id":     f["agr_no"],
        "car":    f"{f['vehicle']} [{f['plate']}]",
        "date":   fmt_date(f["start"]),
        "time":   f["s_time"],
        "driver": "",
        "out_km": "",
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
        "id":     f["agr_no"],
        "car":    f"{f['vehicle']} [{f['plate']}]",
        "date":   fmt_date(f["start"]),
        "time":   f["s_time"],
        "driver": "",
        "out_km": "",
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
                # Update end date in store — no extension card posted
                bookings[key]["end_date"]       = end
                bookings[key]["pickup_alerted"] = False
                if thread_ts:
                    # Post a simple extension note in the booking thread
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
