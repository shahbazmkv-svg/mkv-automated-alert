"""
MKV Luxury - Car Availability
=============================
Test/live switch:
- TEST_MODE = True  -> posts to #mkv-test-automation
- TEST_MODE = False -> posts to #team-mkv-car-availability

Source of truth:
1. Google Sheet = master fleet list, category, and status.
2. Appic vehicles API = vehicle IDs for plate matching.
3. Appic availability API = live availability for STR vehicles.
4. Appic check-in/check-out API = today's STR deliveries and returns.
"""

import csv
import io
import json
import os
import re
from datetime import datetime, timezone, timedelta

import requests


TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"

SLACK_TOKEN = os.environ["SLACK_BOT_TOKEN"]
APPIC_KEY = os.environ.get("APPIC_KEY", "")

TEST_CHANNEL = "C0B0TGBDCDU"   # #mkv-test-automation
LIVE_CHANNEL = "C0ABW8AGMRU"   # #team-mkv-car-availability
SLACK_CHANNEL = TEST_CHANNEL if TEST_MODE else LIVE_CHANNEL

DUBAI_TZ = timezone(timedelta(hours=4))
BLOCK_LIMIT = 2900

SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1GfzIz2ASBurkPW63WwscnGUsTAnVGswdX46mf5aP1cI/"
    "gviz/tq?tqx=out:csv&gid=571607065"
)

BASE_URL = "https://www.appicfleet.com/appiccar-apis-mkv"
MKV_VEHICLES_URL = f"{BASE_URL}/get-mkv-vehicles.php"
MKV_AVAIL_URL = f"{BASE_URL}/get-mkv-available-vehicle.php"
MKV_MOVEMENTS_URL = f"{BASE_URL}/get-mkv-checkin-checkout.php"

SKIP_STATUSES = {"cancelled", "canceled", "voided", "void", "deleted"}

CONTACT_FOOTER = (
    "+971 56 279 4545\n"
    "+971 4 238 8987\n"
    "https://www.mkvluxury.com/\n"
    "https://www.instagram.com/mkvluxurydubai/\n"
    "contact@mkvluxury.com\n"
    "Al Jreena Street 41, Al Qouz Industrial Third, Dubai, UAE"
)


def now_dubai():
    return datetime.now(DUBAI_TZ)


def plate_key(plate):
    return re.sub(r"\s+", "", str(plate or "").upper())


def norm(value):
    return str(value or "").strip()


def category_key(value):
    value = norm(value).upper()
    if value in {"LONG TERM", "LONGTERM", "LT", "LTR"}:
        return "LTR"
    if value in {"LEASE", "LEASING"}:
        return "LEASE"
    if value in {"SHORT TERM", "SHORTTERM", "STR"}:
        return "STR"
    if value in {"NRV", "NON RENTAL", "NON-RENTAL"}:
        return "NRV"
    return value or "-"


def status_key(value):
    return norm(value).upper() or "-"


def fmt_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d %b %Y")
    except Exception:
        return value or "-"


def fmt_time(value):
    return str(value or "")[:5] or "-"


def get_vehicle_id(vehicle):
    for key in ("vehicleID", "vehicleId", "id"):
        value = norm(vehicle.get(key))
        if value and value != "0":
            return value
    return ""


def text_to_blocks(text):
    blocks = []
    chunk = ""
    for line in text.split("\n"):
        candidate = chunk + line + "\n"
        if len(candidate) > BLOCK_LIMIT:
            if chunk.strip():
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"```{chunk.rstrip()}```"}})
            chunk = line + "\n"
        else:
            chunk = candidate
    if chunk.strip():
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"```{chunk.rstrip()}```"}})
    return blocks


def fetch_sheet_fleet():
    response = requests.get(SHEET_CSV_URL, timeout=25)
    response.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(response.text)))

    fleet = []
    for row in rows:
        plate = norm(row.get("PLATE"))
        vehicle = norm(row.get("VEHICLE"))
        if not plate or not vehicle:
            continue
        fleet.append({
            "plate": plate,
            "plate_key": plate_key(plate),
            "vehicle": vehicle.upper(),
            "category": category_key(row.get("CATEGORY")),
            "status": status_key(row.get("STATUS")),
        })

    print(f"  Sheet fleet rows: {len(fleet)}")
    return fleet


def fetch_appic_vehicle_lookup():
    response = requests.post(MKV_VEHICLES_URL, data={"key": APPIC_KEY}, timeout=25)
    response.raise_for_status()
    data = response.json()
    vehicles = data if isinstance(data, list) else data.get("data", data.get("vehicles", []))

    lookup = {}
    for vehicle in vehicles:
        plate = norm(vehicle.get("plate") or vehicle.get("vehiclePlate"))
        if plate:
            lookup[plate_key(plate)] = vehicle

    print(f"  Appic vehicle rows: {len(lookup)}")
    return lookup


def is_appic_available(vehicle_id, start_date, end_date):
    response = requests.post(
        MKV_AVAIL_URL,
        data={
            "key": APPIC_KEY,
            "startDate": start_date,
            "endDate": end_date,
            "vehicleID": vehicle_id,
        },
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    raw_status = norm(data.get("status")).lower()
    is_booked = data.get("isBooked", False)
    return (not is_booked) and ("available" in raw_status)


def fetch_available_str(fleet, appic_lookup):
    today = now_dubai().strftime("%Y-%m-%d")
    tomorrow = (now_dubai() + timedelta(days=1)).strftime("%Y-%m-%d")

    available = []
    unmatched = []
    errors = []

    for car in fleet:
        if car["category"] != "STR":
            continue
        if car["status"] == "SERVICE/GARAGE":
            continue

        appic_vehicle = appic_lookup.get(car["plate_key"])
        if not appic_vehicle:
            unmatched.append(car)
            if car["status"] == "AVAILABLE":
                available.append({**car, "source": "sheet-fallback"})
            continue

        vehicle_id = get_vehicle_id(appic_vehicle)
        if not vehicle_id:
            unmatched.append(car)
            continue

        try:
            if is_appic_available(vehicle_id, today, tomorrow):
                available.append({**car, "source": "appic"})
        except Exception as exc:
            errors.append(f"{car['plate']}: {exc}")
            if car["status"] == "AVAILABLE":
                available.append({**car, "source": "sheet-fallback"})

    print(f"  Available STR: {len(available)}")
    print(f"  Unmatched STR plates: {len(unmatched)}")
    if errors:
        print(f"  Availability API errors: {len(errors)}")
    return available, unmatched, errors


def get_records(payload):
    if isinstance(payload, list):
        return payload
    for key in ("data", "bookings", "records", "vehicles", "checkinCheckout", "checkins", "checkouts"):
        value = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(value, list):
            return value
    return []


def fetch_movements(direction):
    today = now_dubai().strftime("%Y-%m-%d")
    response = requests.post(
        MKV_MOVEMENTS_URL,
        data={
            "key": APPIC_KEY,
            "startDate": today,
            "endDate": today,
            "direction": direction,
        },
        timeout=25,
    )
    response.raise_for_status()
    data = response.json()
    records = get_records(data)
    print(f"  Movement {direction}: {len(records)}")
    return records


def fetch_today_movements():
    deliveries = fetch_movements("Out")
    returns = fetch_movements("In")
    return deliveries, returns


def filter_str_movements(bookings, str_plate_keys):
    return [
        booking for booking in bookings
        if plate_key(booking.get("vehiclePlate")) in str_plate_keys
    ]


def fmt_movement_lines(bookings, time_key):
    if not bookings:
        return "None scheduled."

    lines = []
    for index, booking in enumerate(bookings, 1):
        vehicle = norm(booking.get("vehicleName") or booking.get("vehicle_name") or "N/A").title()
        plate = norm(booking.get("vehiclePlate") or "N/A")
        customer = norm(booking.get("customerName") or booking.get("customer_name") or "N/A").title()
        mobile = norm(booking.get("mobile"))
        contract = norm(booking.get("contractID") or booking.get("contract_id"))
        time_value = fmt_time(booking.get(time_key))
        lines.append(
            f"{index}. {vehicle} [{plate}]\n"
            f"   Customer : {customer} {mobile}\n"
            f"   Time     : {time_value}\n"
            f"   AGR      : {contract or '-'}"
        )
    return "\n\n".join(lines)


def build_message(fleet, available_str, deliveries, returns, unmatched):
    now = now_dubai()
    date_str = now.strftime("%B %d, %Y").upper()
    day_str = now.strftime("%A").upper()

    total = len(fleet)
    lease_count = sum(1 for car in fleet if car["category"] == "LEASE")
    ltr_count = sum(1 for car in fleet if car["category"] == "LTR")
    nrv_count = sum(1 for car in fleet if car["category"] == "NRV")
    str_total = sum(1 for car in fleet if car["category"] == "STR")
    fleet_available = sum(1 for car in fleet if car["status"] == "AVAILABLE")
    fleet_rented = sum(1 for car in fleet if car["status"] == "RENTED")
    service_count = sum(1 for car in fleet if car["status"] == "SERVICE/GARAGE")
    available_count = len(available_str)

    summary = "\n".join([
        f"{date_str} {day_str}",
        "",
        f"Total Fleet       : {total}",
        f"STR               : {str_total}",
        f"Lease             : {lease_count}",
        f"Long Term         : {ltr_count}",
        f"NRV               : {nrv_count}",
        "",
        f"Fleet Available   : {fleet_available}",
        f"Fleet Rented      : {fleet_rented}",
        f"Service/Garage    : {service_count}",
        "",
        f"Live STR Available: {available_count}",
    ])

    available_lines = ["AVAILABLE SHORT TERM", "-" * 30]
    if available_str:
        for index, car in enumerate(sorted(available_str, key=lambda item: item["vehicle"]), 1):
            available_lines.append(f"{index}. {car['vehicle']} [{car['plate']}]")
    else:
        available_lines.append("None available.")

    delivery_text = fmt_movement_lines(deliveries, "startTime")
    return_text = fmt_movement_lines(returns, "endTime")

    blocks = []
    blocks += text_to_blocks(summary)
    blocks.append({"type": "divider"})
    blocks += text_to_blocks("\n".join(available_lines))
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*DELIVERY TODAY - {len(deliveries)} vehicle(s)*\n\n```{delivery_text}```"},
    })
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*RETURN TODAY - {len(returns)} vehicle(s)*\n\n```{return_text}```"},
    })
    blocks.append({"type": "divider"})
    blocks += text_to_blocks(CONTACT_FOOTER)

    if unmatched:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"Note: {len(unmatched)} STR plate(s) were not matched in Appic vehicles API; sheet status was used where possible."}]})

    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": "MKV Active Fleet - Google Sheet master + Appic STR availability + check-in/out movement"}]})

    return {"blocks": blocks, "text": f"MKV Car Availability - {date_str} {day_str}"}


def post_slack(message):
    response = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"},
        data=json.dumps({
            "channel": SLACK_CHANNEL,
            "username": "MKV Car Availability",
            "icon_emoji": ":car:",
            "unfurl_links": False,
            "unfurl_media": False,
            **message,
        }),
        timeout=20,
    )
    data = response.json()
    if not data.get("ok"):
        print(f"  Slack error: {data.get('error')}")
        raise SystemExit(1)
    print(f"  Posted to Slack successfully: {SLACK_CHANNEL}")


def main():
    print("=" * 55)
    print("  MKV CAR AVAILABILITY")
    print(f"  {now_dubai().strftime('%d %b %Y | %I:%M %p Dubai Time')}")
    print(f"  TEST_MODE: {TEST_MODE}")
    print("=" * 55)

    print("\n[1] Fetching Google Sheet master fleet ...")
    fleet = fetch_sheet_fleet()

    print("\n[2] Fetching Appic vehicles ...")
    appic_lookup = fetch_appic_vehicle_lookup()

    print("\n[3] Checking STR availability ...")
    available_str, unmatched, _errors = fetch_available_str(fleet, appic_lookup)

    print("\n[4] Fetching today's STR deliveries and returns ...")
    deliveries, returns = fetch_today_movements()
    str_plate_keys = {car["plate_key"] for car in fleet if car["category"] == "STR"}
    deliveries = filter_str_movements(deliveries, str_plate_keys)
    returns = filter_str_movements(returns, str_plate_keys)
    print(f"  STR deliveries today: {len(deliveries)}")
    print(f"  STR returns today: {len(returns)}")

    print("\n[5] Building Slack message ...")
    message = build_message(fleet, available_str, deliveries, returns, unmatched)

    print("\n[6] Posting to Slack ...")
    post_slack(message)


if __name__ == "__main__":
    main()
