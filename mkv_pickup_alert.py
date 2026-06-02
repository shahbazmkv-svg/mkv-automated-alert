import json
import os
from datetime import datetime, timedelta

import pytz
import requests


APPIC_KEY = os.environ["APPIC_KEY"]
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]

APPIC_BOOKINGS = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-bookings.php"
THREAD_STORE = "booking_thread_store.json"
DUBAI_TZ = pytz.timezone("Asia/Dubai")

CHANNEL_BOOKINGS = "C0ABPC606F7"   # #mkv-bookings
CHANNEL_DELIVERY = "C0ABLDUAZ0B"   # #mkv-delivery
CHANNEL_SCHEDULE = "C0ACB9C8J01"   # #mkv-schedule-for-delivery
CHANNEL_PICKUP = "C0ABW979FML"     # #mkv-car-pickup
CHANNEL_TEST = "C0B0TGBDCDU"       # #mkv-test-automation

TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"

SLACK_HEADERS = {
    "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    "Content-Type": "application/json",
}

SKIP_STATUSES = {"cancelled", "canceled", "voided", "void", "deleted"}


def dubai_now():
    return datetime.now(DUBAI_TZ)


def tomorrow_str():
    return (dubai_now() + timedelta(days=1)).strftime("%Y-%m-%d")


def fmt_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d %b %Y")
    except Exception:
        return value or "-"


def post_message(channel, blocks, text):
    if TEST_MODE:
        channel = CHANNEL_TEST
    try:
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers=SLACK_HEADERS,
            json={
                "channel": channel,
                "text": text,
                "blocks": blocks,
                "unfurl_links": False,
                "unfurl_media": False,
            },
            timeout=15,
        )
        data = response.json()
        if not data.get("ok"):
            print(f"  Slack error: {data.get('error')}")
            return False
        return True
    except Exception as exc:
        print(f"  Slack request failed: {exc}")
        return False


def load_thread_store():
    if not os.path.exists(THREAD_STORE):
        return {}
    try:
        with open(THREAD_STORE, encoding="utf-8") as file:
            data = json.load(file)
        return data.get("bookings", {})
    except Exception as exc:
        print(f"  Store read failed: {exc}")
        return {}


def find_store_entry(store, contract_id, plate, start_date):
    if contract_id and contract_id in store:
        return store[contract_id]

    for _key, value in store.items():
        if not isinstance(value, dict):
            continue
        if value.get("plate") == plate and value.get("start_date") == start_date:
            return value
    return {}


def app_redirect(channel, ts):
    if not channel or not ts:
        return ""
    clean_ts = str(ts).replace(".", "")
    return f"https://mkv-global.slack.com/archives/{channel}/p{clean_ts}"

def booking_thread_link(entry):
    return app_redirect(CHANNEL_BOOKINGS, entry.get("thread_ts"))


def delivery_thread_link(entry):
    return app_redirect(CHANNEL_DELIVERY, entry.get("delivery_ts")) or booking_thread_link(entry)


def booking_data_from_appic(booking):
    vehicle = (booking.get("vehicleName") or "-").strip().title()
    plate = (booking.get("vehiclePlate") or "-").strip()
    return {
        "id": (booking.get("contractID") or "-").strip(),
        "car": f"{vehicle} [{plate}]",
        "date": fmt_date((booking.get("startDate") or "").strip()),
        "time": (booking.get("startTime") or "")[:5],
        "location": (
            booking.get("deliveryLocation")
            or booking.get("pickupLocation")
            or booking.get("dropoffLocation")
            or booking.get("address")
            or "-"
        ),
        "driver": "",
        "out_km": "",
    }


def get_bookings():
    lookback = (dubai_now() - timedelta(days=30)).strftime("%Y-%m-%d")
    lookahead = (dubai_now() + timedelta(days=2)).strftime("%Y-%m-%d")
    try:
        response = requests.post(
            APPIC_BOOKINGS,
            data={"key": APPIC_KEY, "startDate": lookback, "endDate": lookahead},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        bookings = data.get("bookings", data.get("data", []))
        print(f"  Appic returned {len(bookings)} bookings")
        return bookings
    except Exception as exc:
        print(f"  Appic fetch failed: {exc}")
        return []


def build_delivery_message(deliveries, target_date, store):
    now_str = dubai_now().strftime("%d %b %Y | %I:%M %p")
    if not deliveries:
        return [
            {"type": "header", "text": {"type": "plain_text", "text": f"DELIVERY ALERT - {target_date}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "No deliveries scheduled for tomorrow."}},
        ]

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"DELIVERY ALERT - {target_date}"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"Sent: {now_str} | Vehicles to deliver: {len(deliveries)}"}]},
        {"type": "divider"},
    ]

    for index, booking in enumerate(deliveries, 1):
        customer = (booking.get("customerName") or "-").strip().title()
        mobile = (booking.get("mobile") or "-").strip()
        vehicle = (booking.get("vehicleName") or "-").strip().title()
        plate = (booking.get("vehiclePlate") or "-").strip()
        start_time = (booking.get("startTime") or "")[:5]
        start_date = (booking.get("startDate") or "").strip()
        contract = (booking.get("contractID") or "").strip()
        entry = find_store_entry(store, contract, plate, start_date)
        link = booking_thread_link(entry)
        link_text = f"<{link}|Open booking thread>" if link else "-"

        text = (
            f"*{index}. {vehicle}* `{plate}`\n"
            f"Customer: {customer} | {mobile}\n"
            f"Delivery: {fmt_date(target_date)} {start_time}\n"
            f"Thread: {link_text}"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
        blocks.append({"type": "actions", "elements": [{
            "type": "button",
            "text": {"type": "plain_text", "text": "Delivery"},
            "style": "primary",
            "action_id": "open_delivery",
            "value": json.dumps(booking_data_from_appic(booking)),
        }]})

    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": "Brief delivery list only. Full details and documents stay in #mkv-bookings."}]})
    return blocks


def build_pickup_message(returns, target_date, store):
    now_str = dubai_now().strftime("%d %b %Y | %I:%M %p")
    if not returns:
        return [
            {"type": "header", "text": {"type": "plain_text", "text": f"PICKUP ALERT - {target_date}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "No returns scheduled for tomorrow."}},
        ]

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"PICKUP ALERT - {target_date}"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"Sent: {now_str} | Vehicles to collect: {len(returns)}"}]},
        {"type": "divider"},
    ]

    for index, booking in enumerate(returns, 1):
        customer = (booking.get("customerName") or "-").strip().title()
        mobile = (booking.get("mobile") or "-").strip()
        vehicle = (booking.get("vehicleName") or "-").strip().title()
        plate = (booking.get("vehiclePlate") or "-").strip()
        end_time = (booking.get("endTime") or "")[:5]
        start_date = (booking.get("startDate") or "").strip()
        contract = (booking.get("contractID") or "").strip()
        entry = find_store_entry(store, contract, plate, start_date)
        link = delivery_thread_link(entry)
        link_text = f"<{link}|Open delivery thread>" if link else "-"

        text = (
            f"*{index}. {vehicle}* `{plate}`\n"
            f"Customer: {customer} | {mobile}\n"
            f"Pickup: {fmt_date(target_date)} {end_time}\n"
            f"Thread: {link_text}"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
        blocks.append({"type": "actions", "elements": [{
            "type": "button",
            "text": {"type": "plain_text", "text": "Pickup"},
            "style": "primary",
            "action_id": "open_pickup",
            "value": json.dumps(booking_data_from_appic(booking)),
        }]})

    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": "Brief pickup list only. Full details and documents stay in #mkv-bookings."}]})
    return blocks


def main():
    now = dubai_now()
    target_date = tomorrow_str()

    print("=" * 56)
    print("  MKV PICKUP / DELIVERY ALERT")
    print(f"  Tomorrow : {target_date}")
    print(f"  Sent at  : {now.strftime('%d %b %Y | %I:%M %p')} Dubai Time")
    print(f"  TEST_MODE: {TEST_MODE}")
    print("=" * 56)

    store = load_thread_store()
    all_bookings = get_bookings()

    deliveries = [
        booking for booking in all_bookings
        if (booking.get("startDate") or "").strip() == target_date
        and (booking.get("status") or "").lower().strip() not in SKIP_STATUSES
    ]
    returns = [
        booking for booking in all_bookings
        if (booking.get("endDate") or "").strip() == target_date
        and (booking.get("startDate") or "").strip() != target_date
        and (booking.get("status") or "").lower().strip() not in SKIP_STATUSES
    ]

    print(f"  Deliveries tomorrow : {len(deliveries)}")
    print(f"  Returns tomorrow    : {len(returns)}")

    delivery_blocks = build_delivery_message(deliveries, target_date, store)
    ok_delivery = post_message(
        CHANNEL_SCHEDULE,
        delivery_blocks,
        f"Delivery Alert - {target_date} | {len(deliveries)} vehicle(s)",
    )
    print(f"  Delivery alert {'OK' if ok_delivery else 'FAILED'} -> #mkv-schedule-for-delivery")

    pickup_blocks = build_pickup_message(returns, target_date, store)
    ok_pickup = post_message(
        CHANNEL_PICKUP,
        pickup_blocks,
        f"Pickup Alert - {target_date} | {len(returns)} vehicle(s)",
    )
    print(f"  Pickup alert {'OK' if ok_pickup else 'FAILED'} -> #mkv-car-pickup")

    print("=" * 56)
    print("  Done")


if __name__ == "__main__":
    main()
