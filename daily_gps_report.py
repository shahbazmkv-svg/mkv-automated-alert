"""
MKV CAR RENTAL — Pilot GPS Daily Report Generator
Generates an Excel workbook with 5 report tabs for all 64 vehicles.
Tabs: Mileage | Activity | Speed | Geofence | Trips & Parking
Posts a summary to Slack #daily-gps-update on completion.

Usage:
    python daily_gps_report.py
    python daily_gps_report.py --date 2026-05-31
    python daily_gps_report.py --days 7

Requirements:
    pip install requests openpyxl python-dotenv
"""

import requests
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from datetime import datetime, timedelta, timezone
import time
import argparse
import os
import sys
from dotenv import load_dotenv

load_dotenv()

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────
API_BASE      = os.getenv("PILOT_API_BASE", "https://pilot-gps.ru/api/api.php")
API_TOKEN     = os.getenv("PILOT_TOKEN", "")
API_USER      = os.getenv("PILOT_USER", "")
API_PASS      = os.getenv("PILOT_PASS", "")
SLACK_TOKEN   = os.getenv("SLACK_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "C0B6Y6EG85D")
TIMEZONE      = "Asia/Dubai"
REQUEST_DELAY = 0.3

DUBAI_OFFSET = timezone(timedelta(hours=4))

# ──────────────────────────────────────────────
# STYLES
# ──────────────────────────────────────────────
DARK_GREEN = "1A3C2E"
MID_GREEN  = "2E6B4F"
WHITE      = "FFFFFF"
LIGHT_GREY = "F5F5F5"

def header_font(bold=True, color=WHITE, size=11):
    return Font(name="Arial", bold=bold, color=color, size=size)

def body_font(bold=False, size=10):
    return Font(name="Arial", bold=bold, size=size)

def header_fill(color=DARK_GREEN):
    return PatternFill("solid", fgColor=color)

def alt_fill():
    return PatternFill("solid", fgColor=LIGHT_GREY)

def thin_border():
    s = Side(style="thin", color="CCCCCC")
    return Border(left=s, right=s, top=s, bottom=s)

def center():
    return Alignment(horizontal="center", vertical="center", wrap_text=True)

def left():
    return Alignment(horizontal="left", vertical="center", wrap_text=True)

def style_header_row(ws, row, cols):
    for col in range(1, cols + 1):
        cell = ws.cell(row=row, column=col)
        cell.font = header_font()
        cell.fill = header_fill()
        cell.alignment = center()
        cell.border = thin_border()

def style_data_row(ws, row, cols, alt=False):
    for col in range(1, cols + 1):
        cell = ws.cell(row=row, column=col)
        cell.font = body_font()
        cell.border = thin_border()
        cell.alignment = left()
        if alt:
            cell.fill = alt_fill()

def add_title_block(ws, title, date_str, col_span):
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=col_span)
    t = ws.cell(1, 1, f"MKV CAR RENTAL — {title}")
    t.font = Font(name="Arial", bold=True, size=14, color=WHITE)
    t.fill = header_fill(DARK_GREEN)
    t.alignment = center()
    ws.row_dimensions[1].height = 28

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=col_span)
    d = ws.cell(2, 1, f"Report Date: {date_str}  |  Timezone: {TIMEZONE}  |  Generated: {datetime.now(DUBAI_OFFSET).strftime('%Y-%m-%d %H:%M')}")
    d.font = Font(name="Arial", size=9, color=WHITE)
    d.fill = header_fill(MID_GREEN)
    d.alignment = center()
    ws.row_dimensions[2].height = 18

def set_col_widths(ws, widths):
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

def add_totals_row(ws, row, col_count, sum_cols, label_col=1):
    ws.cell(row, label_col).value = "TOTAL / AVERAGE"
    ws.cell(row, label_col).font = Font(name="Arial", bold=True, color=WHITE)
    ws.cell(row, label_col).fill = header_fill(MID_GREEN)
    ws.cell(row, label_col).alignment = center()
    for col, formula in sum_cols.items():
        c = ws.cell(row, col)
        c.value = formula
        c.font = Font(name="Arial", bold=True, color=WHITE)
        c.fill = header_fill(MID_GREEN)
        c.alignment = center()
        c.border = thin_border()
    for col in range(1, col_count + 1):
        ws.cell(row, col).border = thin_border()


# ──────────────────────────────────────────────
# SLACK NOTIFICATION
# ──────────────────────────────────────────────
def post_slack_summary(date_str, vehicle_count, report_stats, fname):
    if not SLACK_TOKEN:
        print("  [Slack] No SLACK_TOKEN in .env — skipping notification")
        return

    active     = report_stats.get("active_vehicles", 0)
    total_km   = report_stats.get("total_km", 0)
    total_trips= report_stats.get("total_trips", 0)
    speeding   = report_stats.get("speeding_events", 0)
    offline    = vehicle_count - active

    msg = (
        f":car: *MKV CAR RENTAL — Daily GPS Report*\n"
        f"*Date:* {date_str}  |  *Timezone:* {TIMEZONE}\n"
        f"─────────────────────────────\n"
        f":white_check_mark: *Active Vehicles:* {active} / {vehicle_count}\n"
        f":red_circle: *Offline Vehicles:* {offline}\n"
        f":round_pushpin: *Total Distance:* {total_km:,.1f} km\n"
        f":twisted_rightwards_arrows: *Total Trips:* {total_trips}\n"
        f":rotating_light: *Speeding Events:* {speeding}\n"
        f"─────────────────────────────\n"
        f":page_facing_up: *Report file:* `{fname}`\n"
        f"_Generated at {datetime.now(DUBAI_OFFSET).strftime('%Y-%m-%d %H:%M')} (Dubai time)_"
    )

    try:
        r = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"},
            json={"channel": SLACK_CHANNEL, "text": msg},
            timeout=10
        )
        result = r.json()
        if result.get("ok"):
            print(f"  [Slack] Posted to #daily-gps-update ✓")
        else:
            print(f"  [Slack] Failed: {result.get('error', 'unknown error')}")
    except Exception as e:
        print(f"  [Slack] Error: {e}")


# ──────────────────────────────────────────────
# PILOT GPS API CLIENT
# ──────────────────────────────────────────────
class PilotAPI:
    def __init__(self):
        self.session = requests.Session()
        self.base = API_BASE

        if API_TOKEN:
            self.session.params = {"sid": API_TOKEN}
            print(f"  Auth: using session token")
        elif API_USER and API_PASS:
            self.session.auth = (API_USER, API_PASS)
            print(f"  Auth: using Basic Auth ({API_USER})")
        else:
            print("  [WARN] No credentials found in .env")

    def get(self, cmd, params=None):
        p = {"cmd": cmd}
        if params:
            p.update(params)
        try:
            r = self.session.get(self.base, params=p, timeout=30)
            r.raise_for_status()
            data = r.json()
            if data.get("code") != 0:
                print(f"  [WARN] cmd={cmd} → {data.get('msg','unknown error')}")
                return None
            return data
        except Exception as e:
            print(f"  [ERROR] cmd={cmd}: {e}")
            return None

    def get_vehicles(self):
        data = self.get("list")
        if not data:
            return []
        vehicles = []
        for obj in data.get("data", []):
            vehicles.append({
                "imei":  str(obj.get("imei") or obj.get("id") or ""),
                "name":  obj.get("name", "Unknown"),
                "plate": obj.get("description", "") or obj.get("plate", ""),
                "group": obj.get("group_name", "MKV CAR RENTAL"),
            })
        return vehicles

    def get_mileage(self, imei, ts, te):
        data = self.get("mileage", {"imei": imei, "ts": ts, "te": te})
        if not data:
            return {}
        d = data.get("data", {})
        return {
            "mileage_km":   round(float(d.get("mileage", 0)), 1),
            "engine_hours": round(float(d.get("engine_hours", 0)), 2),
            "fuel_used_l":  round(float(d.get("fuel", 0)), 1),
        }

    def get_trips(self, imei, ts, te):
        data = self.get("trips", {"imei": imei, "ts": ts, "te": te})
        if not data:
            return []
        return data.get("data", [])

    def get_parkings(self, imei, ts, te):
        data = self.get("parkings", {"imei": imei, "ts": ts, "te": te})
        if not data:
            return []
        return data.get("data", [])

    def get_speed_events(self, imei, ts, te):
        data = self.get("events", {"imei": imei, "ts": ts, "te": te, "type": "speed"})
        if not data:
            return []
        return data.get("data", [])

    def get_geofence_visits(self, imei, ts, te):
        data = self.get("ag_geozones", {"imei": imei, "start": ts, "stop": te})
        if not data:
            return []
        return data.get("data", [])

    def get_activity(self, imei, ts, te):
        data = self.get("istatus", {"imei": imei, "unixtimestamp": te})
        return data.get("data", {}) if data else {}


# ──────────────────────────────────────────────
# REPORT BUILDERS
# ──────────────────────────────────────────────

def build_mileage_sheet(wb, vehicles, api, ts, te, date_str, stats):
    ws = wb.create_sheet("Mileage Report")
    headers = ["#", "Vehicle Name", "Plate / ID", "IMEI", "Mileage (km)", "Engine Hours", "Fuel Used (L)", "Avg Speed (km/h)", "Status"]
    widths  = [5, 28, 18, 18, 15, 15, 14, 17, 12]
    add_title_block(ws, "DAILY MILEAGE REPORT", date_str, len(headers))
    ws.append([])
    ws.append(headers)
    style_header_row(ws, 4, len(headers))
    set_col_widths(ws, widths)

    data_start = 5
    total_km = 0
    active_count = 0
    for i, v in enumerate(vehicles, 1):
        print(f"  Mileage: {v['name']}")
        m = api.get_mileage(v["imei"], ts, te)
        time.sleep(REQUEST_DELAY)

        km    = m.get("mileage_km", 0)
        hours = m.get("engine_hours", 0)
        fuel  = m.get("fuel_used_l", 0)
        avg   = round(km / hours, 1) if hours > 0 else 0
        status = "Active" if km > 0 else "Idle/Off"
        if km > 0:
            active_count += 1
        total_km += km

        row = [i, v["name"], v["plate"], v["imei"], km, hours, fuel, avg, status]
        ws.append(row)
        style_data_row(ws, data_start + i - 1, len(headers), alt=(i % 2 == 0))
        s_cell = ws.cell(data_start + i - 1, 9)
        s_cell.font = Font(name="Arial", bold=True, size=10,
                           color="2E6B4F" if status == "Active" else "CC4400")

    stats["total_km"] = round(total_km, 1)
    stats["active_vehicles"] = active_count

    total_row = data_start + len(vehicles)
    r1, r2 = data_start, total_row - 1
    add_totals_row(ws, total_row, len(headers), {
        5: f"=SUM(E{r1}:E{r2})",
        6: f"=SUM(F{r1}:F{r2})",
        7: f"=SUM(G{r1}:G{r2})",
        8: f"=AVERAGE(H{r1}:H{r2})",
    })
    ws.freeze_panes = "A5"


def build_activity_sheet(wb, vehicles, api, ts, te, date_str, stats):
    ws = wb.create_sheet("Activity Report")
    headers = ["#", "Vehicle Name", "Plate / ID", "Total Trips", "Total Parkings",
               "Moving Time (hrs)", "Idle Time (hrs)", "Online", "Last Seen"]
    widths  = [5, 28, 18, 13, 15, 18, 16, 10, 22]
    add_title_block(ws, "DAILY ACTIVITY REPORT", date_str, len(headers))
    ws.append([])
    ws.append(headers)
    style_header_row(ws, 4, len(headers))
    set_col_widths(ws, widths)

    data_start = 5
    total_trips = 0
    for i, v in enumerate(vehicles, 1):
        print(f"  Activity: {v['name']}")
        trips    = api.get_trips(v["imei"], ts, te)
        time.sleep(REQUEST_DELAY)
        parkings = api.get_parkings(v["imei"], ts, te)
        time.sleep(REQUEST_DELAY)
        status   = api.get_activity(v["imei"], ts, te)
        time.sleep(REQUEST_DELAY)

        moving_secs = sum(float(t.get("duration", 0)) for t in trips)
        idle_secs   = sum(float(p.get("duration", 0)) for p in parkings)
        total_trips += len(trips)

        last_seen = ""
        if status.get("last_time"):
            try:
                last_seen = datetime.fromtimestamp(int(status["last_time"]), tz=DUBAI_OFFSET).strftime("%Y-%m-%d %H:%M")
            except Exception:
                last_seen = str(status.get("last_time", ""))

        online = "Online" if status.get("online") == 1 else "Offline"
        row = [i, v["name"], v["plate"], len(trips), len(parkings),
               round(moving_secs / 3600, 2), round(idle_secs / 3600, 2), online, last_seen]
        ws.append(row)
        style_data_row(ws, data_start + i - 1, len(headers), alt=(i % 2 == 0))
        oc = ws.cell(data_start + i - 1, 8)
        oc.font = Font(name="Arial", bold=True, size=10,
                       color="2E6B4F" if online == "Online" else "999999")

    stats["total_trips"] = total_trips

    total_row = data_start + len(vehicles)
    r1, r2 = data_start, total_row - 1
    add_totals_row(ws, total_row, len(headers), {
        4: f"=SUM(D{r1}:D{r2})",
        5: f"=SUM(E{r1}:E{r2})",
        6: f"=SUM(F{r1}:F{r2})",
        7: f"=SUM(G{r1}:G{r2})",
    })
    ws.freeze_panes = "A5"


def build_speed_sheet(wb, vehicles, api, ts, te, date_str, stats):
    ws = wb.create_sheet("Speed Report")
    headers = ["#", "Vehicle Name", "Plate / ID", "Max Speed (km/h)", "Avg Speed (km/h)",
               "Speeding Events", "Time Over Limit (min)", "Speed Limit (km/h)"]
    widths  = [5, 28, 18, 17, 16, 16, 20, 18]
    add_title_block(ws, "DAILY SPEED REPORT", date_str, len(headers))
    ws.append([])
    ws.append(headers)
    style_header_row(ws, 4, len(headers))
    set_col_widths(ws, widths)

    data_start = 5
    total_speeding = 0
    for i, v in enumerate(vehicles, 1):
        print(f"  Speed: {v['name']}")
        trips  = api.get_trips(v["imei"], ts, te)
        time.sleep(REQUEST_DELAY)
        events = api.get_speed_events(v["imei"], ts, te)
        time.sleep(REQUEST_DELAY)

        max_speed  = max((float(t.get("max_speed", 0)) for t in trips), default=0)
        avg_speeds = [float(t.get("avg_speed", 0)) for t in trips if float(t.get("avg_speed", 0)) > 0]
        avg_speed  = round(sum(avg_speeds) / len(avg_speeds), 1) if avg_speeds else 0
        speed_events   = len(events)
        over_limit_min = round(sum(float(e.get("duration", 0)) for e in events) / 60, 1)
        limit = 120
        total_speeding += speed_events

        row = [i, v["name"], v["plate"], round(max_speed, 1), avg_speed,
               speed_events, over_limit_min, limit]
        ws.append(row)
        style_data_row(ws, data_start + i - 1, len(headers), alt=(i % 2 == 0))
        if max_speed > limit:
            ws.cell(data_start + i - 1, 4).font = Font(name="Arial", bold=True, color="CC0000")

    stats["speeding_events"] = total_speeding

    total_row = data_start + len(vehicles)
    r1, r2 = data_start, total_row - 1
    add_totals_row(ws, total_row, len(headers), {
        4: f"=MAX(D{r1}:D{r2})",
        5: f"=AVERAGE(E{r1}:E{r2})",
        6: f"=SUM(F{r1}:F{r2})",
        7: f"=SUM(G{r1}:G{r2})",
    })
    ws.freeze_panes = "A5"


def build_geofence_sheet(wb, vehicles, api, ts, te, date_str):
    ws = wb.create_sheet("Geofence Report")
    headers = ["#", "Vehicle Name", "Plate / ID", "Geofence Name", "Entry Time",
               "Exit Time", "Duration (min)", "Event Type"]
    widths  = [5, 28, 18, 25, 20, 20, 15, 14]
    add_title_block(ws, "DAILY GEOFENCE REPORT", date_str, len(headers))
    ws.append([])
    ws.append(headers)
    style_header_row(ws, 4, len(headers))
    set_col_widths(ws, widths)

    data_start = 5
    row_num = data_start
    for v in vehicles:
        print(f"  Geofence: {v['name']}")
        visits = api.get_geofence_visits(v["imei"], ts, te)
        time.sleep(REQUEST_DELAY)

        if not visits:
            ws.append(["", v["name"], v["plate"], "No geofence events", "", "", "", ""])
            style_data_row(ws, row_num, len(headers))
            row_num += 1
            continue

        for visit in visits:
            entry_ts = visit.get("entry_time") or visit.get("start") or ""
            exit_ts  = visit.get("exit_time")  or visit.get("stop")  or ""
            entry_str, exit_str, dur = "", "", 0
            try:
                if entry_ts:
                    entry_str = datetime.fromtimestamp(int(entry_ts), tz=DUBAI_OFFSET).strftime("%Y-%m-%d %H:%M")
                if exit_ts:
                    exit_str = datetime.fromtimestamp(int(exit_ts), tz=DUBAI_OFFSET).strftime("%Y-%m-%d %H:%M")
                if entry_ts and exit_ts:
                    dur = round((int(exit_ts) - int(entry_ts)) / 60, 1)
            except Exception:
                pass

            etype = visit.get("event_type", visit.get("type", "Visit"))
            ws.append(["", v["name"], v["plate"],
                       visit.get("zone_name", visit.get("name", "")),
                       entry_str, exit_str, dur, etype])
            style_data_row(ws, row_num, len(headers), alt=(row_num % 2 == 0))
            row_num += 1

    ws.freeze_panes = "A5"


def build_trips_parking_sheet(wb, vehicles, api, ts, te, date_str):
    ws = wb.create_sheet("Trips & Parking")
    headers = ["#", "Vehicle Name", "Plate / ID", "Type", "Start Time", "End Time",
               "Duration (min)", "Distance (km)", "Start Location", "End Location", "Max Speed"]
    widths  = [5, 26, 16, 10, 20, 20, 14, 14, 28, 28, 12]
    add_title_block(ws, "TRIPS & PARKING REPORT", date_str, len(headers))
    ws.append([])
    ws.append(headers)
    style_header_row(ws, 4, len(headers))
    set_col_widths(ws, widths)

    data_start = 5
    row_num = data_start
    counter = 1
    for v in vehicles:
        print(f"  Trips & Parking: {v['name']}")
        trips    = api.get_trips(v["imei"], ts, te)
        time.sleep(REQUEST_DELAY)
        parkings = api.get_parkings(v["imei"], ts, te)
        time.sleep(REQUEST_DELAY)

        events = []
        for t in trips:
            events.append({**t, "_type": "Trip"})
        for p in parkings:
            events.append({**p, "_type": "Parking"})
        events.sort(key=lambda x: int(x.get("start_time", x.get("ts", 0)) or 0))

        if not events:
            ws.append([counter, v["name"], v["plate"], "No data", "", "", "", "", "", "", ""])
            style_data_row(ws, row_num, len(headers))
            row_num += 1
            counter += 1
            continue

        for ev in events:
            st = ev.get("start_time") or ev.get("ts") or ""
            et = ev.get("end_time")   or ev.get("te") or ""
            st_str, et_str, dur = "", "", 0
            try:
                if st:
                    st_str = datetime.fromtimestamp(int(st), tz=DUBAI_OFFSET).strftime("%Y-%m-%d %H:%M")
                if et:
                    et_str = datetime.fromtimestamp(int(et), tz=DUBAI_OFFSET).strftime("%Y-%m-%d %H:%M")
                dur = round(float(ev.get("duration", 0)) / 60, 1)
            except Exception:
                pass

            dist      = round(float(ev.get("mileage", ev.get("distance", 0)) or 0), 1)
            mspd      = round(float(ev.get("max_speed", 0) or 0), 1)
            start_loc = ev.get("start_address", ev.get("begin_address", ""))
            end_loc   = ev.get("end_address",   ev.get("finish_address", ""))
            etype     = ev["_type"]

            ws.append([counter, v["name"], v["plate"], etype, st_str, et_str,
                       dur, dist, start_loc, end_loc, mspd])
            style_data_row(ws, row_num, len(headers), alt=(row_num % 2 == 0))
            ws.cell(row_num, 4).font = Font(name="Arial", bold=True, size=10,
                                            color="1A3C2E" if etype == "Trip" else "CC7700")
            row_num += 1
            counter += 1

    ws.freeze_panes = "A5"


def build_summary_sheet(wb, date_str, vehicle_count):
    ws = wb.create_sheet("Summary", 0)
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 3
    ws.column_dimensions["B"].width = 32
    ws.column_dimensions["C"].width = 22
    ws.column_dimensions["D"].width = 22

    ws.merge_cells("B1:D1")
    t = ws["B1"]
    t.value = "MKV CAR RENTAL"
    t.font = Font(name="Arial", bold=True, size=18, color=WHITE)
    t.fill = header_fill(DARK_GREEN)
    t.alignment = center()
    ws.row_dimensions[1].height = 36

    ws.merge_cells("B2:D2")
    s = ws["B2"]
    s.value = f"Daily GPS Fleet Report  |  {date_str}  |  Timezone: {TIMEZONE}"
    s.font = Font(name="Arial", size=10, color=WHITE)
    s.fill = header_fill(MID_GREEN)
    s.alignment = center()
    ws.row_dimensions[2].height = 20

    ws["B4"] = "Fleet Overview"
    ws["B4"].font = Font(name="Arial", bold=True, size=12, color=DARK_GREEN)

    info = [
        ("Total Vehicles", vehicle_count),
        ("Report Date", date_str),
        ("Generated At", datetime.now(DUBAI_OFFSET).strftime("%Y-%m-%d %H:%M")),
        ("Timezone", TIMEZONE),
        ("Data Source", "Pilot GPS (pilot-gps.ru)"),
    ]
    for row_i, (label, val) in enumerate(info, 5):
        ws.cell(row_i, 2).value = label
        ws.cell(row_i, 2).font = Font(name="Arial", bold=True, size=10)
        ws.cell(row_i, 2).fill = PatternFill("solid", fgColor=LIGHT_GREY)
        ws.cell(row_i, 3).value = str(val)
        ws.cell(row_i, 3).font = Font(name="Arial", size=10)
        ws.row_dimensions[row_i].height = 18

    ws["B11"] = "Report Tabs"
    ws["B11"].font = Font(name="Arial", bold=True, size=12, color=DARK_GREEN)

    tabs = [
        ("Mileage Report",  "Daily km, engine hours, fuel per vehicle"),
        ("Activity Report", "Trips count, moving/idle time, online status"),
        ("Speed Report",    "Max/avg speed, speeding events"),
        ("Geofence Report", "Geofence entries, exits, duration"),
        ("Trips & Parking", "Every trip and parking event with timestamps"),
    ]
    for row_i, (tab, desc) in enumerate(tabs, 12):
        ws.cell(row_i, 2).value = tab
        ws.cell(row_i, 2).font = Font(name="Arial", bold=True, size=10, color=MID_GREEN)
        ws.cell(row_i, 3).value = desc
        ws.cell(row_i, 3).font = Font(name="Arial", size=10)
        ws.row_dimensions[row_i].height = 18


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="MKV Car Rental — Pilot GPS Daily Report")
    parser.add_argument("--date", help="Report date YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--days", type=int, default=1, help="Number of days (default: 1)")
    args = parser.parse_args()

    if args.date:
        report_date = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=DUBAI_OFFSET)
    else:
        report_date = datetime.now(DUBAI_OFFSET) - timedelta(days=1)

    report_date = report_date.replace(hour=0, minute=0, second=0, microsecond=0)
    end_date    = report_date + timedelta(days=args.days)
    ts = int(report_date.timestamp())
    te = int(end_date.timestamp())
    date_str = report_date.strftime("%Y-%m-%d") if args.days == 1 else \
               f"{report_date.strftime('%Y-%m-%d')} to {(end_date - timedelta(days=1)).strftime('%Y-%m-%d')}"

    print(f"\n{'='*55}")
    print(f"  MKV CAR RENTAL — Daily GPS Report")
    print(f"  Period: {date_str}")
    print(f"{'='*55}\n")

    api = PilotAPI()

    print("Fetching vehicle list...")
    vehicles = api.get_vehicles()
    if not vehicles:
        print("ERROR: Could not fetch vehicles. Check credentials in .env")
        sys.exit(1)
    print(f"Found {len(vehicles)} vehicles.\n")

    stats = {}
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    build_summary_sheet(wb, date_str, len(vehicles))
    print("Building Mileage Report...")
    build_mileage_sheet(wb, vehicles, api, ts, te, date_str, stats)
    print("\nBuilding Activity Report...")
    build_activity_sheet(wb, vehicles, api, ts, te, date_str, stats)
    print("\nBuilding Speed Report...")
    build_speed_sheet(wb, vehicles, api, ts, te, date_str, stats)
    print("\nBuilding Geofence Report...")
    build_geofence_sheet(wb, vehicles, api, ts, te, date_str)
    print("\nBuilding Trips & Parking Report...")
    build_trips_parking_sheet(wb, vehicles, api, ts, te, date_str)

    fname = f"MKV_GPS_Report_{report_date.strftime('%Y-%m-%d')}.xlsx"
    out   = os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)
    wb.save(out)
    print(f"\n✓ Report saved: {fname}")
    print(f"  Vehicles processed: {len(vehicles)}")
    print(f"  Tabs: Summary | Mileage | Activity | Speed | Geofence | Trips & Parking")

    print("\nPosting to Slack...")
    post_slack_summary(date_str, len(vehicles), stats, fname)
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
