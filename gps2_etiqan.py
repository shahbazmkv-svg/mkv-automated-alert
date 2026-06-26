"""
MKV CAR RENTAL — GPS2: AL ETIQAN Daily Report
Output : MKV_GPS2_ETIQAN_YYYY-MM-DD.xlsx + 4 Slack messages

Tabs:
  GPS2 Summary
  GPS2 Daily Movement
  GPS2 Parked Vehicles
  GPS2 Fleet Status

Auth:
  REST API (track.etqanuae.com/api/api.php)
  Uses API key from hardcoded ETIQAN config

Usage:
    python gps2_etiqan.py                      # live run (today's data)
    python gps2_etiqan.py --date 2026-06-03    # specific date
    python gps2_etiqan.py --test               # dry-run with mock data
"""

import requests
import argparse
import os
import sys
import json
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────
ETIQAN_BASE   = "http://track.etqanuae.com/api/api.php"
ETIQAN_KEY    = "8651C0D3A56F60178A09B3007B8BF32B"
SLACK_TOKEN   = os.getenv("SLACK_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "C0B6Y6EG85D")
TIMEZONE      = "Asia/Dubai"
DUBAI_OFFSET  = timezone(timedelta(hours=4))

DRY_RUN       = False  # set by --test flag

NAVY       = "1B2A5A"
GOLD       = "C9A84C"
GREEN      = "2E6B4F"
WHITE      = "FFFFFF"
LIGHT_BLUE = "EEF1F8"
LIGHT_GRN  = "EAF4EE"
LIGHT_GOLD = "FDF6E3"
SEP_BLUE   = "E8EDF7"
GREY       = "999999"

SNAPSHOT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "gps2_etiqan_snapshot.json")


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────
def fmt_duration(mins):
    if not mins:
        return "—"
    mins = int(mins)
    if mins < 60:
        return f"{mins} min"
    h = mins // 60
    m = mins % 60
    return f"{h}h {m}m"


def fwhite(bold=True, sz=11):
    return Font(name="Arial", bold=bold, color=WHITE, size=sz)


def fdark(bold=False, sz=10, color=None):
    return Font(name="Arial", bold=bold, color=color or NAVY, size=sz)


def fill(color):
    return PatternFill("solid", fgColor=color)


def thin_border():
    s = Side(style="thin", color="CCCCCC")
    return Border(left=s, right=s, top=s, bottom=s)


def center():
    return Alignment(horizontal="center", vertical="center", wrap_text=True)


def left_align():
    return Alignment(horizontal="left", vertical="center", wrap_text=True)


# ──────────────────────────────────────────────
# MOCK DATA (--test mode only)
# ──────────────────────────────────────────────
def get_mock_vehicles():
    now = datetime.now(DUBAI_OFFSET).strftime("%Y-%m-%d %H:%M")
    return [
        {
            "device_id": "U-74545", "name": "U-74545 Ferrari",
            "status": "Stopped",    "engine": 0,
            "speed_kmh": 0,         "duration_mins": 340,
            "daily_km": 45.2,       "avg_speed": 68.0,
            "odometer_km": 45099.3, "last_update": now,
            "lat": "25.2048", "lng": "55.2708",
        },
        {
            "device_id": "V-1243", "name": "V-1243 Rolls Royce Cullinan",
            "status": "Moving",     "engine": 1,
            "speed_kmh": 125,       "duration_mins": 45,
            "daily_km": 52.1,       "avg_speed": 71.5,
            "odometer_km": 49329.5, "last_update": now,
            "lat": "25.2158", "lng": "55.2821",
        },
        {
            "device_id": "*3764", "name": "*3764 Forthing S7",
            "status": "Moving",     "engine": 1,
            "speed_kmh": 140,       "duration_mins": 22,
            "daily_km": 31.5,       "avg_speed": 92.0,
            "odometer_km": 11102.2, "last_update": now,
            "lat": "25.1998", "lng": "55.2650",
        },
    ]


# ──────────────────────────────────────────────
# ODOMETER SNAPSHOT (same as GPS1/GPS3)
# ──────────────────────────────────────────────
def load_snapshot():
    try:
        if os.path.exists(SNAPSHOT_FILE):
            with open(SNAPSHOT_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_snapshot(vehicles, date_str):
    snap = {
        "date":      date_str,
        "odometers": {v["device_id"]: v.get("odometer_km", 0) for v in vehicles if v.get("device_id")},
    }
    try:
        with open(SNAPSHOT_FILE, "w") as f:
            json.dump(snap, f)
        print(f"  [GPS2] Snapshot saved ({len(snap['odometers'])} vehicles)")
    except Exception as e:
        print(f"  [GPS2] Snapshot save error: {e}")


def apply_snapshot_km(vehicles, snapshot):
    """Calculate daily_km using odometer delta, matching GPS1 logic."""
    today     = datetime.now(DUBAI_OFFSET).strftime("%Y-%m-%d")
    yesterday = (datetime.now(DUBAI_OFFSET) - timedelta(days=1)).strftime("%Y-%m-%d")
    snap_date = snapshot.get("date", "")
    snap_odos = snapshot.get("odometers", {})

    if snap_date not in (today, yesterday):
        print(f"  [GPS2] Snapshot date '{snap_date}' too old — daily_km will show 0")
        return

    for v in vehicles:
        device_id = v.get("device_id", "")
        curr = v.get("odometer_km", 0)
        prev = snap_odos.get(device_id)
        if prev is not None and curr > 0:
            v["daily_km"] = max(0.0, round(curr - prev, 1))


# ──────────────────────────────────────────────
# REST API — FETCH LIVE VEHICLE DATA
# ──────────────────────────────────────────────
def fetch_vehicles():
    """Call ETIQAN REST API: USER_GET_OBJECTS_STATUS endpoint."""
    url = ETIQAN_BASE
    params = {
        "api": "user",
        "ver": "1.0",
        "key": ETIQAN_KEY,
        "cmd": "USER_GET_OBJECTS_STATUS",
    }
    
    try:
        print(f"  [GPS2] Calling API: {url}")
        r = requests.get(url, params=params, timeout=20)
        print(f"  [GPS2] API response status: {r.status_code}")
        r.raise_for_status()
        data = r.json()
        print(f"  [GPS2] API response keys: {list(data.keys())}")
        print(f"  [GPS2] API rows count: {len(data.get('rows', []))}")
    except Exception as e:
        print(f"  [GPS2] API error: {e}")
        print(f"  [GPS2] API response text: {r.text if 'r' in locals() else 'N/A'}")
        return []

    vehicles = []
    try:
        # API returns flat array of vehicle objects, not wrapped in {"rows": [...]}
        for obj in data if isinstance(data, list) else data.get("rows", []):
            # Extract fields directly from object (ETIQAN REST API format)
            name = str(obj.get("name", "Unknown"))
            status_raw = str(obj.get("status", "Unknown")).lower()
            speed_str = str(obj.get("speed", "0"))
            odometer_raw = str(obj.get("odometer", "0"))
            lat = str(obj.get("lat", "—"))
            lng = str(obj.get("lng", "—"))
            altitude = str(obj.get("altitude", "—"))
            angle = str(obj.get("angle", "—"))
            tracker_time = str(obj.get("dt_tracker", ""))
            
            # Normalize status to GPS1/GPS3 format
            if "moving" in status_raw or "active" in status_raw:
                status = "Moving"
            elif "stopped" in status_raw or "parked" in status_raw or "idle" in status_raw:
                status = "Stopped"
            elif "offline" in status_raw or "no data" in status_raw:
                status = "Offline"
            else:
                status = status_raw.title()
            
            try:
                speed_kmh = float(speed_str)
            except:
                speed_kmh = 0
            
            try:
                # Odometer from ETIQAN is in km already
                odo = float(odometer_raw)
                odometer_km = odo  # Already in km, no conversion needed
            except:
                odometer_km = 0
            
            # Extract device_id from name (e.g., "U-74545 Ferrari" → "U-74545")
            device_id = name.split()[0] if " " in name else name
            
            vehicles.append({
                "device_id": device_id,
                "name": name,
                "status": status,
                "engine": 1 if status == "Moving" else 0,
                "speed_kmh": speed_kmh,
                "duration_mins": 0,
                "daily_km": 0.0,
                "avg_speed": 0.0,
                "odometer_km": odometer_km,
                "last_update": tracker_time,
                "lat": lat,
                "lng": lng,
            })
            print(f"    {name}: {status} @ {speed_kmh} km/h | odo={odometer_km:.1f} km")
        
        print(f"  [GPS2] Fetched {len(vehicles)} vehicles via REST API")
    except Exception as e:
        print(f"  [GPS2] Parse error: {e}")
        import traceback
        traceback.print_exc()

    return vehicles


# ──────────────────────────────────────────────
# EXCEL WORKBOOK BUILDER
# ──────────────────────────────────────────────
def build_summary(wb, vehicles, date_str):
    """GPS2 Summary tab — matches GPS1/GPS3 format."""
    ws = wb.create_sheet("GPS2 Summary", 0)
    ws.sheet_view.showGridLines = False
    
    ws.merge_cells("A1:C1")
    t = ws["A1"]
    t.value = "MKV CAR RENTAL — GPS2: AL ETIQAN — Daily Summary"
    t.font = fwhite(bold=True, sz=14)
    t.fill = fill(NAVY)
    t.alignment = center()
    ws.row_dimensions[1].height = 25

    ws.merge_cells("A2:C2")
    s = ws["A2"]
    s.value = f"Report Date: {date_str}  |  Timezone: {TIMEZONE}  |  Generated: {datetime.now(DUBAI_OFFSET).strftime('%Y-%m-%d %H:%M')}"
    s.font = fwhite(bold=False, sz=9)
    s.fill = fill(GOLD)
    s.alignment = center()
    ws.row_dimensions[2].height = 18

    total   = len(vehicles)
    offline = sum(1 for v in vehicles if "offline" in str(v["status"]).lower())
    online  = total - offline
    moving  = sum(1 for v in vehicles if "moving"  in str(v["status"]).lower())
    moved_count = sum(1 for v in vehicles if v.get("daily_km", 0) > 0 or "moving" in str(v.get("status", "")).lower())
    total_km = sum(v.get("daily_km", 0) for v in vehicles)

    stats = [
        ("Total Vehicles", total),
        ("Online", online),
        ("Offline", offline),
        ("Currently Moving", moving),
        ("Moved Today", moved_count),
        ("Total Km Driven Today", f"{total_km:.1f} km"),
    ]

    row = 4
    for label, val in stats:
        ws[f"A{row}"] = label
        ws[f"A{row}"].font = fdark(bold=True, sz=10)
        ws[f"B{row}"] = val
        ws[f"B{row}"].font = fdark(bold=False, sz=10)
        ws[f"B{row}"].alignment = center()
        row += 1

    ws.column_dimensions["A"].width = 25
    ws.column_dimensions["B"].width = 20
    ws.column_dimensions["C"].width = 20


def build_moving(wb, vehicles, date_str):
    """GPS2 Daily Movement tab."""
    ws = wb.create_sheet("GPS2 Daily Movement", 1)
    ws.sheet_view.showGridLines = False
    
    ws.merge_cells("A1:C1")
    t = ws["A1"]
    t.value = "MKV CAR RENTAL — GPS2: AL ETIQAN — Daily Movement"
    t.font = fwhite(bold=True, sz=14)
    t.fill = fill(NAVY)
    t.alignment = center()
    ws.row_dimensions[1].height = 25

    moving_vehicles = [v for v in vehicles if "moving" in str(v["status"]).lower()]
    
    ws["A3"] = "Vehicle"
    ws["B3"] = "Km Today"
    ws["C3"] = "Current Speed (kmh)"
    for col in ["A", "B", "C"]:
        ws[f"{col}3"].font = fwhite(bold=True, sz=10)
        ws[f"{col}3"].fill = fill(NAVY)
        ws[f"{col}3"].alignment = center()
        ws[f"{col}3"].border = thin_border()

    row = 4
    for v in sorted(moving_vehicles, key=lambda x: x["name"]):
        ws[f"A{row}"] = v["name"]
        ws[f"B{row}"] = f"{v.get('daily_km', 0):.1f}"
        ws[f"C{row}"] = f"{v.get('speed_kmh', 0):.0f}"
        for col in ["A", "B", "C"]:
            ws[f"{col}{row}"].border = thin_border()
            ws[f"{col}{row}"].alignment = left_align() if col == "A" else center()
        row += 1

    ws.column_dimensions["A"].width = 35
    ws.column_dimensions["B"].width = 15
    ws.column_dimensions["C"].width = 20


def build_parked(wb, vehicles, date_str):
    """GPS2 Parked Vehicles tab."""
    ws = wb.create_sheet("GPS2 Parked Vehicles", 2)
    ws.sheet_view.showGridLines = False
    
    ws.merge_cells("A1:C1")
    t = ws["A1"]
    t.value = "MKV CAR RENTAL — GPS2: AL ETIQAN — Parked Vehicles"
    t.font = fwhite(bold=True, sz=14)
    t.fill = fill(NAVY)
    t.alignment = center()
    ws.row_dimensions[1].height = 25

    stationary = [v for v in vehicles if "stopped" in str(v["status"]).lower()]
    offline = [v for v in vehicles if "offline" in str(v["status"]).lower()]
    
    ws["A3"] = "Vehicle"
    ws["B3"] = "Status"
    ws["C3"] = "Zone"
    for col in ["A", "B", "C"]:
        ws[f"{col}3"].font = fwhite(bold=True, sz=10)
        ws[f"{col}3"].fill = fill(NAVY)
        ws[f"{col}3"].alignment = center()
        ws[f"{col}3"].border = thin_border()

    row = 4
    for v in sorted(stationary, key=lambda x: x.get("duration_mins", 0), reverse=True):
        ws[f"A{row}"] = v["name"]
        ws[f"B{row}"] = v["status"]
        ws[f"C{row}"] = "—"
        for col in ["A", "B", "C"]:
            ws[f"{col}{row}"].border = thin_border()
            ws[f"{col}{row}"].alignment = left_align() if col == "A" else center()
        row += 1

    if offline:
        ws[f"A{row}"] = f"({len(offline)} offline — excluded from list)"
        ws[f"A{row}"].font = fdark(bold=False, sz=9)
        row += 1

    ws.column_dimensions["A"].width = 35
    ws.column_dimensions["B"].width = 20
    ws.column_dimensions["C"].width = 20


def build_fleet(wb, vehicles, date_str):
    """GPS2 Fleet Status tab — all vehicles."""
    ws = wb.create_sheet("GPS2 Fleet Status", 3)
    ws.sheet_view.showGridLines = False
    
    ws.merge_cells("A1:E1")
    t = ws["A1"]
    t.value = "MKV CAR RENTAL — GPS2: AL ETIQAN — Fleet Status"
    t.font = fwhite(bold=True, sz=14)
    t.fill = fill(NAVY)
    t.alignment = center()
    ws.row_dimensions[1].height = 25

    ws["A3"] = "Vehicle"
    ws["B3"] = "Status"
    ws["C3"] = "Speed (kmh)"
    ws["D3"] = "Odometer (km)"
    ws["E3"] = "Last Update"
    for col in ["A", "B", "C", "D", "E"]:
        ws[f"{col}3"].font = fwhite(bold=True, sz=10)
        ws[f"{col}3"].fill = fill(NAVY)
        ws[f"{col}3"].alignment = center()
        ws[f"{col}3"].border = thin_border()

    row = 4
    for v in sorted(vehicles, key=lambda x: x["name"]):
        ws[f"A{row}"] = v["name"]
        ws[f"B{row}"] = v["status"]
        ws[f"C{row}"] = f"{v.get('speed_kmh', 0):.0f}"
        ws[f"D{row}"] = f"{v.get('odometer_km', 0):.1f}"
        ws[f"E{row}"] = v.get("last_update", "—")
        for col in ["A", "B", "C", "D", "E"]:
            ws[f"{col}{row}"].border = thin_border()
            ws[f"{col}{row}"].alignment = left_align() if col == "A" else center()
        row += 1

    ws.column_dimensions["A"].width = 35
    ws.column_dimensions["B"].width = 15
    ws.column_dimensions["C"].width = 15
    ws.column_dimensions["D"].width = 18
    ws.column_dimensions["E"].width = 20


# ──────────────────────────────────────────────
# SLACK POSTING (4 messages)
# ──────────────────────────────────────────────
def post_to_slack(date_str, vehicles, fname):
    if not SLACK_TOKEN and not DRY_RUN:
        print("  [WARN] No SLACK_TOKEN — skipping Slack post.")
        return

    total    = len(vehicles)
    offline  = sum(1 for v in vehicles if "offline" in str(v["status"]).lower())
    online   = total - offline
    moving   = sum(1 for v in vehicles if "moving" in str(v["status"]).lower())
    total_km = sum(v.get("daily_km", 0) for v in vehicles)
    moved_count = sum(1 for v in vehicles if v.get("daily_km", 0) > 0 or "moving" in str(v.get("status", "")).lower())

    moving_vehicles  = [v for v in vehicles if "moving" in str(v["status"]).lower()]
    stationary       = [v for v in vehicles if "stopped" in str(v["status"]).lower()]
    offline_vehicles = [v for v in vehicles if "offline" in str(v["status"]).lower()]

    # ── MSG 1: Summary ─────────────────────────
    msg1 = (
        f":etiqan: *MKV CAR RENTAL — GPS2: AL ETIQAN Daily Report*\n"
        f"*Date:* {date_str}  |  *Timezone:* {TIMEZONE}\n"
        f"─────────────────────────────\n"
        f":white_check_mark: *Online:* {online} / {total}\n"
        f":red_circle: *Offline:* {offline}\n"
        f":blue_car: *Moving Now:* {moving}\n"
        f":chart_with_upwards_trend: *Moved Today:* {moved_count} vehicles\n"
        f":round_pushpin: *Total Km Driven Today:* {total_km:,.1f} km\n"
        f":busts_in_silhouette: *In a Zone:* —\n"
        f":lock: *Engine Blocked:* —\n"
        f"─────────────────────────────\n"
        f":page_facing_up: *File:* `{fname}`\n"
        f"_Generated at {datetime.now(DUBAI_OFFSET).strftime('%Y-%m-%d %H:%M')} (Dubai time)_"
    )

    # ── MSG 2: Vehicles That Moved ─────────────
    mov_table = "```\n"
    mov_table += f"{'Vehicle':<35} {'Km Today':>10} {'Zone':<20}\n"
    mov_table += "─" * 68 + "\n"
    for v in sorted(moving_vehicles, key=lambda x: x["name"]):
        dk = v.get("daily_km", 0)
        km_str = f"{dk:.1f} km" if dk > 0 else "—"
        mov_table += f"{v['name'][:34]:<35} {km_str:>10} {'—':<20}\n"
    if not moving_vehicles:
        mov_table += f"{'No vehicles moving':<35}\n"
    mov_table += "```"
    msg2 = (
        f":blue_car: *GPS2 — Vehicles That Moved Today ({len(moving_vehicles)})*\n"
        f"{mov_table}"
    )

    # ── MSG 3: Parked Vehicles ─────────────────
    park_table = "```\n"
    park_table += f"{'Vehicle':<35} {'Parked (hrs)':>12} {'Zone':<20}\n"
    park_table += "─" * 70 + "\n"
    for v in sorted(stationary, key=lambda x: x.get("duration_mins", 0), reverse=True):
        mins = v.get("duration_mins", 0)
        p_str = f"{mins/60:.1f} hrs" if mins >= 60 else f"{mins} min"
        park_table += f"{v['name'][:34]:<35} {p_str:>12} {'—':<20}\n"
    if not stationary:
        park_table += f"{'No vehicles parked':<35}\n"
    park_table += "```"
    msg3 = (
        f":parking: *GPS2 — Parked Vehicles — Online ({len(stationary)})*\n"
        f"{park_table}\n"
        f"_:red_circle: {len(offline_vehicles)} vehicle(s) offline — excluded from list_"
    )

    # ── MSG 4: Speeding Alert ──────────────────
    speeding = [v for v in vehicles if v.get("speed_kmh", 0) > 120]
    if speeding:
        spd_table = "```\n"
        spd_table += f"{'Vehicle':<35} {'Max Speed':>10} {'Avg Speed':>10} {'Trip km':>8}\n"
        spd_table += "─" * 68 + "\n"
        for v in sorted(speeding, key=lambda x: x.get("speed_kmh", 0), reverse=True):
            spd = v.get("speed_kmh", 0)
            avg = v.get("avg_speed", 0) or 0
            km = v.get("daily_km", 0) or 0
            spd_table += f"{v['name'][:34]:<35} {spd:>8.0f} km {avg:>8.0f} km {km:>6.1f}km\n"
        spd_table += "```"
        msg4 = (
            f":rotating_light: *GPS2 — Speeding Alert — Today ({len(speeding)} vehicle(s) over 120 kph)*\n"
            f"{spd_table}"
        )
    else:
        msg4 = ":white_check_mark: *GPS2 — No speeding events recorded today*"

    # ── Terminal preview ───────────────────────
    print("\n" + "─" * 60)
    print("  SLACK MESSAGE PREVIEW")
    print("─" * 60)
    for i, m in enumerate([msg1, msg2, msg3, msg4], 1):
        print(f"\n── MSG {i} ──────────────────────────────────────")
        print(m)
    print("\n" + "─" * 60)

    if DRY_RUN:
        print("  [DRY-RUN] Slack posting SKIPPED — messages shown above.")
        return

    try:
        hdrs = {"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"}
        for i, m in enumerate([msg1, msg2, msg3, msg4], 1):
            r = requests.post(
                "https://slack.com/api/chat.postMessage",
                headers=hdrs,
                json={"channel": SLACK_CHANNEL, "text": m},
                timeout=15,
            )
            if not r.json().get("ok"):
                print(f"  [Slack] MSG{i} error: {r.json().get('error')}")
            else:
                print(f"  [Slack] MSG{i} posted ✓")
        print("  [Slack] Posted 4/4 messages to #daily-gps-update")
    except Exception as e:
        print(f"  [Slack] Exception: {e}")


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    global DRY_RUN

    parser = argparse.ArgumentParser(description="MKV — GPS2: AL ETIQAN Daily Report")
    parser.add_argument("--date", help="YYYY-MM-DD (default: today)")
    parser.add_argument("--test", action="store_true", help="Dry-run with mock data — no live API calls, no Slack posting")
    args = parser.parse_args()

    DRY_RUN = args.test

    if args.date:
        report_date = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=DUBAI_OFFSET)
    else:
        report_date = datetime.now(DUBAI_OFFSET)
    report_date = report_date.replace(hour=0, minute=0, second=0, microsecond=0)
    date_str = report_date.strftime("%Y-%m-%d")

    print(f"\n{'='*55}")
    print(f"  MKV CAR RENTAL — GPS2: AL ETIQAN Report")
    print(f"  Date   : {date_str}")
    if DRY_RUN:
        print(f"  MODE   : *** DRY-RUN / TEST (mock data, no Slack) ***")
    print(f"{'='*55}")

    if DRY_RUN:
        vehicles = get_mock_vehicles()
        print(f"  [TEST] Loaded {len(vehicles)} mock vehicles")
    else:
        vehicles = fetch_vehicles()
        if not vehicles:
            print("ERROR: Could not fetch vehicles from API.")
            sys.exit(1)

    # Load and apply odometer snapshot
    snapshot = load_snapshot()
    apply_snapshot_km(vehicles, snapshot)

    # Save today's snapshot for tomorrow
    save_snapshot(vehicles, date_str)

    # Build Excel workbook
    print("  Building Excel workbook...")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    build_summary(wb, vehicles, date_str)
    build_moving(wb, vehicles, date_str)
    build_parked(wb, vehicles, date_str)
    build_fleet(wb, vehicles, date_str)

    fname = f"MKV_GPS2_ETIQAN_{date_str}.xlsx"
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)
    wb.save(out)
    print(f"  Saved: {fname}")

    # Post to Slack
    post_to_slack(date_str, vehicles, fname)

    print(f"\n{'='*55}")
    print(f"  ✓ Report complete")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
