import requests
import json
import os
from datetime import datetime, timezone, timedelta

APPIC_KEY          = os.environ["APPIC_KEY"]
SLACK_BOT_TOKEN    = os.environ["SLACK_BOT_TOKEN"]

CHANNEL_BOOKINGS   = "C0ABPC606F7"
CHANNEL_TEST       = "C0AVCCCG0S0"

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

def fmt_date(d):
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%d %b %Y")
    except:
        return d

def build_new_booking_blocks(b, now_str):
    customer  = (b.get("customerName") or "N/A").strip().title()
    mobile    = (b.get("mobile")       or "N/A").strip()
    vehicle   = (b.get("vehicleName")  or "N/A").strip().title()
    plate     = (b.get("vehiclePlate") or "N/A").strip()
    start     = (b.get("startDate")    or "N/A").strip()
    end       = (b.get("endDate")      or "N/A").strip()
    s_time    = (b.get("startTime")    or "")[:5]
    e_time    = (b.get("endTime")      or "")[:5]
    amount    = b.get("amount", 0)
    try:
        dur = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days
        dur_str = f"{dur} day{'s' if dur != 1 else ''}"
    except:
        dur_str = "N/A"
    try:
        amt_val = float(amount)
        amt_str = f"AED {amt_val:,.2f}" if amt_val > 0 else "Amount TBC"
    except:
        amt_str = "Amount TBC"

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "🚗 NEW BOOKING — MKV CAR RENTAL"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": f"Detected: {now_str}  |  Auto-alert via GitHub Actions"}]},
        {"type": "divider"},
        {"type": "section",
         "fields": [
             {"type": "mrkdwn", "text": f"*👤 Customer*\n{customer}"},
             {"type": "mrkdwn", "text": f"*📱 Mobile*\n{mobile}"},
             {"type": "mrkdwn", "text": f"*🚘 Vehicle*\n{vehicle}"},
             {"type": "mrkdwn", "text": f"*🔢 Plate*\n`{plate}`"},
             {"type": "mrkdwn", "text": f"*📅 Start*\n{fmt_date(start)}  {s_time}"},
             {"type": "mrkdwn", "text": f"*📅 End*\n{fmt_date(end)}  {e_time}"},
             {"type": "mrkdwn", "text": f"*⏱ Duration*\n{dur_str}"},
             {"type": "mrkdwn", "text": f"*💰 Amount*\n{amt_str}"},
         ]},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn",
             "text": "📋 *Status:* `BOOKING CONFIRMED` — Awaiting Delivery\n_All updates will appear in this thread_"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": "MKV Car Rental  |  AGR# available after Appic API update"}]},
    ]
    return blocks, f"🚗 New Booking: {customer} — {vehicle} ({plate}) | {fmt_date(start)} → {fmt_date(end)} | {amt_str}"

def build_delivery_checklist_blocks(b, now_str):
    customer  = (b.get("customerName") or "N/A").strip().title()
    mobile    = (b.get("mobile")       or "N/A").strip()
    vehicle   = (b.get("vehicleName")  or "N/A").strip().title()
    plate     = (b.get("vehiclePlate") or "N/A").strip()
    start     = (b.get("startDate")    or "N/A").strip()
    s_time    = (b.get("startTime")    or "")[:5]
    amount    = b.get("amount", 0)
    try:
        amt_str = f"AED {float(amount):,.2f}" if float(amount) > 0 else "Amount TBC"
    except:
        amt_str = "Amount TBC"

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "📦 DELIVERY CHECKLIST"}},
        {"type": "divider"},
        {"type": "section",
         "fields": [
             {"type": "mrkdwn", "text": f"*👤 Customer*\n{customer}"},
             {"type": "mrkdwn", "text": f"*📱 Mobile*\n{mobile}"},
             {"type": "mrkdwn", "text": f"*🚘 Vehicle*\n{vehicle}"},
             {"type": "mrkdwn", "text": f"*🔢 Plate*\n`{plate}`"},
             {"type": "mrkdwn", "text": f"*📅 Delivery Date*\n{fmt_date(start)}"},
             {"type": "mrkdwn", "text": f"*🕐 Delivery Time*\n{s_time}"},
             {"type": "mrkdwn", "text": f"*💰 Amount*\n{amt_str}"},
         ]},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn",
             "text": (
                 "*✏️ Driver — reply in this thread with:*\n\n"
                 "```\n"
                 "OUT KM      : ___\n"
                 "FUEL LEVEL  : ___  (e.g. 50% / Full / 3/4)\n"
                 "DRIVER NAME : ___\n"
                 "REMARKS     : ___\n"
                 "```"
             )}},
        {"type": "section",
         "text": {"type": "mrkdwn",
             "text": "📎 *Also attach:* Contract PDF + Car Photos + Emirates ID copy"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": f"Checklist posted: {now_str}  |  Status: `PENDING DELIVERY`"}]},
    ]
    return blocks, f"📦 Delivery Checklist: {customer} — {vehicle} ({plate}) on {fmt_date(start)} at {s_time}"

def build_extension_blocks(b, now_str, old_end, new_end):
    customer = (b.get("customerName") or "N/A").strip().title()
    vehicle  = (b.get("vehicleName")  or "N/A").strip().title()
    plate    = (b.get("vehiclePlate") or "N/A").strip()
    try:
        extra   = (datetime.strptime(new_end, "%Y-%m-%d") - datetime.strptime(old_end, "%Y-%m-%d")).days
        ext_str = f"+{extra} day{'s' if extra != 1 else ''}"
    except:
        ext_str = "Extended"

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "🔄 CONTRACT EXTENDED"}},
        {"type": "section",
         "fields": [
             {"type": "mrkdwn", "text": f"*👤 Customer*\n{customer}"},
             {"type": "mrkdwn", "text": f"*🚘 Vehicle*\n`{plate}` {vehicle}"},
             {"type": "mrkdwn", "text": f"*📅 Previous End*\n~{fmt_date(old_end)}~"},
             {"type": "mrkdwn", "text": f"*📅 New End Date*\n{fmt_date(new_end)}  ({ext_str})"},
         ]},
        {"type": "section",
         "text": {"type": "mrkdwn",
             "text": "📋 *Status:* `EXTENDED` — Pickup date updated\n_Pickup checklist will be reposted closer to new end date_"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn", "text": f"Extension detected: {now_str}"}]},
    ]
    return blocks, f"🔄 Extension: {customer} — {vehicle} | New end: {fmt_date(new_end)} ({ext_str})"

def build_pickup_checklist_blocks(b, now_str):
    customer  = (b.get("customerName") or "N/A").strip().title()
    mobile    = (b.get("mobile")       or "N/A").strip()
    vehicle   = (b.get("vehicleName")  or "N/A").strip().title()
    plate     = (b.get("vehiclePlate") or "N/A").strip()
    end       = (b.get("endDate")      or "N/A").strip()
    e_time    = (b.get("endTime")      or "")[:5]
    start     = (b.get("startDate")    or "N/A").strip()
    s_time    = (b.get("startTime")    or "")[:5]

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "🔑 PICKUP CHECKLIST — DUE TOMORROW"}},
        {"type": "divider"},
        {"type": "section",
         "fields": [
             {"type": "mrkdwn", "text": f"*👤 Customer*\n{customer}"},
             {"type": "mrkdwn", "text": f"*📱 Mobile*\n{mobile}"},
             {"type": "mrkdwn", "text": f"*🚘 Vehicle*\n{vehicle}"},
             {"type": "mrkdwn", "text": f"*🔢 Plate*\n`{plate}`"},
             {"type": "mrkdwn", "text": f"*📅 Delivery Was*\n{fmt_date(start)}  {s_time}"},
             {"type": "mrkdwn", "text": f"*📅 Return Due*\n{fmt_date(end)}  {e_time}"},
         ]},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn",
             "text": (
                 "*✏️ Driver — reply in this thread with:*\n\n"
                 "```\n"
                 "IN KM            : ___\n"
                 "EXTRA KM         : ___  (auto if known)\n"
                 "FUEL CHARGE      : ___  AED (0 if full)\n"
                 "SALIK            : ___  AED\n"
                 "FINES            : ___  AED\n"
                 "DAMAGE           : ___  (None / describe)\n"
                 "AMOUNT COLLECTED : ___  AED\n"
                 "PAYMENT MODE     : ___  (Cash / Card / Transfer)\n"
                 "REMARKS          : ___\n"
                 "```"
             )}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": f"Checklist posted: {now_str}  |  Status: `PENDING PICKUP`"}]},
    ]
    return blocks, f"🔑 Pickup Checklist: {customer} — {vehicle} ({plate}) | Return: {fmt_date(end)} at {e_time}"

def main():
    now      = dubai_now()
    now_str  = now.strftime("%d %b %Y | %I:%M %p Dubai Time")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    # ── SEED MODE ──────────────────────────────
    # True  = first run, stores all silently
    # False = normal, posts new bookings
    SEED_MODE = False
    # ───────────────────────────────────────────

    print("=" * 56)
    print("  MKV BOOKING BOT")
    print(f"  {now_str}")
    print(f"  SEED MODE: {SEED_MODE}")
    print("=" * 56)

    store    = load_store()
    bookings = store.get("bookings", {})

    print("  Fetching bookings from Appic...")
    all_bookings = fetch_bookings()

    for b in all_bookings:
        key      = booking_key(b)
        if not key:
            continue

        plate    = (b.get("vehiclePlate") or "").strip()
        start    = (b.get("startDate")    or "").strip()
        end      = (b.get("endDate")      or "").strip()
        customer = (b.get("customerName") or "").strip()

        if SEED_MODE:
            if key not in bookings:
                bookings[key] = {
                    "thread_ts":        None,
                    "end_date":         end,
                    "plate":            plate,
                    "customer":         customer,
                    "vehicle":          (b.get("vehicleName") or "").strip(),
                    "delivery_alerted": True,
                    "pickup_alerted":   False,
                    "start_date":       start,
                }
            continue

        if key not in bookings:
            print(f"  NEW: {customer} | {plate} | {start}")

            # Post booking card
            blocks, text = build_new_booking_blocks(b, now_str)
            ts = post_message(TARGET_CHANNEL, blocks, text)
            if ts:
                bookings[key] = {
                    "thread_ts":        ts,
                    "end_date":         end,
                    "plate":            plate,
                    "customer":         customer,
                    "vehicle":          (b.get("vehicleName") or "").strip(),
                    "delivery_alerted": False,
                    "pickup_alerted":   False,
                    "start_date":       start,
                }
                print(f"  Booking posted — thread: {ts}")

                # Post delivery checklist in thread immediately
                d_blocks, d_text = build_delivery_checklist_blocks(b, now_str)
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

            # Extension detected
            if end and old_end and end != old_end and end > old_end:
                print(f"  EXTENSION: {customer} | {plate} | {old_end} -> {end}")
                if thread_ts:
                    e_blocks, e_text = build_extension_blocks(b, now_str, old_end, end)
                    e_ts = post_message(TARGET_CHANNEL, e_blocks, e_text, thread_ts=thread_ts)
                    if e_ts:
                        bookings[key]["end_date"]       = end
                        bookings[key]["pickup_alerted"] = False
                        print(f"  Extension posted in thread")
                else:
                    bookings[key]["end_date"] = end

            # Pickup checklist — day before end date
            if (end == tomorrow and
                not stored.get("pickup_alerted") and
                thread_ts):
                print(f"  PICKUP CHECKLIST: {customer} | {plate} | due {end}")
                p_blocks, p_text = build_pickup_checklist_blocks(b, now_str)
                p_ts = post_message(TARGET_CHANNEL, p_blocks, p_text, thread_ts=thread_ts)
                if p_ts:
                    bookings[key]["pickup_alerted"] = True
                    print(f"  Pickup checklist posted in thread")

    store["bookings"] = bookings
    save_store(store)

    if SEED_MODE:
        print(f"  SEED MODE ON — {len(bookings)} bookings stored silently")

    print("=" * 56)
    print("  Done")
    print("=" * 56)

if __name__ == "__main__":
    main()
