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
CHANNEL_TEST       = "C0AVCCCG0S0"   # #mkvtest

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
        return datetime.strptime(d, "%Y-%m-%d").strftime("%d %b")
    except:
        return d or "N/A"

def fmt_date_full(d):
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

def booking_key(b):
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
        dur_str = f"{dur}d"
    except:
        dur_str = "N/A"
    try:
        amt_val    = float(b.get("amount", 0))
        rental_amt = f"AED {amt_val:,.0f}" if amt_val > 0 else "TBC"
    except:
        rental_amt = "TBC"

    return {
        "customer":     (b.get("customerName")  or "N/A").strip().title(),
        "mobile":       (b.get("mobile")        or "N/A").strip(),
        "vehicle":      (b.get("vehicleName")   or "N/A").strip().title(),
        "plate":        (b.get("vehiclePlate")  or "N/A").strip(),
        "start":        start,
        "end":          end,
        "s_time":       s_time,
        "e_time":       e_time,
        "dur_str":      dur_str,
        "rental_amt":   rental_amt,
        "total_amt":    rental_amt,
        "agr_no":       b.get("agreementId")      or "—",
        "lead_source":  b.get("leadSource")       or "—",
        "delivery_loc": b.get("deliveryLocation") or "—",
        "email":        b.get("clientEmail")      or "—",
        "address":      b.get("clientAddress")    or "—",
        "zero_dep":     fmt_amount(b.get("zeroDepositFee",  0)),
        "addon":        fmt_amount(b.get("addOnCharges",    0)),
        "vat":          fmt_amount(b.get("vatAmount",       0)),
        "advance":      fmt_amount(b.get("advanceReceived", 0)),
        "pay_mode":     b.get("paymentMode")      or "—",
        "agent":        b.get("salesAgent")       or "—",
        "remarks":      b.get("remarks")          or "—",
        "status":       b.get("status")           or "confirmed",
    }

# ─────────────────────────────────────────────
#  COMPACT MESSAGE BUILDERS
# ─────────────────────────────────────────────
def build_booking_card(f, now_str):
    text_body = (
        f"*🔖 AGR#:* `{f['agr_no']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*👤* {f['customer']}  |  *📱* {f['mobile']}\n"
        f"*📧* {f['email']}\n"
        f"*🏠* {f['address']}\n"
        f"*📣 Source:* {f['lead_source']}  |  *👨‍💼 Agent:* {f['agent']}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*🚘* {f['vehicle']}  |  *🔢* `{f['plate']}`\n"
        f"*📅* {fmt_date(f['start'])} {f['s_time']} → {fmt_date(f['end'])} {f['e_time']}  |  *⏱* {f['dur_str']}\n"
        f"*📍* {f['delivery_loc']}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*💰 Rental:* {f['rental_amt']}  |  *🧾 VAT:* {f['vat']}  |  *💳 Total:* {f['total_amt']}\n"
        f"*🔒 Zero Dep:* {f['zero_dep']}  |  *➕ Add-on:* {f['addon']}  |  *💵 Advance:* {f['advance']}\n"
        f"*💳 Pay Mode:* {f['pay_mode']}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*📝 Remarks:* {f['remarks']}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🚚 *Delivery:* `PENDING`  |  🔑 *Pickup:* `PENDING`\n"
        f"_All updates in this thread_"
    )

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "🚗 NEW BOOKING — MKV CAR RENTAL"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": f"Detected: {now_str}  |  Auto-alert via GitHub Actions"}]},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": text_body}},
    ]
    return blocks, f"🚗 {f['customer']} | {f['vehicle']} ({f['plate']}) | {fmt_date_full(f['start'])} → {fmt_date_full(f['end'])} | {f['total_amt']}"


def build_delivery_checklist(f, now_str):
    text_body = (
        f"*🔖 AGR#:* `{f['agr_no']}`\n"
        f"*👤* {f['customer']}  |  *📱* {f['mobile']}\n"
        f"*🚘* {f['vehicle']}  |  *🔢* `{f['plate']}`\n"
        f"*📅* {fmt_date_full(f['start'])}  {f['s_time']}\n"
        f"*📍* {f['delivery_loc']}\n"
        f"*💰* {f['total_amt']}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*✏️ Driver — copy and reply:*\n\n"
        f"OUT KM:\n"
        f"FUEL LEVEL:\n"
        f"DRIVER NAME:\n"
        f"REMARKS:\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📎 Attach: Contract PDF + Car Photos + Emirates ID"
    )

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "📦 DELIVERY CHECKLIST"}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": text_body}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": f"Posted: {now_str}  |  Status: `PENDING DELIVERY`"}]},
    ]
    return blocks, f"📦 Delivery: {f['customer']} | {f['vehicle']} ({f['plate']}) | {fmt_date_full(f['start'])} {f['s_time']}"


def build_extension_checklist(f, now_str, old_end, new_end):
    try:
        extra   = (datetime.strptime(new_end, "%Y-%m-%d") - datetime.strptime(old_end, "%Y-%m-%d")).days
        ext_str = f"+{extra}d"
    except:
        ext_str = "Extended"

    text_body = (
        f"*🔖 AGR#:* `{f['agr_no']}`\n"
        f"*👤* {f['customer']}\n"
        f"*🚘* {f['vehicle']}  |  *🔢* `{f['plate']}`\n"
        f"*📅 Prev End:* ~{fmt_date_full(old_end)}~\n"
        f"*📅 New End:* {fmt_date_full(new_end)}  ({ext_str})\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*✏️ Driver — copy and reply:*\n\n"
        f"EXTENSION AMOUNT: AED\n"
        f"PAYMENT MODE:\n"
        f"PAYMENT STATUS:\n"
        f"REMARKS:"
    )

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "🔄 CONTRACT EXTENSION"}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": text_body}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": f"Detected: {now_str}  |  Status: `EXTENDED`"}]},
    ]
    return blocks, f"🔄 Extension: {f['customer']} | {f['vehicle']} | New end: {fmt_date_full(new_end)} ({ext_str})"


def build_pickup_checklist(f, now_str):
    text_body = (
        f"*🔖 AGR#:* `{f['agr_no']}`\n"
        f"*👤* {f['customer']}  |  *📱* {f['mobile']}\n"
        f"*🚘* {f['vehicle']}  |  *🔢* `{f['plate']}`\n"
        f"*📅 Delivered:* {fmt_date_full(f['start'])}  {f['s_time']}\n"
        f"*📅 Return Due:* {fmt_date_full(f['end'])}  {f['e_time']}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*✏️ Driver — copy and reply:*\n\n"
        f"IN KM:\n"
        f"EXTRA KM:\n"
        f"FUEL CHARGE: AED\n"
        f"SALIK: AED\n"
        f"FINES: AED\n"
        f"DAMAGE CHARGES: AED\n"
        f"AMOUNT COLLECTED: AED\n"
        f"PAYMENT MODE:\n"
        f"REMARKS:"
    )

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "🔑 PICKUP CHECKLIST — DUE TOMORROW"}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": text_body}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": f"Posted: {now_str}  |  Status: `PENDING PICKUP`"}]},
    ]
    return blocks, f"🔑 Pickup: {f['customer']} | {f['vehicle']} ({f['plate']}) | Return: {fmt_date_full(f['end'])} {f['e_time']}"


def build_contract_closed(f, now_str):
    text_body = (
        f"*🔖 AGR#:* `{f['agr_no']}`\n"
        f"*👤* {f['customer']}  |  *📱* {f['mobile']}\n"
        f"*🚘* {f['vehicle']}  |  *🔢* `{f['plate']}`\n"
        f"*📅* {fmt_date_full(f['start'])} {f['s_time']} → {fmt_date_full(f['end'])} {f['e_time']}\n"
        f"*⏱ Duration:* {f['dur_str']}  |  *💰 Total:* {f['total_amt']}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ *CONTRACT CLOSED — NO FURTHER ACTION REQUIRED*"
    )

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "🏁 CONTRACT CLOSED"}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": text_body}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": f"Closed: {now_str}  |  Auto-detected from Appic"}]},
    ]
    return blocks, f"🏁 Closed: {f['customer']} | {f['vehicle']} ({f['plate']})"

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
            print(f"  NEW: {customer} | {plate} | {start}")
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
