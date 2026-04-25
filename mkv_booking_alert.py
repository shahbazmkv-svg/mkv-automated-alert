import requests
import json
import os
from datetime import datetime, timezone, timedelta

APPIC_KEY           = os.environ["APPIC_KEY"]
WEBHOOK_BOOKINGS    = os.environ["WEBHOOK_BOOKINGS"]
WEBHOOK_DELIVERY    = os.environ["WEBHOOK_DELIVERY"]

APPIC_BOOKINGS_URL  = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-bookings.php"
STORE_FILE          = "booking_alert_store.json"
DUBAI_TZ            = timezone(timedelta(hours=4))

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
    return {"seen": []}

def save_store(store):
    with open(STORE_FILE, "w") as f:
        json.dump(store, f, indent=2)

def post_slack(webhook, payload):
    try:
        r = requests.post(webhook, json=payload, timeout=10)
        ok = r.status_code == 200
        if not ok:
            print(f"  Slack HTTP {r.status_code}: {r.text[:100]}")
        return ok
    except Exception as e:
        print(f"  Slack error: {e}")
        return False

def fetch_bookings():
    now        = dubai_now()
    start_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
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
        print(f"  API fetch error: {e}")
        return []

def build_bookings_message(b, now_str):
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
        d1       = datetime.strptime(start, "%Y-%m-%d")
        d2       = datetime.strptime(end,   "%Y-%m-%d")
        duration = (d2 - d1).days
        dur_str  = f"{duration} day{'s' if duration != 1 else ''}"
    except:
        dur_str  = "N/A"
    try:
        amt_val  = float(amount)
        amt_str  = f"AED {amt_val:,.2f}" if amt_val > 0 else "Amount TBC"
    except:
        amt_str  = "Amount TBC"
    try:
        start_fmt = datetime.strptime(start, "%Y-%m-%d").strftime("%d %b %Y")
        end_fmt   = datetime.strptime(end,   "%Y-%m-%d").strftime("%d %b %Y")
    except:
        start_fmt = start
        end_fmt   = end

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
             {"type": "mrkdwn", "text": f"*📅 Start*\n{start_fmt}  {s_time}"},
             {"type": "mrkdwn", "text": f"*📅 End*\n{end_fmt}  {e_time}"},
             {"type": "mrkdwn", "text": f"*⏱ Duration*\n{dur_str}"},
             {"type": "mrkdwn", "text": f"*💰 Amount*\n{amt_str}"},
         ]},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn",
             "text": "📋 *Next Steps:* Use Slack workflow shortcuts → *VEHICLE DELIVERY* → *VEHICLE PICKUP* → *CONTRACT CLOSED*"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": "MKV Car Rental — Booking Auto-Detection  |  AGR# available after Appic API update"}]},
    ]
    return {
        "text": f"🚗 New Booking: {customer} — {vehicle} ({plate}) | {start_fmt} → {end_fmt} | {amt_str}",
        "blocks": blocks
    }

def build_delivery_alert(b, now_str):
    customer  = (b.get("customerName") or "N/A").strip().title()
    mobile    = (b.get("mobile")       or "N/A").strip()
    vehicle   = (b.get("vehicleName")  or "N/A").strip().title()
    plate     = (b.get("vehiclePlate") or "N/A").strip()
    start     = (b.get("startDate")    or "N/A").strip()
    s_time    = (b.get("startTime")    or "")[:5]
    try:
        start_fmt = datetime.strptime(start, "%Y-%m-%d").strftime("%d %b %Y")
    except:
        start_fmt = start

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "📦 DELIVERY REQUIRED — NEW BOOKING"}},
        {"type": "divider"},
        {"type": "section",
         "fields": [
             {"type": "mrkdwn", "text": f"*👤 Customer*\n{customer}"},
             {"type": "mrkdwn", "text": f"*📱 Mobile*\n{mobile}"},
             {"type": "mrkdwn", "text": f"*🚘 Vehicle*\n{vehicle}"},
             {"type": "mrkdwn", "text": f"*🔢 Plate*\n`{plate}`"},
             {"type": "mrkdwn", "text": f"*📅 Delivery Date*\n{start_fmt}"},
             {"type": "mrkdwn", "text": f"*🕐 Delivery Time*\n{s_time}"},
         ]},
        {"type": "divider"},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": "MKV Car Rental — Prepare: Contract PDF + Car Photos + Emirates ID"}]},
    ]
    return {
        "text": f"📦 Delivery: {customer} — {vehicle} ({plate}) on {start_fmt} at {s_time}",
        "blocks": blocks
    }

def main():
    now     = dubai_now()
    now_str = now.strftime("%d %b %Y | %I:%M %p Dubai Time")
    print("=" * 56)
    print("  MKV BOOKING ALERT")
    print(f"  {now_str}")
    print("=" * 56)

    store = load_store()
    seen  = set(store.get("seen", []))
    print(f"  Previously seen bookings: {len(seen)}")

    print("  Fetching bookings from Appic...")
    bookings = fetch_bookings()

    new_bookings = []
    for b in bookings:
        key = booking_key(b)
        if key and key not in seen:
            new_bookings.append(b)
            seen.add(key)

    print(f"  New bookings detected: {len(new_bookings)}")

    if not new_bookings:
        print("  No new bookings — nothing to post")
    else:
        for b in new_bookings:
            customer = (b.get("customerName") or "").strip()
            plate    = (b.get("vehiclePlate") or "").strip()
            print(f"  Posting: {customer} | {plate}")

            ok1 = post_slack(WEBHOOK_BOOKINGS, build_bookings_message(b, now_str))
            print(f"  Slack {'OK' if ok1 else 'FAILED'} -> #mkv-bookings")

            ok2 = post_slack(WEBHOOK_DELIVERY, build_delivery_alert(b, now_str))
            print(f"  Slack {'OK' if ok2 else 'FAILED'} -> #mkv-schedule-for-delivery")

    store["seen"] = list(seen)
    save_store(store)
    print("=" * 56)
    print("  Done")
    print("=" * 56)

if __name__ == "__main__":
    main()
