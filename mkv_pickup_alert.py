import requests
import json
import os
from datetime import datetime, timezone, timedelta

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
APPIC_KEY        = os.environ["APPIC_KEY"]
SLACK_BOT_TOKEN  = os.environ["SLACK_BOT_TOKEN"]

APPIC_BOOKINGS   = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-bookings.php"
THREAD_STORE     = "booking_thread_store.json"
DUBAI_TZ         = timezone(timedelta(hours=4))

CHANNEL_DELIVERY = "C0ACB9C8J01"   # #mkv-schedule-for-delivery
CHANNEL_PICKUP   = "C0ABW979FML"   # #mkv-car-pickup
CHANNEL_BOOKINGS = "C0ABPC606F7"   # #mkv-bookings (for thread links)
CHANNEL_TEST     = "C0B0TGBDCDU"   # #mkvtest

TEST_MODE        = False
TARGET_DELIVERY  = CHANNEL_TEST if TEST_MODE else CHANNEL_DELIVERY
TARGET_PICKUP    = CHANNEL_TEST if TEST_MODE else CHANNEL_PICKUP
THREAD_CHANNEL   = CHANNEL_TEST if TEST_MODE else CHANNEL_BOOKINGS

SKIP_STATUSES    = {"cancelled", "canceled", "voided", "void", "deleted"}

SLACK_HEADERS    = {
    "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    "Content-Type": "application/json"
}

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def dubai_now():
    return datetime.now(DUBAI_TZ)

def tomorrow_str():
    return (dubai_now() + timedelta(days=1)).strftime("%Y-%m-%d")

def fmt_date(d):
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%d %b %Y")
    except:
        return d or "N/A"

def post_message(channel, blocks, text):
    try:
        r = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers=SLACK_HEADERS,
            json={"channel": channel, "text": text, "blocks": blocks},
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

def load_store():
    if os.path.exists(THREAD_STORE):
        try:
            with open(THREAD_STORE) as f:
                return json.load(f).get("bookings", {})
        except:
            pass
    return {}

def get_thread_link(store, contract_id, plate, start_date):
    """Return Slack deep link to the booking thread."""
    entry = store.get(contract_id)
    if not entry:
        # Fallback — search by plate + start_date
        for v in store.values():
            if isinstance(v, dict) and v.get("plate") == plate and v.get("start_date") == start_date:
                entry = v
                break
    if entry and isinstance(entry, dict):
        ts = entry.get("thread_ts")
        if ts:
            ts_clean = ts.replace(".", "")
            return f"https://mkv-luxury.slack.com/archives/{THREAD_CHANNEL}/p{ts_clean}"
    return None

def fetch_bookings(date):
    try:
        # Wide window ±30 days to catch all active contracts
        from datetime import datetime, timedelta
        dt          = datetime.strptime(date, "%Y-%m-%d")
        start_window = (dt - timedelta(days=30)).strftime("%Y-%m-%d")
        end_window   = (dt + timedelta(days=30)).strftime("%Y-%m-%d")
        r = requests.post(
            APPIC_BOOKINGS,
            data={"key": APPIC_KEY, "startDate": start_window, "endDate": end_window},
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
        if data.get("issuccess"):
            return data.get("bookings", [])
        return []
    except Exception as e:
        print(f"  API error: {e}")
        return []

# ─────────────────────────────────────────────
#  MESSAGE BUILDERS
# ─────────────────────────────────────────────

def build_delivery_alert(deliveries, tomorrow, store, now_str):
    if not deliveries:
        return [
            {"type": "header", "text": {"type": "plain_text",
                "text": f"DELIVERY ALERT — {fmt_date(tomorrow)}"}},
            {"type": "section", "text": {"type": "mrkdwn",
                "text": "No deliveries scheduled for tomorrow."}},
        ], "No deliveries tomorrow"

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
        grand_total = float(b.get("grandTotal", 0) or 0)
        amount_str  = f"AED {grand_total:,.0f}" if grand_total > 0 else "—"

        thread_link = get_thread_link(store, contract, plate, start_date)
        link_text   = f"<{thread_link}|View Thread>" if thread_link else "—"

        pickup_loc  = (b.get("pickupLocation")  or "").strip()
        dropoff_loc = (b.get("dropoffLocation") or "").strip()
        location    = pickup_loc or dropoff_loc or "—"

        rows += (
            f"*{i}. {vehicle}* — `{plate}`\n"
            f"   Customer  : {customer}  |  {mobile}\n"
            f"   Delivery  : {fmt_date(tomorrow)}  {s_time}\n"
            f"   Return    : {fmt_date(end_date)}\n"
            f"   Location  : {location}\n"
            f"   Amount    : {amount_str}\n"
            f"   Thread    : {link_text}\n\n"
        )

    blocks = [
        {"type": "header", "text": {"type": "plain_text",
            "text": f"DELIVERY ALERT — {fmt_date(tomorrow)}"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"Sent: {now_str}  |  Vehicles to deliver tomorrow: {len(deliveries)}"}]},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": rows.strip()}},
        {"type": "divider"},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "MKV Car Rental — Auto Alert via GitHub Actions"}]},
    ]
    return blocks, f"Delivery Alert — {fmt_date(tomorrow)} | {len(deliveries)} vehicle(s)"


def build_pickup_alert(returns, tomorrow, store, now_str):
    if not returns:
        return [
            {"type": "header", "text": {"type": "plain_text",
                "text": f"PICKUP ALERT — {fmt_date(tomorrow)}"}},
            {"type": "section", "text": {"type": "mrkdwn",
                "text": "No returns scheduled for tomorrow."}},
        ], "No pickups tomorrow"

    rows = ""
    for i, b in enumerate(returns, 1):
        customer   = (b.get("customerName") or "N/A").strip().title()
        mobile     = (b.get("mobile")       or "N/A").strip()
        vehicle    = (b.get("vehicleName")  or "N/A").strip().title()
        plate      = (b.get("vehiclePlate") or "N/A").strip()
        e_time     = (b.get("endTime")      or "")[:5]
        start_date = (b.get("startDate")    or "").strip()
        contract   = (b.get("contractID")   or "").strip()
        grand_total = float(b.get("grandTotal", 0) or 0)
        amount_str  = f"AED {grand_total:,.0f}" if grand_total > 0 else "—"

        thread_link = get_thread_link(store, contract, plate, start_date)
        link_text   = f"<{thread_link}|View Thread>" if thread_link else "—"

        rows += (
            f"*{i}. {vehicle}* — `{plate}`\n"
            f"   Customer  : {customer}  |  {mobile}\n"
            f"   Return    : {fmt_date(tomorrow)}  {e_time}\n"
            f"   Amount    : {amount_str}\n"
            f"   Thread    : {link_text}\n\n"
        )

    blocks = [
        {"type": "header", "text": {"type": "plain_text",
            "text": f"PICKUP ALERT — {fmt_date(tomorrow)}"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"Sent: {now_str}  |  Vehicles to collect tomorrow: {len(returns)}"}]},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": rows.strip()}},
        {"type": "divider"},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "MKV Car Rental — Auto Alert via GitHub Actions"}]},
    ]
    return blocks, f"Pickup Alert — {fmt_date(tomorrow)} | {len(returns)} vehicle(s)"


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    now      = dubai_now()
    tomorrow = tomorrow_str()
    now_str  = now.strftime("%d %b %Y | %I:%M %p Dubai Time")

    print("=" * 56)
    print(f"  MKV PICKUP & DELIVERY ALERT")
    print(f"  Alerts for: {fmt_date(tomorrow)}")
    print(f"  Sent at   : {now_str}")
    print(f"  TEST MODE : {TEST_MODE}")
    print("=" * 56)

    print("  Loading store...")
    store = load_store()
    print(f"  Store entries: {len(store)}")

    print(f"  Fetching bookings for {tomorrow}...")
    all_bookings = fetch_bookings(tomorrow)
    print(f"  Total bookings: {len(all_bookings)}")

    # Filter out cancelled/voided
    active = [b for b in all_bookings
              if (b.get("status") or "").lower() not in SKIP_STATUSES]

    deliveries = [b for b in active
                  if (b.get("startDate") or "").strip() == tomorrow]
    returns    = [b for b in active
                  if (b.get("endDate") or "").strip() == tomorrow]

    print(f"  Deliveries tomorrow: {len(deliveries)}")
    print(f"  Pickups tomorrow   : {len(returns)}")

    # ── Delivery alert → #mkv-schedule-for-delivery ──────────────────────
    d_blocks, d_text = build_delivery_alert(deliveries, tomorrow, store, now_str)
    d_ts = post_message(TARGET_DELIVERY, d_blocks, d_text)
    print(f"  Delivery alert {'posted' if d_ts else 'FAILED'} → {TARGET_DELIVERY}")

    # ── Pickup alert → #mkv-car-pickup ───────────────────────────────────
    p_blocks, p_text = build_pickup_alert(returns, tomorrow, store, now_str)
    p_ts = post_message(TARGET_PICKUP, p_blocks, p_text)
    print(f"  Pickup alert {'posted' if p_ts else 'FAILED'} → {TARGET_PICKUP}")

    print("=" * 56)
    print("  Done")
    print("=" * 56)


if __name__ == "__main__":
    main()
