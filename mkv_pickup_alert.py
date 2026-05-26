import requests
import json
import os
from datetime import datetime, timedelta
import pytz

APPIC_KEY         = os.environ["APPIC_KEY"]
SLACK_BOT_TOKEN   = os.environ["SLACK_BOT_TOKEN"]

APPIC_BOOKINGS    = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-bookings.php"
DUBAI_TZ          = pytz.timezone("Asia/Dubai")
THREAD_STORE      = "booking_thread_store.json"

# ── Hardcoded channel IDs — never use env vars ──────────────────
SLACK_WORKSPACE        = "mkv-luxury"
CHANNEL_BOOKINGS       = "C0ABPC606F7"   # #mkv-bookings
CHANNEL_SCHEDULE       = "C0ACB9C8J01"   # #mkv-schedule-for-delivery
CHANNEL_PICKUP         = "C0ABW979FML"   # #mkv-car-pickup
CHANNEL_TEST           = "C0B0TGBDCDU"   # #mkv-test-automation

TEST_MODE              = False
THREAD_CHANNEL         = CHANNEL_TEST if TEST_MODE else CHANNEL_BOOKINGS
TARGET_SCHEDULE        = CHANNEL_TEST if TEST_MODE else CHANNEL_SCHEDULE
TARGET_PICKUP          = CHANNEL_TEST if TEST_MODE else CHANNEL_PICKUP

SLACK_HEADERS = {
    "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    "Content-Type": "application/json"
}

def dubai_now():
    return datetime.now(DUBAI_TZ)

def tomorrow_str():
    return (dubai_now() + timedelta(days=1)).strftime("%Y-%m-%d")

def fetch_post(url, data):
    try:
        r = requests.post(url, data=data, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  API error: {e}")
        return {}

def post_slack(channel, payload):
    try:
        payload["channel"] = channel
        r = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers=SLACK_HEADERS,
            json=payload,
            timeout=10
        )
        data = r.json()
        if data.get("ok"):
            return True
        else:
            print(f"  Slack error: {data.get('error')}")
            return False
    except Exception as e:
        print(f"  Slack error: {e}")
        return False

def load_thread_store():
    """Load booking thread store to get Slack thread links"""
    if os.path.exists(THREAD_STORE):
        try:
            with open(THREAD_STORE) as f:
                data = json.load(f)
                return data.get("bookings", {})
        except:
            pass
    return {}

def get_thread_link(bookings_store, contract_id, plate, start_date):
    """Find thread_ts for a booking and build Slack link"""
    # Try by contract ID first
    if contract_id and contract_id in bookings_store:
        ts = bookings_store[contract_id].get("thread_ts")
        if ts:
            ts_clean = ts.replace(".", "")
            return f"https://{SLACK_WORKSPACE}.slack.com/archives/{THREAD_CHANNEL}/p{ts_clean}"

    # Fallback: search by plate + start_date
    for key, val in bookings_store.items():
        if (val.get("plate", "") == plate and
            val.get("start_date", "") == start_date and
            val.get("thread_ts")):
            ts = val["thread_ts"].replace(".", "")
            return f"https://{SLACK_WORKSPACE}.slack.com/archives/{THREAD_CHANNEL}/p{ts}"

    return None

def get_bookings(target_date):
    now = dubai_now()
    start_window = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    end_window   = (now + timedelta(days=60)).strftime("%Y-%m-%d")
    data = fetch_post(APPIC_BOOKINGS, {
        "key":       APPIC_KEY,
        "startDate": start_window,
        "endDate":   end_window
    })
    all_bookings = data.get("bookings", []) if isinstance(data, dict) else []
    return all_bookings

def build_delivery_message(deliveries, tomorrow, store):
    now_str = dubai_now().strftime("%d %b %Y | %I:%M %p")
    if not deliveries:
        return {
            "text": f"No deliveries scheduled for {tomorrow}",
            "blocks": [
                {"type": "header",
                 "text": {"type": "plain_text", "text": f"DELIVERY ALERT — {tomorrow}"}},
                {"type": "section",
                 "text": {"type": "mrkdwn", "text": "No deliveries scheduled for tomorrow."}},
            ]
        }

    rows = ""
    for i, b in enumerate(deliveries, 1):
        customer   = (b.get("customerName") or "N/A").strip().title()
        mobile     = (b.get("mobile")       or "N/A").strip()
        vehicle    = (b.get("vehicleName")  or "N/A").strip().title()
        plate      = (b.get("vehiclePlate") or "N/A").strip()
        s_time     = (b.get("startTime")    or "")[:5]
        start_date = (b.get("startDate")    or "").strip()
        contract   = (b.get("contractID")   or "").strip()
        end_date   = (b.get("endDate")      or "").strip()

        thread_link = get_thread_link(store, contract, plate, start_date)
        link_text   = f"<{thread_link}|View Booking Thread>" if thread_link else "Thread not found"

        rows += (
            f"*{i}. {vehicle}* — `{plate}`\n"
            f"   Customer : {customer}  |  {mobile}\n"
            f"   Delivery : {tomorrow}  {s_time}\n"
            f"   Return   : {end_date}\n"
            f"   Thread   : {link_text}\n\n"
        )

    return {
        "text": f"Delivery Alert — {tomorrow} | {len(deliveries)} vehicle(s)",
        "blocks": [
            {"type": "header",
             "text": {"type": "plain_text", "text": f"DELIVERY ALERT — {tomorrow}"}},
            {"type": "context",
             "elements": [{"type": "mrkdwn",
                 "text": f"Sent: {now_str}  |  Vehicles to deliver: {len(deliveries)}"}]},
            {"type": "divider"},
            {"type": "section",
             "text": {"type": "mrkdwn", "text": rows.strip()}},
            {"type": "divider"},
            {"type": "context",
             "elements": [{"type": "mrkdwn",
                 "text": "MKV Car Rental — Auto Alert via GitHub Actions"}]},
        ]
    }

def build_pickup_message(returns, tomorrow, store):
    now_str = dubai_now().strftime("%d %b %Y | %I:%M %p")
    if not returns:
        return {
            "text": f"No returns scheduled for {tomorrow}",
            "blocks": [
                {"type": "header",
                 "text": {"type": "plain_text", "text": f"PICKUP ALERT — {tomorrow}"}},
                {"type": "section",
                 "text": {"type": "mrkdwn", "text": "No returns scheduled for tomorrow."}},
            ]
        }

    rows = ""
    for i, b in enumerate(returns, 1):
        customer   = (b.get("customerName") or "N/A").strip().title()
        mobile     = (b.get("mobile")       or "N/A").strip()
        vehicle    = (b.get("vehicleName")  or "N/A").strip().title()
        plate      = (b.get("vehiclePlate") or "N/A").strip()
        e_time     = (b.get("endTime")      or "")[:5]
        start_date = (b.get("startDate")    or "").strip()
        contract   = (b.get("contractID")   or "").strip()

        thread_link = get_thread_link(store, contract, plate, start_date)
        link_text   = f"<{thread_link}|View Booking Thread>" if thread_link else "Thread not found"

        rows += (
            f"*{i}. {vehicle}* — `{plate}`\n"
            f"   Customer : {customer}  |  {mobile}\n"
            f"   Return   : {tomorrow}  {e_time}\n"
            f"   Thread   : {link_text}\n\n"
        )

    return {
        "text": f"Pickup Alert — {tomorrow} | {len(returns)} vehicle(s)",
        "blocks": [
            {"type": "header",
             "text": {"type": "plain_text", "text": f"PICKUP ALERT — {tomorrow}"}},
            {"type": "context",
             "elements": [{"type": "mrkdwn",
                 "text": f"Sent: {now_str}  |  Vehicles to collect: {len(returns)}"}]},
            {"type": "divider"},
            {"type": "section",
             "text": {"type": "mrkdwn", "text": rows.strip()}},
            {"type": "divider"},
            {"type": "context",
             "elements": [{"type": "mrkdwn",
                 "text": "MKV Car Rental — Auto Alert via GitHub Actions"}]},
        ]
    }

def main():
    now      = dubai_now()
    tomorrow = tomorrow_str()

    print("=" * 56)
    print(f"  MKV PICKUP ALERT (for {tomorrow})")
    print(f"  Sent at: {now.strftime('%d %b %Y | %I:%M %p')} Dubai Time")
    print("=" * 56)

    print("  Loading booking thread store...")
    store = load_thread_store()
    print(f"  Threads in store: {len(store)}")

    print("  Fetching tomorrow bookings...")
    all_bookings = get_bookings(tomorrow)

    deliveries = [b for b in all_bookings if (b.get("startDate") or "").strip() == tomorrow]
    returns    = [b for b in all_bookings if (b.get("endDate")   or "").strip() == tomorrow]

    print(f"  Deliveries: {len(deliveries)}")
    print(f"  Returns: {len(returns)}")

    print("  Sending to Slack...")

    delivery_msg = build_delivery_message(deliveries, tomorrow, store)
    ok1 = post_slack(TARGET_SCHEDULE, delivery_msg)
    print(f"  Slack {'OK' if ok1 else 'FAILED'} -> #mkv-schedule-for-delivery")

    pickup_msg = build_pickup_message(returns, tomorrow, store)
    ok2 = post_slack(TARGET_PICKUP, pickup_msg)
    print(f"  Slack {'OK' if ok2 else 'FAILED'} -> #mkv-car-pickup")

    print("=" * 56)
    print("  Done")
    print("=" * 56)

if __name__ == "__main__":
    main()
