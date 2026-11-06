"""
MKV Luxury - Car Availability
=============================
Test/live switch (environment variable, set by the GitHub workflow):
- TEST_MODE=true  -> posts to #mkv-test-automation
- TEST_MODE=false -> posts to #team-mkv-car-availability (default)
- Run with --dry-run (or DRY_RUN=1) to print the message without posting.

Sources
-------
1. Google Sheet (Drive) = master fleet list: PLATE, VEHICLE, CATEGORY, STATUS.
   - CATEGORY decides the bucket: STR / LEASE / LTR / NRV.
   - STATUS "SERVICE/GARAGE" moves the car into the Garage bucket.
   - Only rows ABOVE the colour legend ("- IDLE", "- RENTED", ...) are the
     active fleet. Rows below the legend, and rows with any other category
     (Sold, Transfer RTO, ...), are shown as "Not active" and not counted.
     Total = STR + Lease + LTR + Garage + NRV, always.
2. Appic bookings API = agreement status for every contract:
   confirmed = agreement opened (car out), closed = returned, draft = not opened.
3. Appic check-in/check-out API = full plate per contract number. The bookings
   API often drops the plate letter ("66789" could be O66789 or S66789).

Rules
-----
- Car out   = a confirmed agreement that has started and has not been superseded
              by a later agreement on the same car (Appic leaves some old
              agreements "confirmed" after the car came back and was re-rented).
- Overdue   = car out whose agreement end date is before today.
- Draft running = no opened agreement, but a draft covers right now -> the car
              is treated as not available and flagged for checking.
- An STR car marked SERVICE/GARAGE in the sheet but out on a current (not
  overdue) Appic agreement is counted as STR on rent (Appic wins) and flagged.
  With an overdue agreement it stays in Garage and is flagged.
- Delivery today = every agreement starting today (opened, closed or draft).
- Return today   = every agreement ending today (returned or still out).
"""

import csv
import io
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone

import requests


TEST_MODE = os.environ.get("TEST_MODE", "false").strip().lower() == "true"
DRY_RUN = "--dry-run" in sys.argv or os.environ.get("DRY_RUN") == "1"

# STR car is SERVICE/GARAGE in the sheet but Appic has an opened agreement:
# True -> count it as STR on rent (car is with a customer per Appic).
STR_OPEN_AGREEMENT_BEATS_GARAGE = True


def _load_local_env():
    """Read .env next to this file for local runs. GitHub Actions uses secrets."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()

SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
APPIC_KEY = os.environ.get("APPIC_KEY") or os.environ.get("APPIC_API_KEY", "")

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
BOOKINGS_URL = f"{BASE_URL}/get-mkv-bookings.php"
MOVEMENTS_URL = f"{BASE_URL}/get-mkv-checkin-checkout.php"

HISTORY_START = "2018-01-01"   # long leases started years ago; fetch everything
FUTURE_DAYS = 120

CATEGORY_MAP = {
    "STR": "STR", "SHORT TERM": "STR", "SHORTTERM": "STR", "SHORT TERM RENTAL": "STR",
    "LEASE": "LEASE", "LEASING": "LEASE",
    "LTR": "LTR", "LT": "LTR", "LONG TERM": "LTR", "LONGTERM": "LTR", "LONG TERM RENTAL": "LTR",
    "NRV": "NRV", "NON RENTAL": "NRV", "NON-RENTAL": "NRV",
}
GARAGE_STATUSES = {"SERVICE/GARAGE", "GARAGE", "SERVICE", "WORKSHOP", "GARAGE/SERVICE"}
BUCKET_ORDER = ["STR", "LEASE", "LTR", "GARAGE", "NRV"]
BUCKET_LABEL = {"STR": "STR", "LEASE": "Lease", "LTR": "LTR", "GARAGE": "Garage", "NRV": "NRV"}

STATE_LABEL = {
    "ON_RENT": "on rent",
    "OVERDUE": "overdue",
    "DRAFT_RUNNING": "draft running (agreement not opened)",
    "FREE": "no open agreement",
}

CONTACT_FOOTER = (
    "+971 56 279 4545\n"
    "+971 4 221 3816\n"
    "https://www.mkvluxury.com/\n"
    "https://www.instagram.com/mkvluxurydubai/\n"
    "contact@mkvluxury.com\n"
    "Al Jreena Street 41, Al Qouz Industrial Third, Dubai, UAE"
)


# ---------------------------------------------------------------- helpers

def now_dubai():
    return datetime.now(DUBAI_TZ)


def norm(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def plate_key(plate):
    return re.sub(r"[^A-Z0-9]", "", str(plate or "").upper())


def plate_digits(plate):
    return re.sub(r"\D", "", str(plate or ""))


def category_key(value):
    return CATEGORY_MAP.get(norm(value).upper(), "")


def status_key(value):
    value = re.sub(r"\s*/\s*", "/", norm(value).upper())
    return "" if value in {"", "-"} else value


def contract_no(value):
    """'0011224-MADY-Mercedes' / '0011224 (Draft)' -> '11224'. '000-x' -> ''."""
    match = re.match(r"\s*(\d+)", str(value or ""))
    return match.group(1).lstrip("0") if match else ""


def agr_label(value):
    match = re.match(r"\s*(\d+)", str(value or ""))
    return match.group(1) if match else norm(value) or "-"


def parse_dt(date_value, time_value):
    try:
        day = datetime.strptime(norm(date_value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    clock = time(0, 0)
    raw = norm(time_value)
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            clock = datetime.strptime(raw[:8], fmt).time()
            break
        except ValueError:
            continue
    return datetime.combine(day, clock, DUBAI_TZ)


def fmt_hm(dt):
    return dt.strftime("%H:%M") if dt else "-"


def fmt_day_hm(dt):
    return dt.strftime("%d %b %H:%M") if dt else "-"


def fmt_day(dt):
    return dt.strftime("%d %b %Y") if dt else "-"


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


def titled_section(title, body):
    return [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}}] + text_to_blocks(body)


def get_records(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("bookings", "data", "records", "vehicles"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


# ---------------------------------------------------------------- sources

def fetch_sheet_fleet():
    response = requests.get(SHEET_CSV_URL, timeout=30)
    response.raise_for_status()
    reader = csv.DictReader(io.StringIO(response.text))
    headers = {norm(h).upper() for h in (reader.fieldnames or []) if h}
    missing = {"PLATE", "VEHICLE", "CATEGORY", "STATUS"} - headers
    if missing:
        raise SystemExit(f"  Sheet did not return the fleet table (missing {sorted(missing)}). Not posting.")

    fleet, uncounted, seen = [], [], set()
    below_legend = False
    for raw_row in reader:
        row = {norm(k).upper(): v for k, v in raw_row.items() if k}
        plate = norm(row.get("PLATE"))
        vehicle = norm(row.get("VEHICLE")).upper()
        if not plate or not vehicle:
            if plate.startswith("-"):
                below_legend = True   # colour legend ("- IDLE", "- RENTED", ...)
            continue
        key = plate_key(plate)
        if key in seen:
            print(f"  WARNING duplicate plate in sheet ignored: {plate}")
            continue
        seen.add(key)
        car = {
            "plate": plate,
            "plate_key": key,
            "vehicle": vehicle,
            "category_raw": norm(row.get("CATEGORY")) or "-",
            "category": category_key(row.get("CATEGORY")),
            "status": status_key(row.get("STATUS")),
            # Active fleet = rows above the legend with a known category.
            # Rows under the legend (inactive, sold, transferred) are not counted.
            "inactive": below_legend or not category_key(row.get("CATEGORY")),
        }
        (uncounted if car["inactive"] else fleet).append(car)

    if not fleet:
        raise SystemExit("  Sheet returned no fleet rows. Not posting.")
    print(f"  Sheet: {len(fleet)} active vehicles, {len(uncounted)} not active (below legend / other category)")
    return fleet, uncounted


def appic_post(url, **data):
    if not APPIC_KEY:
        raise SystemExit("  APPIC_KEY is not set. Not posting.")
    response = requests.post(url, data={"key": APPIC_KEY, **data}, timeout=90)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and payload.get("issuccess") is False:
        raise RuntimeError(f"Appic error from {url}: {payload.get('msg') or payload}")
    return payload


def fetch_bookings(today):
    end = (today + timedelta(days=FUTURE_DAYS)).strftime("%Y-%m-%d")
    records = get_records(appic_post(BOOKINGS_URL, startDate=HISTORY_START, endDate=end))
    if not records:
        raise SystemExit("  Appic bookings API returned no records. Not posting.")
    print(f"  Appic agreements: {len(records)} {dict(Counter(norm(r.get('status')).lower() for r in records))}")
    return records


def fetch_contract_plates(today):
    """contract number -> full plate, from the check-in/check-out API."""
    end = (today + timedelta(days=FUTURE_DAYS)).strftime("%Y-%m-%d")
    plates = {}
    try:
        records = get_records(appic_post(MOVEMENTS_URL, startDate=HISTORY_START, endDate=end, direction="Out"))
    except Exception as exc:   # bookings still work; ambiguous plates fall back to name matching
        print(f"  WARNING check-in/check-out API failed, plate join skipped: {exc}")
        return plates, False
    for record in records:
        number = contract_no(record.get("contractID"))
        plate = norm(record.get("vehiclePlate"))
        if number and plate:
            plates.setdefault(number, plate)
    print(f"  Contract plates from check-in/out: {len(plates)}")
    return plates, True


# ---------------------------------------------------------------- matching

NAME_STOPWORDS = {"MERCEDES", "BENZ", "MERCEDESBENZ", "AC", "THE", "AND", "NEW"}


def name_tokens(name):
    return {t for t in re.findall(r"[A-Z0-9]+", str(name or "").upper()) if len(t) > 1} - NAME_STOPWORDS


def make_resolver(sheet_cars, contract_plates):
    by_key = {car["plate_key"]: car for car in sheet_cars}
    by_digits = defaultdict(list)
    for car in sheet_cars:
        by_digits[plate_digits(car["plate"])].append(car)

    def resolve(booking):
        """Return (sheet car or None, best plate text)."""
        full = contract_plates.get(contract_no(booking.get("contractID")))
        plate = full or norm(booking.get("vehiclePlate"))
        key = plate_key(plate)
        if not key:
            return None, plate
        if key in by_key:
            return by_key[key], plate
        if key.isdigit():   # letter missing: match on digits, break ties by vehicle name
            candidates = by_digits.get(key, [])
            if len(candidates) == 1:
                return candidates[0], plate
            if len(candidates) > 1:
                tokens = name_tokens(booking.get("vehicleName"))
                scored = sorted(((len(tokens & name_tokens(c["vehicle"])), c) for c in candidates),
                                key=lambda item: item[0], reverse=True)
                if scored[0][0] > 0 and scored[0][0] > scored[1][0]:
                    return scored[0][1], plate
        return None, plate

    return resolve


def normalize_bookings(raw_bookings, resolve):
    bookings = []
    for raw in raw_bookings:
        status = norm(raw.get("status")).lower()
        if status not in {"confirmed", "closed", "draft"}:
            continue
        car, plate = resolve(raw)
        bookings.append({
            "contract": norm(raw.get("contractID")),
            "agr": agr_label(raw.get("contractID")),
            "status": status,
            "car": car,
            "identity": car["plate_key"] if car else plate_key(plate),
            "plate": car["plate"] if car else (plate or "-"),
            "vehicle": car["vehicle"] if car else (norm(raw.get("vehicleName")).upper() or "-"),
            "customer": norm(raw.get("customerName")).title() or "-",
            "mobile": norm(raw.get("mobile")),
            "start": parse_dt(raw.get("startDate"), raw.get("startTime")),
            "end": parse_dt(raw.get("endDate"), raw.get("endTime")),
            "start_date": norm(raw.get("startDate"))[:10],
            "end_date": norm(raw.get("endDate"))[:10],
        })
    return bookings


# ---------------------------------------------------------------- classification

def classify(contracts, now):
    today_str = now.strftime("%Y-%m-%d")
    started = [c for c in contracts if c["status"] in ("confirmed", "closed") and c["start"] and c["start"] <= now]
    opened = [c for c in started if c["status"] == "confirmed"]
    live = [c for c in opened if not any(o is not c and o["start"] > c["start"] for o in started)]
    stale = [c for c in opened if c not in live]
    agreement = max(live, key=lambda c: c["start"]) if live else None
    latest_start = max((c["start"] for c in started), default=None)

    def not_superseded(c):
        return latest_start is None or (c["start"] and c["start"] > latest_start)

    running_drafts = [
        c for c in contracts
        if c["status"] == "draft" and c["start"] and c["end"] and c["start"] <= now <= c["end"] and not_superseded(c)
    ]
    upcoming = sorted(
        (c for c in contracts if c["status"] in ("draft", "confirmed") and c["start"] and c["start"] > now),
        key=lambda c: c["start"],
    )

    if agreement:
        state = "OVERDUE" if agreement["end_date"] and agreement["end_date"] < today_str else "ON_RENT"
    elif running_drafts:
        state = "DRAFT_RUNNING"
    else:
        state = "FREE"

    return {
        "state": state,
        "agreement": agreement,
        "draft": max(running_drafts, key=lambda c: c["start"]) if running_drafts else None,
        "stale": stale,
        "next": upcoming[0] if upcoming else None,
        "not_superseded": not_superseded,
    }


def build_report(fleet, uncounted, bookings, now):
    today_str = now.strftime("%Y-%m-%d")
    by_identity = defaultdict(list)
    for b in bookings:
        if b["identity"]:
            by_identity[b["identity"]].append(b)
    info = {identity: classify(items, now) for identity, items in by_identity.items()}
    empty = classify([], now)

    cars = []
    checks = []
    for car in fleet:
        ci = info.get(car["plate_key"], empty)
        agreement = ci["agreement"]
        in_garage = car["status"] in GARAGE_STATUSES
        # Only a current agreement overrides the sheet's garage status; an overdue
        # (unclosed) agreement is weaker evidence, so the car stays in Garage and is flagged.
        override = (in_garage and car["category"] == "STR" and STR_OPEN_AGREEMENT_BEATS_GARAGE
                    and ci["state"] == "ON_RENT")
        bucket = "GARAGE" if in_garage and not override else car["category"]
        cars.append({**car, **ci, "bucket": bucket})

        label = f"{car['plate']} {car['vehicle']}"
        ref = agreement or ci["draft"]
        ref_txt = f" (AGR {ref['agr']}, {ref['customer']}, since {fmt_day(ref['start'])})" if ref else ""
        if override:
            checks.append(f"{label}: sheet SERVICE/GARAGE, Appic agreement open - counted as STR on rent{ref_txt}")
        elif in_garage and ci["state"] == "OVERDUE":
            checks.append(f"{label}: sheet SERVICE/GARAGE, Appic agreement still open past return date{ref_txt}")
        elif bucket == "STR" and car["status"] == "AVAILABLE" and ci["state"] != "FREE":
            checks.append(f"{label}: sheet AVAILABLE, Appic {STATE_LABEL[ci['state']]}{ref_txt}")
        elif bucket in ("STR", "LEASE", "LTR") and car["status"] == "RENTED" and ci["state"] == "FREE":
            checks.append(f"{label}: sheet RENTED, no open agreement in Appic")
        elif bucket == "NRV" and agreement:
            checks.append(f"{label}: NRV but Appic agreement open{ref_txt}")
        if bucket == "STR" and ci["state"] == "DRAFT_RUNNING":
            checks.append(f"{label}: draft covers today but agreement not opened{ref_txt}")

    buckets = Counter(c["bucket"] for c in cars)
    str_cars = [c for c in cars if c["bucket"] == "STR"]
    str_states = Counter(c["state"] for c in str_cars)

    # Deliveries: everything starting today. Returns: everything ending today that is real.
    deliveries = sorted((b for b in bookings if b["start_date"] == today_str),
                        key=lambda b: b["start"] or now)
    returns = []
    for b in bookings:
        if b["end_date"] != today_str:
            continue
        ci = info.get(b["identity"], empty)
        if b["status"] == "closed":
            returns.append(b)
        elif b["status"] == "confirmed" and b not in ci["stale"]:
            returns.append(b)
        elif b["status"] == "draft" and ci["not_superseded"](b):
            returns.append(b)
    returns.sort(key=lambda b: b["end"] or now)

    delivery_count_by_car = Counter(b["identity"] for b in deliveries if b["identity"] and b["status"] != "closed")
    for b in deliveries:
        b["warnings"] = []
        if b["status"] == "draft":
            ci = info.get(b["identity"], empty)
            other = ci["agreement"]
            if other and other is not b and other["end"] and b["start"] and other["end"] > b["start"]:
                b["warnings"].append(f"car still out on AGR {other['agr']} until {fmt_day_hm(other['end'])}")
            if b["identity"] and delivery_count_by_car[b["identity"]] > 1:
                b["warnings"].append("more than one delivery booked for this car today")

    overdue = sorted((c for c in cars if c["state"] == "OVERDUE"), key=lambda c: c["agreement"]["end"])

    total = len(cars)
    assert total == sum(buckets[b] for b in BUCKET_ORDER), "bucket counts do not add up to total"
    assert len(str_cars) == sum(str_states.values()), "STR states do not add up"

    return {
        "cars": cars, "uncounted": uncounted, "buckets": buckets, "total": total,
        "str_cars": str_cars, "str_states": str_states,
        "deliveries": deliveries, "returns": returns, "overdue": overdue, "checks": checks,
    }


# ---------------------------------------------------------------- message

def movement_tag(b):
    if not b["car"]:
        return " [not in fleet sheet]"
    return " [not active in sheet]" if b["car"].get("inactive") else ""


def fmt_deliveries(deliveries):
    if not deliveries:
        return "None scheduled."
    status_text = {"confirmed": "Agreement opened", "closed": "Opened & closed", "draft": "Draft - agreement not opened"}
    lines = []
    for i, b in enumerate(deliveries, 1):
        entry = (
            f"{i}. {b['vehicle']} [{b['plate']}]{movement_tag(b)}\n"
            f"   Customer : {b['customer']} {b['mobile']}\n"
            f"   Time     : {fmt_hm(b['start'])}  ->  return {fmt_day_hm(b['end'])}\n"
            f"   AGR      : {b['agr']}  |  {status_text[b['status']]}"
        )
        for warning in b.get("warnings", []):
            entry += f"\n   !! {warning}"
        lines.append(entry)
    return "\n\n".join(lines)


def fmt_returns(returns, now):
    if not returns:
        return "None scheduled."
    lines = []
    for i, b in enumerate(returns, 1):
        if b["status"] == "closed":
            status = "Returned (agreement closed)"
        elif b["status"] == "draft":
            status = "Draft - agreement not opened"
        elif b["end"] and b["end"] < now:
            status = "Still out - return time passed"
        else:
            status = "Due back"
        lines.append(
            f"{i}. {b['vehicle']} [{b['plate']}]{movement_tag(b)}\n"
            f"   Customer : {b['customer']} {b['mobile']}\n"
            f"   Time     : {fmt_hm(b['end'])}\n"
            f"   AGR      : {b['agr']}  |  {status}"
        )
    return "\n\n".join(lines)


def build_message(report, now, plate_join_ok):
    date_str = now.strftime("%B %d, %Y").upper()
    day_str = now.strftime("%A").upper()
    b, s = report["buckets"], report["str_states"]
    returning_today = sum(1 for c in report["str_cars"] if c["state"] == "ON_RENT"
                          and c["agreement"]["end_date"] == now.strftime("%Y-%m-%d"))

    summary_lines = [
        f"{date_str} {day_str}  |  {now.strftime('%I:%M %p')}",
        "",
        f"Total Fleet       : {report['total']}",
        f"STR               : {b['STR']}",
        f"Lease             : {b['LEASE']}",
        f"LTR               : {b['LTR']}",
        f"Garage            : {b['GARAGE']}",
        f"NRV               : {b['NRV']}",
    ]
    summary_lines += [
        "",
        f"STR STATUS ({b['STR']}) - live from Appic",
        f"On Rent           : {s['ON_RENT']}  (returning today: {returning_today})",
        f"Overdue           : {s['OVERDUE']}",
    ]
    if s["DRAFT_RUNNING"]:
        summary_lines.append(f"Draft running     : {s['DRAFT_RUNNING']}")
    summary_lines.append(f"Available Now     : {s['FREE']}")

    available = sorted((c for c in report["str_cars"] if c["state"] == "FREE"), key=lambda c: c["vehicle"])
    available_lines = []
    for i, c in enumerate(available, 1):
        line = f"{i}. {c['vehicle']} [{c['plate']}]"
        nxt = c["next"]
        if nxt:
            when = f"today {fmt_hm(nxt['start'])}" if nxt["start_date"] == now.strftime("%Y-%m-%d") else fmt_day_hm(nxt["start"])
            line += f"\n   next booking {when} (AGR {nxt['agr']}, {nxt['status']})"
        available_lines.append(line)

    garage = sorted((c for c in report["cars"] if c["bucket"] == "GARAGE"), key=lambda c: c["vehicle"])
    garage_lines = [f"{i}. {c['vehicle']} [{c['plate']}] - {c['category']}" for i, c in enumerate(garage, 1)]

    deliveries, returns = report["deliveries"], report["returns"]
    d_open = sum(1 for x in deliveries if x["status"] != "draft")
    r_done = sum(1 for x in returns if x["status"] == "closed")

    overdue_lines = []
    for i, c in enumerate(report["overdue"], 1):
        ag = c["agreement"]
        days = (now.date() - ag["end"].date()).days if ag["end"] else "?"
        overdue_lines.append(
            f"{i}. {c['vehicle']} [{c['plate']}] - {c['category']}"
            f"{' (sheet: garage)' if c['bucket'] == 'GARAGE' else ''}\n"
            f"   Customer : {ag['customer']} {ag['mobile']}\n"
            f"   Due      : {fmt_day_hm(ag['end'])} ({days} day(s) ago)\n"
            f"   AGR      : {ag['agr']} still open in Appic"
        )

    blocks = []
    blocks += text_to_blocks("\n".join(summary_lines))
    blocks.append({"type": "divider"})
    blocks += titled_section(f"AVAILABLE SHORT TERM - {len(available)}",
                             "\n".join(available_lines) or "None available.")
    blocks.append({"type": "divider"})
    blocks += titled_section(f"IN GARAGE - {len(garage)}", "\n".join(garage_lines) or "None.")
    blocks.append({"type": "divider"})
    blocks += titled_section(
        f"DELIVERY TODAY - {len(deliveries)} vehicle(s) ({d_open} opened, {len(deliveries) - d_open} draft)",
        fmt_deliveries(deliveries))
    blocks.append({"type": "divider"})
    blocks += titled_section(
        f"RETURN TODAY - {len(returns)} vehicle(s) ({r_done} returned, {len(returns) - r_done} pending)",
        fmt_returns(returns, now))
    if overdue_lines:
        blocks.append({"type": "divider"})
        blocks += titled_section(f"OVERDUE - agreement still open after return date - {len(overdue_lines)}",
                                 "\n\n".join(overdue_lines))
    if report["checks"]:
        blocks.append({"type": "divider"})
        blocks += titled_section(f"DATA CHECK - sheet vs Appic - {len(report['checks'])}",
                                 "\n".join(f"- {line}" for line in report["checks"]))
    blocks.append({"type": "divider"})
    blocks += text_to_blocks(CONTACT_FOOTER)

    note = "Fleet & category: Google Sheet | Car out / returns / deliveries: Appic agreements"
    if not plate_join_ok:
        note += " | check-in/out API unavailable: plates matched by number + vehicle name"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})

    if len(blocks) > 50:
        blocks = blocks[:49] + [{"type": "context", "elements": [{"type": "mrkdwn", "text": "Message truncated."}]}]
    return {"blocks": blocks, "text": f"MKV Car Availability - {date_str} {day_str}"}


def render_text(message):
    parts = []
    for block in message["blocks"]:
        if block["type"] == "divider":
            parts.append("-" * 40)
        elif block["type"] == "section":
            parts.append(block["text"]["text"].replace("```", ""))
        elif block["type"] == "context":
            parts.append("(" + " ".join(e["text"] for e in block["elements"]) + ")")
    return "\n".join(parts)


def post_slack(message):
    if not SLACK_TOKEN:
        raise SystemExit("  SLACK_BOT_TOKEN is not set.")
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
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    now = now_dubai()
    print("=" * 55)
    print("  MKV CAR AVAILABILITY")
    print(f"  {now.strftime('%d %b %Y | %I:%M %p Dubai Time')}")
    print(f"  TEST_MODE: {TEST_MODE}   DRY_RUN: {DRY_RUN}")
    print("=" * 55)

    print("\n[1] Google Sheet master fleet ...")
    fleet, uncounted = fetch_sheet_fleet()

    print("\n[2] Appic agreements ...")
    raw_bookings = fetch_bookings(now)
    contract_plates, plate_join_ok = fetch_contract_plates(now)

    print("\n[3] Matching agreements to fleet ...")
    resolve = make_resolver(fleet + uncounted, contract_plates)
    bookings = normalize_bookings(raw_bookings, resolve)
    print(f"  Agreements on fleet cars: {sum(1 for b in bookings if b['car'])} of {len(bookings)}")

    print("\n[4] Building report ...")
    report = build_report(fleet, uncounted, bookings, now)
    b = report["buckets"]
    print(f"  Total {report['total']} = " + " + ".join(f"{BUCKET_LABEL[k]} {b[k]}" for k in BUCKET_ORDER))
    print(f"  STR states: {dict(report['str_states'])}")
    print(f"  Deliveries today: {len(report['deliveries'])} | Returns today: {len(report['returns'])}"
          f" | Overdue: {len(report['overdue'])} | Data checks: {len(report['checks'])}")

    message = build_message(report, now, plate_join_ok)

    if DRY_RUN:
        print("\n[5] DRY RUN - message not posted:\n")
        print(render_text(message))
        return

    print("\n[5] Posting to Slack ...")
    post_slack(message)


if __name__ == "__main__":
    main()
