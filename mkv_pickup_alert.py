import requests
import json
import os
from datetime import datetime, timedelta
import pytz

# ─────────────────────────────────────────────
#  CONFIG — all values injected via GitHub Secrets
# ─────────────────────────────────────────────
APPIC_KEY         = os.environ["APPIC_KEY"]
WEBHOOK_DELIVERY  = os.environ["WEBHOOK_DELIVERY"]
WEBHOOK_PICKUP    = os.environ["WEBHOOK_PICKUP"]

APPIC_CHECKINOUT  = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-checkin-checkout.php"
APPIC_BOOKINGS    = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-bookings.php"
APPIC_VEHICLES    = "https://www.appicfleet.com/appiccar-apis-mkv/get-all-vehicles.php"

STORE_FILE        = "pickup_alert_store.json"
DUBAI_TZ          = pytz.timezone("Asia/Dubai")

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def dubai_now():
    return datetime.now(DUBAI_TZ)

def tomorrow_str():
    return (dubai_now() + timedelta(days=1)).strftime("%Y-%m-%d")

def today_str():
    return dubai_now().strftime("%Y-%m-%d")

def fetch(url, params=None):
    p = {"key": APPIC_KEY}
    if params:
        p.update(params)
    try:
        r = requests.get(url, params=p, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  ⚠ API error [{url}]: {e}")
        return []

def load_store():
    if os.path.exists(STORE_FILE):
        try:
            with open(STORE_FILE) as f:
                return json.load(f)
        except:
            pass
    return {}

def save_store(data):
    with open(STORE_FILE, "w") as f:
        json.dump(data, f, indent=2)

def post_slack(webhook, payload):
    try:
        r = requests.post(webhook, json=payload, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"  ⚠ Slack error: {e}")
        return False

# ─────────────────────────────────────────────
#  VEHICLE LOOKUP
# ─────────────────────────────────────────────
def build_vehicle_map():
    vehicles = fetch(APPIC_VEHICLES)
    vmap = {}
    if isinstance(vehicles, list):
        for v in vehicles:
            vid = str(v.get("id") or v.get("vehicle_id") or "")
            plate = v.get("plate_number") or v.get("license_plate") or "N/A"
            model = v.get("make_model") or v.get("model") or v.get("vehicle_name") or "Unknown"
            color = v.get("color") or ""
            vmap[vid] = {"plate": plate, "model": model, "color": color}
    return vmap

# ─────────────────────────────────────────────
#  FETCH TOMORROW DELIVERIES & RETURNS
# ─────────────────────────────────────────────
def get_deliveries(tomorrow, vmap):
    """Bookings with start_date = tomorrow (new deliveries)"""
    bookings = fetch(APPIC_BOOKINGS)
    results = []
    if not isinstance(bookings, list):
        return results
    for b in bookings:
        start = str(b.get("start_date") or b.get("from_date") or "")
        if start.startswith(tomorrow):
            vid   = str(b.get("vehicle_id") or "")
            cname = b.get("customer_name") or b.get("name") or "N/A"
            cphone= b.get("customer_phone") or b.get("phone") or "N/A"
            vinfo = vmap.get(vid, {})
            results.append({
                "booking_id": b.get("id") or b.get("booking_id") or "N/A",
                "customer"  : cname,
                "phone"     : cphone,
                "vehicle"   : vinfo.get("model", "N/A"),
                "plate"     : vinfo.get("plate", "N/A"),
                "color"     : vinfo.get("color", ""),
                "start_date": start[:10],
                "end_date"  : str(b.get("end_date") or b.get("to_date") or "N/A")[:10],
            })
    return results

def get_returns(tomorrow, vmap):
    """Bookings with end_date = tomorrow (returns/pickups)"""
    bookings = fetch(APPIC_BOOKINGS)
    results = []
    if not isinstance(bookings, list):
        return results
    for b in bookings:
        end = str(b.get("end_date") or b.get("to_date") or "")
        if end.startswith(tomorrow):
            vid   = str(b.get("vehicle_id") or "")
            cname = b.get("customer_name") or b.get("name") or "N/A"
            cphone= b.get("customer_phone") or b.get("phone") or "N/A"
            vinfo = vmap.get(vid, {})
            results.append({
                "booking_id": b.get("id") or b.get("booking_id") or "N/A",
                "customer"  : cname,
                "phone"     : cphone,
                "vehicle"   : vinfo.get("model", "N/A"),
                "plate"     : vinfo.get("plate", "N/A"),
                "color"     : vinfo.get("color", ""),
                "start_date": str(b.get("start_date") or b.get("from_date") or "N/A")[:10],
                "end_date"  : end[:10],
            })
    return results

# ─────────────────────────────────────────────
#  CHANGE DETECTION
# ─────────────────────────────────────────────
def has_changed(store, key, new_data):
    old = store.get(key, [])
    old_ids = set(str(x.get("booking_id")) for x in old)
    new_ids = set(str(x.get("booking_id")) for x in new_data)
    return old_ids != new_ids

# ─────────────────────────────────────────────
#  SLACK MESSAGE BUILDERS
# ─────────────────────────────────────────────
def build_delivery_message(deliveries, tomorrow):
    now_str = dubai_now().strftime("%d %b %Y | %I:%M %p")
    if not deliveries:
        return {
            "text": f"📦 *MKV DELIVERY ALERT — {tomorrow}*\n\n✅ No deliveries scheduled for tomorrow.",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn",
                    "text": f"📦 *MKV DELIVERY ALERT*\n*Date:* {tomorrow}  |  *Sent:* {now_str}"}},
                {"type": "section", "text": {"type": "mrkdwn",
                    "text": "✅ *No deliveries scheduled for tomorrow.*"}},
            ]
        }

    rows = ""
    for i, d in enumerate(deliveries, 1):
        color_str = f" ({d['color']})" if d['color'] else ""
        rows += (
            f"*{i}. {d['vehicle']}{color_str}* — `{d['plate']}`\n"
            f"   👤 {d['customer']}  |  📞 {d['phone']}\n"
            f"   📅 {d['start_date']} → {d['end_date']}  |  🔖 #{d['booking_id']}\n\n"
        )

    return {
        "text": f"📦 MKV Delivery Alert — {tomorrow} | {len(deliveries)} vehicle(s)",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text",
                "text": f"📦 MKV DELIVERY ALERT — {tomorrow}"}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": f"Sent: {now_str}  |  Vehicles to deliver: *{len(deliveries)}*"}]},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": rows.strip()}},
            {"type": "divider"},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": "MKV Car Rental — Auto Alert via GitHub Actions"}]},
        ]
    }

def build_pickup_message(returns, tomorrow):
    now_str = dubai_now().strftime("%d %b %Y | %I:%M %p")
    if not returns:
        return {
            "text": f"🔑 *MKV PICKUP ALERT — {tomorrow}*\n\n✅ No returns scheduled for tomorrow.",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn",
                    "text": f"🔑 *MKV PICKUP ALERT*\n*Date:* {tomorrow}  |  *Sent:* {now_str}"}},
                {"type": "section", "text": {"type": "mrkdwn",
                    "text": "✅ *No returns scheduled for tomorrow.*"}},
            ]
        }

    rows = ""
    for i, r in enumerate(returns, 1):
        color_str = f" ({r['color']})" if r['color'] else ""
        rows += (
            f"*{i}. {r['vehicle']}{color_str}* — `{r['plate']}`\n"
            f"   👤 {r['customer']}  |  📞 {r['phone']}\n"
            f"   📅 {r['start_date']} → {r['end_date']}  |  🔖 #{r['booking_id']}\n\n"
        )

    return {
        "text": f"🔑 MKV Pickup Alert — {tomorrow} | {len(returns)} vehicle(s)",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text",
                "text": f"🔑 MKV PICKUP ALERT — {tomorrow}"}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": f"Sent: {now_str}  |  Vehicles to collect: *{len(returns)}*"}]},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": rows.strip()}},
            {"type": "divider"},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": "MKV Car Rental — Auto Alert via GitHub Actions"}]},
        ]
    }

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    now      = dubai_now()
    tomorrow = tomorrow_str()
    today    = today_str()

    print("=" * 56)
    print(f"  MKV PICKUP ALERT (for {tomorrow})")
    print(f"  Sent at: {now.strftime('%d %b %Y | %I:%M %p')} Dubai Time")
    print("=" * 56)

    store = load_store()

    print("  Building vehicle map...")
    vmap = build_vehicle_map()
    print(f"  Vehicles loaded: {len(vmap)}")

    print("  Fetching tomorrow deliveries...")
    deliveries = get_deliveries(tomorrow, vmap)
    print(f"  Deliveries: {len(deliveries)}")

    print("  Fetching tomorrow returns...")
    returns = get_returns(tomorrow, vmap)
    print(f"  Returns: {len(returns)}")

    # ── Always post on GitHub Actions (cloud run = always fresh)
    print("  Sending to Slack...")

    delivery_msg = build_delivery_message(deliveries, tomorrow)
    ok1 = post_slack(WEBHOOK_DELIVERY, delivery_msg)
    print(f"  Slack {'OK' if ok1 else 'FAILED'} -> #mkv-schedule-for-delivery")

    pickup_msg = build_pickup_message(returns, tomorrow)
    ok2 = post_slack(WEBHOOK_PICKUP, pickup_msg)
    print(f"  Slack {'OK' if ok2 else 'FAILED'} -> #mkv-car-pickup")

    # Save state
    store[f"deliveries_{today}"] = deliveries
    store[f"returns_{today}"]    = returns
    save_store(store)

    print("=" * 56)
    print("  Done")
    print("=" * 56)

if __name__ == "__main__":
    main()
