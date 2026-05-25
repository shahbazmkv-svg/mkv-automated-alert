import requests
import json
import os
from datetime import datetime, timedelta
import pytz

# ── CONFIG ────────────────────────────────────────────────
APPIC_KEY       = os.environ["APPIC_KEY"]
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]

APPIC_BOOKINGS  = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-bookings.php"
DUBAI_TZ        = pytz.timezone("Asia/Dubai")
THREAD_STORE    = "booking_thread_store.json"
SLACK_WORKSPACE = "mkv-luxury"

# Channels
CHANNEL_BOOKINGS = "C0ABPC606F7"   # #mkv-bookings (ROOT)
CHANNEL_DELIVERY = "C0ABLDUAZ0B"   # #mkv-delivery
CHANNEL_SCHEDULE = "C0ACB9C8J01"   # #mkv-schedule-for-delivery
CHANNEL_PICKUP   = "C0ABW979FML"   # #mkv-car-pickup
CHANNEL_TEST     = "C0AVCCCG0S0"   # #mkvtest

TEST_MODE        = False

SLACK_HEADERS = {
    "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    "Content-Type":  "application/json"
}

# ── HELPERS ───────────────────────────────────────────────
def dubai_now():
    return datetime.now(DUBAI_TZ)

def tomorrow_str():
    return (dubai_now() + timedelta(days=1)).strftime("%Y-%m-%d")

def post_message(channel, blocks, text):
    if TEST_MODE:
        channel = CHANNEL_TEST
    try:
        r = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers=SLACK_HEADERS,
            json={"channel": channel, "text": text, "blocks": blocks},
            timeout=15
        )
        res = r.json()
        if not res.get("ok"):
            print(f"  Slack error: {res.get('error')}")
            return False
        return True
    except Exception as e:
        print(f"  Slack request failed: {e}")
        return False

def load_thread_store():
    if os.path.exists(THREAD_STORE):
        try:
            with open(THREAD_STORE) as f:
                data = json.load(f)
                return data.get("bookings", {})
        except:
            pass
    return {}

def get_thread_link(store, contract_id, plate, start_date, channel):
    """Build Slack thread link — delivery alert links to #mkv-bookings, pickup alert links to #mkv-delivery"""
    entry = None

    # Try by contract ID first
    if contract_id and contract_id in store:
        entry = store[contract_id]

    # Fallback: search by plate + start_date
    if not entry:
        for key, val in store.items():
            if val.get("plate") == plate and val.get("start_date") == start_date:
                entry = val
                break

    if not entry:
        return None

    # For delivery alert → link to #mkv-bookings thread
    if channel == CHANNEL_SCHEDULE:
        ts = entry.get("thread_ts")
        if ts:
            return f"https://{SLACK_WORKSPACE}.slack.com/archives/{CHANNEL_BOOKINGS}/p{ts.replace('.','')}"

    # For pickup alert → link to #mkv-delivery thread (delivery_ts)
    if channel == CHANNEL_PICKUP:
        ts = entry.get("delivery_ts") or entry.get("thread_ts")
        use_ch = CHANNEL_DELIVERY if entry.get("delivery_ts") else CHANNEL_BOOKINGS
        if ts:
            return f"https://{SLACK_WORKSPACE}.slack.com/archives/{use_ch}/p{ts.replace('.','')}"

    return None

def get_bookings():
    lookback  = (dubai_now() - timedelta(days=30)).strftime("%Y-%m-%d")
    lookahead = (dubai_now() + timedelta(days=2)).strftime("%Y-%m-%d")
    try:
        r = requests.post(
            APPIC_BOOKINGS,
            data={"key": APPIC_KEY, "startDate": lookback, "endDate": lookahead},
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
        bookings = data.get("bookings", data.get("data", []))
        print(f"  Appic returned {len(bookings)} bookings")
        return bookings
    except Exception as e:
        print(f"  Appic error: {e}")
        return []

SKIP = {"cancelled", "canceled", "voided", "void", "deleted"}

# ── DELIVERY ALERT ────────────────────────────────────────
def build_delivery_message(deliveries, tomorrow, store):
    now_str = dubai_now().strftime("%d %b %Y | %I:%M %p")
    if not deliveries:
        return [
            {"type": "header", "text": {"type": "plain_text", "text": f"🚗 DELIVERY ALERT — {tomorrow}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "No deliveries scheduled for tomorrow."}},
        ]

    rows = ""
    for i, b in enumerate(deliveries, 1):
        customer   = (b.get("customerName") or "N/A").strip().title()
        mobile     = (b.get("mobile")       or "N/A").strip()
        vehicle    = (b.get("vehicleName")  or "N/A").strip().title()
        plate      = (b.get("vehiclePlate") or "N/A").strip()
        s_time     = (b.get("startTime")    or "")[:5]
        start_date = (b.get("startDate")    or "").strip()
        end_date   = (b.get("endDate")      or "").strip()
        contract   = (b.get("contractID")   or "").strip()

        thread_link = get_thread_link(store, contract, plate, start_date, CHANNEL_SCHEDULE)
        link_text   = f"<{thread_link}|View Booking Thread>" if thread_link else "—"

        rows += (
            f"*{i}. {vehicle}* — `{plate}`\n"
            f"   Customer : {customer}  |  {mobile}\n"
            f"   Delivery : {tomorrow}  {s_time}\n"
            f"   Return   : {end_date}\n"
            f"   Thread   : {link_text}\n\n"
        )

    return [
        {"type": "header", "text": {"type": "plain_text", "text": f"🚗 DELIVERY ALERT — {tomorrow}"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"Sent: {now_str}  |  Vehicles to deliver: {len(deliveries)}"}]},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": rows.strip()}},
        {"type": "divider"},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "MKV Car Rental — Auto Alert via GitHub Actions"}]},
    ]

# ── PICKUP ALERT ──────────────────────────────────────────
def build_pickup_message(returns, tomorrow, store):
    now_str = dubai_now().strftime("%d %b %Y | %I:%M %p")
    if not returns:
        return [
            {"type": "header", "text": {"type": "plain_text", "text": f"🔑 PICKUP ALERT — {tomorrow}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "No returns scheduled for tomorrow."}},
        ]

    rows = ""
    for i, b in enumerate(returns, 1):
        customer   = (b.get("customerName") or "N/A").strip().title()
        mobile     = (b.get("mobile")       or "N/A").strip()
        vehicle    = (b.get("vehicleName")  or "N/A").strip().title()
        plate      = (b.get("vehiclePlate") or "N/A").strip()
        e_time     = (b.get("endTime")      or "")[:5]
        start_date = (b.get("startDate")    or "").strip()
        contract   = (b.get("contractID")   or "").strip()

        thread_link = get_thread_link(store, contract, plate, start_date, CHANNEL_PICKUP)
        link_text   = f"<{thread_link}|View Delivery Thread>" if thread_link else "—"

        rows += (
            f"*{i}. {vehicle}* — `{plate}`\n"
            f"   Customer : {customer}  |  {mobile}\n"
            f"   Return   : {tomorrow}  {e_time}\n"
            f"   Thread   : {link_text}\n\n"
        )

    return [
        {"type": "header", "text": {"type": "plain_text", "text": f"🔑 PICKUP ALERT — {tomorrow}"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"Sent: {now_str}  |  Vehicles to collect: {len(returns)}"}]},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": rows.strip()}},
        {"type": "divider"},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "MKV Car Rental — Auto Alert via GitHub Actions"}]},
    ]

# ── MAIN ──────────────────────────────────────────────────
def main():
    now      = dubai_now()
    tomorrow = tomorrow_str()

    print("=" * 56)
    print(f"  MKV PICKUP ALERT")
    print(f"  Tomorrow : {tomorrow}")
    print(f"  Sent at  : {now.strftime('%d %b %Y | %I:%M %p')} Dubai Time")
    print(f"  TEST_MODE: {TEST_MODE}")
    print("=" * 56)

    store = load_thread_store()
    print(f"  Threads in store: {len(store)}")

    all_bookings = get_bookings()
    SKIP_S = {"cancelled", "canceled", "voided", "void", "deleted"}

    deliveries = [b for b in all_bookings
                  if (b.get("startDate") or "").strip() == tomorrow
                  and (b.get("status") or "").lower().strip() not in SKIP_S]

    returns    = [b for b in all_bookings
                  if (b.get("endDate") or "").strip() == tomorrow
                  and (b.get("startDate") or "").strip() != tomorrow
                  and (b.get("status") or "").lower().strip() not in SKIP_S]

    print(f"  Deliveries tomorrow : {len(deliveries)}")
    print(f"  Returns tomorrow    : {len(returns)}")

    # Post delivery alert → #mkv-schedule-for-delivery
    d_blocks = build_delivery_message(deliveries, tomorrow, store)
    ok1 = post_message(CHANNEL_SCHEDULE, d_blocks,
                       f"🚗 Delivery Alert — {tomorrow} | {len(deliveries)} vehicle(s)")
    print(f"  Delivery alert {'✅ OK' if ok1 else '❌ FAILED'} → #mkv-schedule-for-delivery")

    # Post pickup alert → #mkv-car-pickup
    p_blocks = build_pickup_message(returns, tomorrow, store)
    ok2 = post_message(CHANNEL_PICKUP, p_blocks,
                       f"🔑 Pickup Alert — {tomorrow} | {len(returns)} vehicle(s)")
    print(f"  Pickup alert  {'✅ OK' if ok2 else '❌ FAILED'} → #mkv-car-pickup")

    print("=" * 56)
    print("  Done")
    print("=" * 56)

if __name__ == "__main__":
    main()
