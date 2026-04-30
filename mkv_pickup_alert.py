import requests
import json
import os
from datetime import datetime, timedelta
import pytz

APPIC_KEY        = os.environ["APPIC_KEY"]
SLACK_BOT_TOKEN  = os.environ.get("SLACK_BOT_TOKEN", "")

# ── Channels ──────────────────────────────────────────────────────────────────
CHANNEL_DELIVERY = "C0ABPC606F7"   # #mkv-schedule-for-delivery
CHANNEL_PICKUP   = "C0ABPC606F7"   # #mkv-car-pickup
CHANNEL_TEST     = "C0B0TGBDCDU"   # #mkvtest (new)

TEST_MODE        = True
SEND_DELIVERY_TO = CHANNEL_TEST if TEST_MODE else CHANNEL_DELIVERY
SEND_PICKUP_TO   = CHANNEL_TEST if TEST_MODE else CHANNEL_PICKUP

# ── Appic endpoints ───────────────────────────────────────────────────────────
APPIC_CHECKINOUT = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-checkin-checkout.php"
APPIC_BOOKINGS   = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-bookings.php"

DUBAI_TZ         = pytz.timezone("Asia/Dubai")
THREAD_STORE     = "booking_thread_store.json"

SLACK_WORKSPACE  = "mkv-luxury"



# ── Helpers ───────────────────────────────────────────────────────────────────

def dubai_now():
    return datetime.now(DUBAI_TZ)

def tomorrow_str():
    return (dubai_now() + timedelta(days=1)).strftime("%Y-%m-%d")

def fetch_checkinout(date, direction):
    """
    Use check-in/checkout API.
    direction=Out → deliveries (vehicles going out)
    direction=In  → pickups (vehicles coming back)
    """
    try:
        r = requests.post(APPIC_CHECKINOUT, data={
            "key":       APPIC_KEY,
            "startDate": date,
            "endDate":   date,
            "direction": direction,
        }, timeout=15)
        r.raise_for_status()
        data = r.json()
        print(f"  CheckInOut [{direction}]: issuccess={data.get('issuccess')} | records={len(data.get('bookings', []))}")
        return data.get("bookings", [])
    except Exception as e:
        print(f"  CheckInOut API error [{direction}]: {e}")
        return []

def load_store():
    if os.path.exists(THREAD_STORE):
        try:
            with open(THREAD_STORE) as f:
                return json.load(f).get("bookings", {})
        except:
            pass
    return {}

def get_thread_link(store, contract_id, plate, start_date, channel):
    """Build direct Slack thread link from booking store."""
    if contract_id and contract_id in store:
        ts = store[contract_id].get("thread_ts")
        if ts:
            return f"https://{SLACK_WORKSPACE}.slack.com/archives/{channel}/p{ts.replace('.', '')}"
    for key, val in store.items():
        if (val.get("plate", "") == plate and
            val.get("start_date", "") == start_date and
            val.get("thread_ts")):
            ts = val["thread_ts"].replace(".", "")
            return f"https://{SLACK_WORKSPACE}.slack.com/archives/{channel}/p{ts}"
    return None

def post_slack(channel, blocks, text):
    try:
        r = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                     "Content-Type": "application/json"},
            json={"channel": channel, "text": text, "blocks": blocks},
            timeout=10
        )
        result = r.json()
        if not result.get("ok"):
            print(f"  Slack error: {result.get('error')}")
            return False
        return True
    except Exception as e:
        print(f"  Slack post error: {e}")
        return False

def fmt_amount(v):
    try:
        f = float(v or 0)
        return f"AED {f:,.0f}" if f > 0 else "—"
    except:
        return "—"

# ── Message builders ──────────────────────────────────────────────────────────

def build_delivery_blocks(bookings, tomorrow, store):
    now_str = dubai_now().strftime("%d %b %Y | %I:%M %p")

    if not bookings:
        return [
            {"type": "header", "text": {"type": "plain_text", "text": f"🚗 DELIVERY ALERT — {tomorrow}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "No deliveries scheduled for tomorrow."}},
        ], "No deliveries scheduled for tomorrow."

    rows = ""
    for i, b in enumerate(bookings, 1):
        customer   = (b.get("customerName") or "N/A").strip().title()
        mobile     = (b.get("mobile")       or "N/A").strip()
        vehicle    = (b.get("vehicleName")  or "N/A").strip().title()
        plate      = (b.get("vehiclePlate") or "N/A").strip()
        s_time     = (b.get("startTime")    or "")[:5]
        start_date = (b.get("startDate")    or "").strip()
        end_date   = (b.get("endDate")      or "").strip()
        contract   = (b.get("contractID")   or "").strip()
        total      = fmt_amount(float(b.get("amount",0) or 0) +
                                float(b.get("vatAmount",0) or 0) +
                                float(b.get("zeroDepositFee",0) or 0) +
                                float(b.get("addOnCharges",0) or 0))

        thread_link = get_thread_link(store, contract, plate, start_date, SEND_DELIVERY_TO)
        link_text   = f"<{thread_link}|📎 View Booking Thread>" if thread_link else "Thread not found"

        rows += (
            f"*{i}. {vehicle}* — `{plate}`\n"
            f"   Customer  : {customer}  |  {mobile}\n"
            f"   Delivery  : {tomorrow}  {s_time}\n"
            f"   Return    : {end_date}\n"
            f"   Total     : {total}\n"
            f"   {link_text}\n\n"
        )

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🚗 DELIVERY ALERT — {tomorrow}"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"Sent: {now_str}  |  Vehicles to deliver tomorrow: *{len(bookings)}*"}]},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": rows.strip()}},
        {"type": "divider"},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "MKV Car Rental — Auto Alert via GitHub Actions"}]},
    ]
    return blocks, f"🚗 Delivery Alert — {tomorrow} | {len(bookings)} vehicle(s)"


def build_pickup_blocks(bookings, tomorrow, store):
    now_str = dubai_now().strftime("%d %b %Y | %I:%M %p")

    if not bookings:
        return [
            {"type": "header", "text": {"type": "plain_text", "text": f"🔑 PICKUP ALERT — {tomorrow}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "No returns scheduled for tomorrow."}},
        ], "No returns scheduled for tomorrow."

    rows = ""
    for i, b in enumerate(bookings, 1):
        customer   = (b.get("customerName") or "N/A").strip().title()
        mobile     = (b.get("mobile")       or "N/A").strip()
        vehicle    = (b.get("vehicleName")  or "N/A").strip().title()
        plate      = (b.get("vehiclePlate") or "N/A").strip()
        e_time     = (b.get("endTime")      or "")[:5]
        start_date = (b.get("startDate")    or "").strip()
        contract   = (b.get("contractID")   or "").strip()
        total      = fmt_amount(float(b.get("amount",0) or 0) +
                                float(b.get("vatAmount",0) or 0) +
                                float(b.get("zeroDepositFee",0) or 0) +
                                float(b.get("addOnCharges",0) or 0))

        thread_link = get_thread_link(store, contract, plate, start_date, SEND_PICKUP_TO)
        link_text   = f"<{thread_link}|📎 View Booking Thread>" if thread_link else "Thread not found"

        rows += (
            f"*{i}. {vehicle}* — `{plate}`\n"
            f"   Customer  : {customer}  |  {mobile}\n"
            f"   Return    : {tomorrow}  {e_time}\n"
            f"   Total     : {total}\n"
            f"   {link_text}\n\n"
        )

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🔑 PICKUP ALERT — {tomorrow}"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"Sent: {now_str}  |  Vehicles to collect tomorrow: *{len(bookings)}*"}]},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": rows.strip()}},
        {"type": "divider"},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "MKV Car Rental — Auto Alert via GitHub Actions"}]},
    ]
    return blocks, f"🔑 Pickup Alert — {tomorrow} | {len(bookings)} vehicle(s)"

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now      = dubai_now()
    tomorrow = tomorrow_str()

    print("=" * 56)
    print(f"  MKV PICKUP & DELIVERY ALERT (for {tomorrow})")
    print(f"  Sent at : {now.strftime('%d %b %Y | %I:%M %p')} Dubai Time")
    print(f"  TEST MODE: {TEST_MODE}")
    print("=" * 56)

    store = load_store()
    print(f"  Booking threads in store: {len(store)}")

    # Use check-in/checkout API
    print(f"\n  Fetching deliveries (direction=Out)...")
    deliveries = fetch_checkinout(tomorrow, "Out")

    print(f"  Fetching pickups (direction=In)...")
    pickups = fetch_checkinout(tomorrow, "In")

    print(f"\n  Deliveries tomorrow : {len(deliveries)}")
    print(f"  Pickups tomorrow    : {len(pickups)}")

    # Post delivery alert
    print(f"\n  Posting delivery alert → {'#mkvtest' if TEST_MODE else '#mkv-schedule-for-delivery'}...")
    d_blocks, d_text = build_delivery_blocks(deliveries, tomorrow, store)
    ok1 = post_slack(SEND_DELIVERY_TO, d_blocks, d_text)
    print(f"  Delivery alert: {'✅ OK' if ok1 else '❌ FAILED'}")

    # Post pickup alert
    print(f"  Posting pickup alert → {'#mkvtest' if TEST_MODE else '#mkv-car-pickup'}...")
    p_blocks, p_text = build_pickup_blocks(pickups, tomorrow, store)
    ok2 = post_slack(SEND_PICKUP_TO, p_blocks, p_text)
    print(f"  Pickup alert: {'✅ OK' if ok2 else '❌ FAILED'}")

    print("=" * 56)
    print("  Done")
    print("=" * 56)

if __name__ == "__main__":
    main()
