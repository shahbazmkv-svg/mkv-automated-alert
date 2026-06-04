"""
MKV CAR RENTAL — GPS2: AL ETIQAN Daily Report
Output : MKV_GPS2_ETIQAN_YYYY-MM-DD.xlsx + Slack post

Tabs:
  GPS2 Summary
  GPS2 Moving Vehicles
  GPS2 Parked Vehicles
  GPS2 Fleet Status (all)

Usage:
    python gps2_etiqan.py
    python gps2_etiqan.py --date 2026-06-03
"""

import requests, argparse, os, sys, time, re
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

ETIQAN_BASE   = "http://track.etqanuae.com"
ETIQAN_USER   = os.getenv("ETIQAN_USER", "mkv")
ETIQAN_PASS   = os.getenv("ETIQAN_PASS", "112233")
SLACK_TOKEN   = os.getenv("SLACK_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "C0B6Y6EG85D")
TIMEZONE      = "Asia/Dubai"
DUBAI_OFFSET  = timezone(timedelta(hours=4))

NAVY       = "1B2A5A"
GOLD       = "C9A84C"
GREEN      = "2E6B4F"
WHITE      = "FFFFFF"
LIGHT_BLUE = "EEF1F8"
LIGHT_GRN  = "EAF4EE"
LIGHT_GOLD = "FDF6E3"
SEP_BLUE   = "E8EDF7"
GREY       = "999999"

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
# HELPERS
# ──────────────────────────────────────────────
def parse_duration_mins(status_full):
    """
    Converts '7 min 35 s' or '57 min 1 s' or '2 h 10 min 5 s' to total minutes.
    """
    total = 0
    h = re.search(r'(\d+)\s*h', status_full)
    m = re.search(r'(\d+)\s*min', status_full)
    if h:
        total += int(h.group(1)) * 60
    if m:
        total += int(m.group(1))
    return total


def fmt_duration(mins):
    if mins < 60:
        return f"{mins} min"
    h = mins // 60
    m = mins % 60
    return f"{h}h {m}m"


# ──────────────────────────────────────────────
# ODOMETER SNAPSHOT  (same approach as GPS1)
# Saves odometer readings to JSON each run.
# Daily km = current odometer − yesterday's snapshot.
# ──────────────────────────────────────────────
import json as _json

SNAPSHOT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "gps2_etiqan_snapshot.json")

def load_snapshot():
    try:
        if os.path.exists(SNAPSHOT_FILE):
            with open(SNAPSHOT_FILE) as f:
                return _json.load(f)
    except Exception:
        pass
    return {}

def save_snapshot(vehicles, date_str):
    snap = {
        "date":      date_str,
        "odometers": {v["imei"]: v.get("odometer_km", 0) for v in vehicles if v.get("imei")},
    }
    try:
        with open(SNAPSHOT_FILE, "w") as f:
            _json.dump(snap, f)
        print(f"  [GPS2] Snapshot saved ({len(snap['odometers'])} vehicles)")
    except Exception as e:
        print(f"  [GPS2] Snapshot save error: {e}")

def apply_snapshot_km(vehicles, snapshot):
    """
    Calculate daily_km using odometer delta, matching GPS1 calc_daily_km logic.
    Falls back to 0 if no snapshot or odometer not available.
    """
    today     = datetime.now(DUBAI_OFFSET).strftime("%Y-%m-%d")
    yesterday = (datetime.now(DUBAI_OFFSET) - timedelta(days=1)).strftime("%Y-%m-%d")
    snap_date = snapshot.get("date", "")
    snap_odos = snapshot.get("odometers", {})

    if snap_date not in (today, yesterday):
        print(f"  [GPS2] Snapshot date {snap_date} too old — km will show 0")
        return

    for v in vehicles:
        imei = v.get("imei", "")
        curr = v.get("odometer_km", 0)
        prev = snap_odos.get(imei)
        if prev is not None and curr > 0:
            delta = round(curr - prev, 1)
            v["daily_km"] = max(0.0, delta)


# ──────────────────────────────────────────────
# DATA FETCHING
# ──────────────────────────────────────────────
def fetch_vehicle_list():
    UA = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{ETIQAN_BASE}/tracking.php",
    }
    s = requests.Session()
    s.headers.update(UA)

    r = s.post(f"{ETIQAN_BASE}/func/fn_connect.php", data={
        "cmd": "login", "username": ETIQAN_USER, "password": ETIQAN_PASS,
        "remember_me": "false", "mobile": "false",
    }, timeout=20)
    if "LOGIN_TRACKING" not in r.text:
        print("  [GPS2] Login failed")
        return []
    print("  [GPS2] Login OK")

    r2 = s.get(f"{ETIQAN_BASE}/func/fn_settings.objects.php", params={
        "cmd": "load_object_list", "_search": "false",
        "rows": "500", "page": "1", "sidx": "name", "sord": "asc",
    }, timeout=20)

    vehicles = []
    try:
        data = r2.json()
        for obj in data.get("rows", []):
            cell = obj.get("cell", [])
            imei = str(obj.get("id", ""))
            name = str(cell[0]) if len(cell) > 0 else ""
            raw_plate = str(cell[2]) if len(cell) > 2 and cell[2] else ""
            plate = raw_plate if raw_plate and "<" not in raw_plate else ""
            vehicles.append({
                "name": name, "imei": imei, "plate": plate,
                "status": "—", "status_full": "—",
                "speed_kmh": "—", "duration_mins": 0,
                "last_update": "",
            })
        print(f"  [GPS2] Vehicle list: {len(vehicles)} vehicles")
    except Exception as e:
        print(f"  [GPS2] Parse error: {e}")

    # Set defaults for mileage fields
    for v in vehicles:
        v["daily_km"]  = 0.0
        v["avg_speed"] = 0.0

    # Fetch mileage for each vehicle
    today     = datetime.now(DUBAI_OFFSET)
    yesterday = today - timedelta(days=1)
    d_from    = yesterday.strftime("%Y-%m-%d %H:%M:%S")
    d_to      = today.strftime("%Y-%m-%d %H:%M:%S")

    for v in vehicles:
        try:
            rm = s.get(f"{ETIQAN_BASE}/func/fn_objects.php", params={
                "cmd":       "load_mileage_data",
                "imei":      v["imei"],
                "date_from": d_from,
                "date_to":   d_to,
            }, timeout=15)
            if rm.text.strip():
                md = rm.json()
                v["daily_km"]  = round(float(md.get("mileage",   md.get("distance", 0)) or 0), 1)
                v["avg_speed"] = round(float(md.get("avg_speed", md.get("speed_avg", 0)) or 0), 1)
        except Exception:
            pass  # mileage stays 0 if unavailable
    return vehicles


def enrich_with_live_status(vehicles):
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By
        from webdriver_manager.chrome import ChromeDriverManager
    except ImportError:
        print("  [GPS2] selenium not installed")
        return vehicles

    print("  [GPS2] Launching Chrome for live status...")
    opts = Options()
    opts.add_argument("--headless")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")

    try:
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()), options=opts
        )
    except Exception as e:
        print(f"  [GPS2] Chrome error: {e}")
        return vehicles

    try:
        driver.get(f"{ETIQAN_BASE}/index.php")
        time.sleep(2)
        driver.find_element(By.ID, "username").send_keys(ETIQAN_USER)
        driver.find_element(By.ID, "password").send_keys(ETIQAN_PASS)
        driver.find_element(By.XPATH, "//input[@value='Login']").click()
        print("  [GPS2] Waiting 12s for live data...")
        time.sleep(12)

        live_map = {}
        for row in driver.find_elements(By.TAG_NAME, "tr"):
            lines = [l.strip() for l in row.text.strip().split("\n") if l.strip()]
            if len(lines) >= 2 and any(
                w in lines[1].lower() for w in ("moving","stopped","offline","parked","idle","online","engine")
            ):
                name        = lines[0]
                status_full = lines[1]               # e.g. "Moving 7 min 35 s"
                status_word = status_full.split()[0]
                if status_word.lower() in ("engine",):
                    status_word = "Stopped"
                speed_raw   = lines[2] if len(lines) > 2 else "0"
                speed       = speed_raw.replace("kph","").strip()
                dur_mins    = parse_duration_mins(status_full)

                live_map[name] = {
                    "status":        status_word,
                    "status_full":   status_full,
                    "speed_kmh":     speed,
                    "duration_mins": dur_mins,
                    "last_update":   datetime.now(DUBAI_OFFSET).strftime("%Y-%m-%d %H:%M"),
                }

        print(f"  [GPS2] Live status: {len(live_map)} vehicles scraped")

        for v in vehicles:
            if v["name"] in live_map:
                v.update(live_map[v["name"]])
            else:
                for lname, ldata in live_map.items():
                    if v["name"][:15] in lname or lname[:15] in v["name"]:
                        v.update(ldata)
                        break

        # ── Fetch odometer via object info list (io16 = odometer in meters) ──
        print("  [GPS2] Fetching odometers via browser...")
        for v in vehicles:
            if not v.get("imei"):
                continue
            try:
                result = driver.execute_script(f"""
                    var xhr = new XMLHttpRequest();
                    xhr.open('GET', '/func/fn_settings.objects.php?cmd=load_object_info_list&imei={v["imei"]}&_search=false&rows=512&page=1&sidx=data&sord=asc', false);
                    xhr.send();
                    return xhr.responseText;
                """)
                if result and result.strip():
                    data = _json.loads(result)
                    for row in data.get("rows", []):
                        cell = row.get("cell", [])
                        if len(cell) >= 2 and str(cell[0]).lower() == "parameters":
                            # io16 = odometer in meters (Teltonika standard)
                            m = re.search(r'io16=(\d+)', str(cell[1]))
                            if m:
                                v["odometer_km"] = round(int(m.group(1)) / 1000, 1)
                                print(f"    {v['name']}: odometer={v['odometer_km']} km (io16={m.group(1)})")
                            break
            except Exception as eo:
                print(f"    {v['name']} odometer error: {eo}")

        # ── Try mileage_data directly ─────────────────────────────
        today_str     = datetime.now(DUBAI_OFFSET).strftime("%Y-%m-%d")
        yesterday_str = (datetime.now(DUBAI_OFFSET) - timedelta(days=1)).strftime("%Y-%m-%d")
        for v in vehicles:
            if v.get("daily_km", 0) > 0 or not v.get("imei"):
                continue
            try:
                result = driver.execute_script(f"""
                    var xhr = new XMLHttpRequest();
                    xhr.open('POST', '/func/fn_objects.php', false);
                    xhr.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
                    xhr.send('cmd=load_mileage_data&imei={v["imei"]}&date_from={yesterday_str}&date_to={today_str}');
                    return xhr.responseText;
                """)
                if result and result.strip() and result.strip()[0] in ('{', '['):
                    md = _json.loads(result)
                    km  = float(md.get("mileage", md.get("distance", md.get("km", 0))) or 0)
                    avg = float(md.get("avg_speed", md.get("speed_avg", 0)) or 0)
                    if km > 0:
                        v["daily_km"]  = round(km,  1)
                        v["avg_speed"] = round(avg, 1)
                        print(f"    {v['name']}: {km:.1f} km avg {avg:.0f} kph")
                    else:
                        print(f"    {v['name']} mileage_data raw: {result[:150]}")
            except Exception as em:
                print(f"    {v['name']} mileage_data error: {em}")

    except Exception as e:
        print(f"  [GPS2] Selenium error: {e}")
    finally:
        driver.quit()

    return vehicles


# ──────────────────────────────────────────────
# EXCEL — shared helpers
# ──────────────────────────────────────────────
def _title_block(ws, title, date_str, col_span, header_color=None):
    hc = header_color or NAVY
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=col_span)
    t = ws.cell(1, 1)
    t.value = f"MKV CAR RENTAL  —  GPS2: AL ETIQAN  —  {title}"
    t.font = fwhite(bold=True, sz=14); t.fill = fill(hc); t.alignment = center()
    ws.row_dimensions[1].height = 30

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=col_span)
    s = ws.cell(2, 1)
    s.value = (f"Report Date: {date_str}  |  Source: track.etqanuae.com  |  "
               f"Generated: {datetime.now(DUBAI_OFFSET).strftime('%Y-%m-%d %H:%M')} (Dubai)")
    s.font = fwhite(bold=False, sz=9); s.fill = fill(GOLD); s.alignment = center()
    ws.row_dimensions[2].height = 18


def _section_row(ws, text, col_span, row=3, bg=None):
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=col_span)
    sep = ws.cell(row, 1)
    sep.value = text
    sep.font = Font(name="Arial", bold=True, size=11, color=NAVY)
    sep.fill = fill(bg or SEP_BLUE); sep.alignment = left_align()
    ws.row_dimensions[row].height = 20


def _write_headers(ws, headers, widths, row=4, hdr_color=None):
    hc = hdr_color or NAVY
    ws.append(headers)
    for col in range(1, len(headers) + 1):
        c = ws.cell(row, col)
        c.font = fwhite(bold=True, sz=11); c.fill = fill(hc)
        c.alignment = center(); c.border = thin_border()
    ws.row_dimensions[row].height = 22
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _style_row(ws, row_n, col_span, alt=False, row_fill=None):
    for col in range(1, col_span + 1):
        c = ws.cell(row_n, col)
        c.font = fdark(sz=10); c.border = thin_border(); c.alignment = left_align()
        if row_fill:
            c.fill = fill(row_fill)
        elif alt:
            c.fill = fill(LIGHT_BLUE)


def _totals_row(ws, row_n, col_span, label="TOTAL", label2=""):
    ws.cell(row_n, 1).value = label
    ws.cell(row_n, 2).value = label2
    for col in range(1, col_span + 1):
        c = ws.cell(row_n, col)
        c.font = fwhite(bold=True, sz=10); c.fill = fill(NAVY)
        c.border = thin_border(); c.alignment = center()


# ──────────────────────────────────────────────
# TAB 1 — Summary
# ──────────────────────────────────────────────
def build_summary(wb, vehicles, date_str):
    ws = wb.create_sheet("GPS2 Summary", 0)
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 3
    ws.column_dimensions["B"].width = 32
    ws.column_dimensions["C"].width = 22
    _title_block(ws, "DAILY SUMMARY", date_str, 3)

    total   = len(vehicles)
    moving  = [v for v in vehicles if "moving"  in str(v["status"]).lower()]
    parked  = [v for v in vehicles if any(w in str(v["status"]).lower() for w in ("stop","park","idle"))]
    offline = [v for v in vehicles if "offline" in str(v["status"]).lower()]
    online  = total - len(offline)

    ws["B4"] = "Fleet Summary  —  GPS2: AL ETIQAN"
    ws["B4"].font = Font(name="Arial", bold=True, size=12, color=NAVY)

    stats = [
        ("Total Vehicles",        total),
        ("Online",                online),
        ("Offline",               len(offline)),
        ("Currently Moving",      f"{len(moving)} vehicles"),
        ("Parked / Stopped",      f"{len(parked)} vehicles"),
        ("Report Date",           date_str),
        ("Data Source",           "AL ETIQAN GPS  (track.etqanuae.com)"),
        ("Generated At",          datetime.now(DUBAI_OFFSET).strftime("%Y-%m-%d %H:%M")),
        ("Timezone",              TIMEZONE),
    ]
    for row_i, (label, val) in enumerate(stats, 5):
        ws.cell(row_i, 2).value = label
        ws.cell(row_i, 2).font  = Font(name="Arial", bold=True, size=10)
        ws.cell(row_i, 2).fill  = fill(SEP_BLUE)
        ws.cell(row_i, 3).value = str(val)
        ws.cell(row_i, 3).font  = Font(name="Arial", size=10)
        ws.row_dimensions[row_i].height = 20

    # Mini vehicle status table
    ws.cell(15, 2).value = "Vehicle Status Breakdown"
    ws.cell(15, 2).font  = Font(name="Arial", bold=True, size=11, color=NAVY)

    mini_hdrs = ["Vehicle Name", "Status", "Duration", "Speed (kph)"]
    for ci, h in enumerate(mini_hdrs, 2):
        c = ws.cell(16, ci)
        c.value = h; c.font = fwhite(bold=True, sz=10)
        c.fill = fill(NAVY); c.alignment = center(); c.border = thin_border()
        ws.column_dimensions[get_column_letter(ci)].width = [30, 14, 16, 12][ci-2]

    for ri, v in enumerate(vehicles, 17):
        status = str(v.get("status","—"))
        sl = status.lower()
        row_bg = LIGHT_GRN if "moving" in sl else LIGHT_GOLD if "stop" in sl or "park" in sl else None
        vals = [v["name"], status, fmt_duration(v.get("duration_mins",0)), v.get("speed_kmh","—")]
        for ci, val in enumerate(vals, 2):
            c = ws.cell(ri, ci)
            c.value = str(val); c.border = thin_border(); c.alignment = left_align()
            c.font = Font(name="Arial", size=10)
            if row_bg:
                c.fill = fill(row_bg)
        # colour status cell
        sc = ws.cell(ri, 3)
        if "moving" in sl:
            sc.font = Font(name="Arial", bold=True, size=10, color=GREEN)
        elif "stop" in sl or "park" in sl:
            sc.font = Font(name="Arial", bold=True, size=10, color=GOLD)
        elif "offline" in sl:
            sc.font = Font(name="Arial", bold=True, size=10, color=GREY)


# ──────────────────────────────────────────────
# TAB 2 — Moving Vehicles
# ──────────────────────────────────────────────
# ──────────────────────────────────────────────
# TAB 2 — Daily Movement  (matches GPS1 layout)
# ──────────────────────────────────────────────
def build_moving(wb, vehicles, date_str):
    ws       = wb.create_sheet("GPS2 Daily Movement")
    headers  = ["#", "Vehicle Name", "Plate / ID",
                "Moved Today", "Current Status", "Speed (km/h)",
                "Moving / Stopped For", "Last Update"]
    widths   = [5, 32, 16, 14, 16, 14, 22, 22]
    col_span = len(headers)

    _title_block(ws, "DAILY MOVEMENT REPORT", date_str, col_span, header_color=GREEN)
    moved_count = sum(1 for v in vehicles if "moving" in str(v["status"]).lower())
    _section_row(ws,
                 f"▌  GPS2: AL ETIQAN  —  {moved_count} vehicle(s) moved today  |  "
                 f"{len(vehicles) - moved_count} parked / stopped",
                 col_span, row=3, bg="EAF4EE")
    _write_headers(ws, headers, widths, row=4, hdr_color=GREEN)

    data_start = 5
    for i, v in enumerate(vehicles, 1):
        rn     = data_start + i - 1
        status = str(v.get("status", "—"))
        sl     = status.lower()
        moved  = "moving" in sl
        alt    = (i % 2 == 0)

        ws.cell(rn, 1).value = i
        ws.cell(rn, 2).value = v.get("name", "")
        ws.cell(rn, 3).value = v.get("plate", "")
        ws.cell(rn, 4).value = "✓ Yes" if moved else "No"
        ws.cell(rn, 5).value = status
        ws.cell(rn, 6).value = v.get("speed_kmh", "—") if moved else "0"
        ws.cell(rn, 7).value = fmt_duration(v.get("duration_mins", 0))
        ws.cell(rn, 8).value = v.get("last_update", "")
        _style_row(ws, rn, col_span, alt=alt)

        # Moved Today colour
        mc = ws.cell(rn, 4)
        mc.font = Font(name="Arial", bold=True, size=10,
                       color=GREEN if moved else GREY)
        # Status colour
        sc = ws.cell(rn, 5)
        if moved:
            sc.font = Font(name="Arial", bold=True, size=10, color=GREEN)
        elif "offline" in sl:
            sc.font = Font(name="Arial", bold=True, size=10, color=GREY)
        else:
            sc.font = Font(name="Arial", bold=True, size=10, color=GOLD)
        # Duration colour
        dc = ws.cell(rn, 7)
        dc.font = Font(name="Arial", bold=True, size=10,
                       color=GREEN if moved else GOLD)

    ws.freeze_panes = "A5"
    _totals_row(ws, data_start + len(vehicles), col_span,
                label="TOTAL",
                label2=f"{moved_count} moved  |  {len(vehicles)-moved_count} parked")


# ──────────────────────────────────────────────
# TAB 3 — Parked / Activity  (matches GPS1 layout)
# ──────────────────────────────────────────────
def build_parked(wb, vehicles, date_str):
    ws       = wb.create_sheet("GPS2 Parked Vehicles")
    parked   = [v for v in vehicles if any(
                w in str(v["status"]).lower() for w in ("stop","park","idle","offline"))]
    headers  = ["#", "Vehicle Name", "Plate / ID", "IMEI",
                "Status", "Parked / Stopped For", "Online", "Last Update"]
    widths   = [5, 32, 16, 20, 14, 22, 10, 22]
    col_span = len(headers)

    _title_block(ws, "PARKED VEHICLES REPORT", date_str, col_span, header_color=NAVY)
    _section_row(ws,
                 f"▌  GPS2: AL ETIQAN  —  {len(parked)} vehicle(s) currently parked / stopped",
                 col_span, row=3, bg=SEP_BLUE)
    _write_headers(ws, headers, widths, row=4, hdr_color=NAVY)

    data_start = 5
    # Sort by longest parked first
    for i, v in enumerate(sorted(parked,
                                  key=lambda x: x.get("duration_mins", 0),
                                  reverse=True), 1):
        rn     = data_start + i - 1
        status = str(v.get("status", "—"))
        sl     = status.lower()
        alt    = (i % 2 == 0)
        offline = "offline" in sl

        ws.cell(rn, 1).value = i
        ws.cell(rn, 2).value = v.get("name", "")
        ws.cell(rn, 3).value = v.get("plate", "")
        ws.cell(rn, 4).value = v.get("imei", "")
        ws.cell(rn, 5).value = status
        ws.cell(rn, 6).value = fmt_duration(v.get("duration_mins", 0))
        ws.cell(rn, 7).value = "Offline" if offline else "Online"
        ws.cell(rn, 8).value = v.get("last_update", "")
        _style_row(ws, rn, col_span, alt=alt)

        sc = ws.cell(rn, 5)
        dc = ws.cell(rn, 6)
        oc = ws.cell(rn, 7)
        if offline:
            sc.font = Font(name="Arial", bold=True, size=10, color=GREY)
            dc.font = Font(name="Arial", bold=True, size=10, color=GREY)
            oc.font = Font(name="Arial", bold=True, size=10, color=GREY)
        else:
            sc.font = Font(name="Arial", bold=True, size=10, color=GOLD)
            dc.font = Font(name="Arial", bold=True, size=10, color=GOLD)
            oc.font = Font(name="Arial", bold=True, size=10, color=GREEN)

    ws.freeze_panes = "A5"
    _totals_row(ws, data_start + len(parked), col_span,
                label="TOTAL", label2=f"{len(parked)} parked / stopped")

    if not parked:
        ws.cell(data_start, 1).value = "No vehicles parked"
        ws.cell(data_start, 1).font = Font(name="Arial", italic=True, size=10, color=GREY)


# ──────────────────────────────────────────────
# TAB 4 — All Vehicles Fleet Status
# ──────────────────────────────────────────────
def build_fleet(wb, vehicles, date_str):
    ws       = wb.create_sheet("GPS2 Fleet Status")
    headers  = ["#", "Vehicle Name", "Plate / ID", "IMEI",
                "Status", "Speed (km/h)", "Duration", "Last Update"]
    widths   = [5, 32, 16, 20, 14, 13, 16, 22]
    col_span = len(headers)

    _title_block(ws, "FULL FLEET STATUS", date_str, col_span)
    _section_row(ws, f"▌  GPS2: AL ETIQAN  —  All {len(vehicles)} Vehicles",
                 col_span, row=3)
    _write_headers(ws, headers, widths, row=4)

    data_start = 5
    for i, v in enumerate(vehicles, 1):
        rn     = data_start + i - 1
        status = str(v.get("status", "—"))
        sl     = status.lower()
        alt    = (i % 2 == 0)

        ws.cell(rn, 1).value = i
        ws.cell(rn, 2).value = v.get("name", "")
        ws.cell(rn, 3).value = v.get("plate", "")
        ws.cell(rn, 4).value = v.get("imei", "")
        ws.cell(rn, 5).value = status
        ws.cell(rn, 6).value = v.get("speed_kmh", "")
        ws.cell(rn, 7).value = fmt_duration(v.get("duration_mins", 0))
        ws.cell(rn, 8).value = v.get("last_update", "")
        _style_row(ws, rn, col_span, alt=alt)

        sc = ws.cell(rn, 5)
        if "moving" in sl:
            sc.font = Font(name="Arial", bold=True, size=10, color=GREEN)
        elif any(w in sl for w in ("stop","park","idle")):
            sc.font = Font(name="Arial", bold=True, size=10, color=GOLD)
        elif "offline" in sl:
            sc.font = Font(name="Arial", bold=True, size=10, color=GREY)
        else:
            sc.font = Font(name="Arial", bold=True, size=10, color=NAVY)

    ws.freeze_panes = "A5"
    _totals_row(ws, data_start + len(vehicles), col_span,
                label="TOTAL", label2=f"{len(vehicles)} vehicles")


# ──────────────────────────────────────────────
# SLACK
# ──────────────────────────────────────────────
def post_to_slack(date_str, vehicles, fname):
    if not SLACK_TOKEN:
        return

    total    = len(vehicles)
    online   = sum(1 for v in vehicles if "offline" not in str(v["status"]).lower())
    offline  = total - online
    moving   = sum(1 for v in vehicles if "moving" in str(v["status"]).lower())
    parked   = sum(1 for v in vehicles if any(w in str(v["status"]).lower()
                   for w in ("stop","park","idle")))
    total_km = sum(v.get("daily_km", 0) for v in vehicles)

    moving_vehicles  = [v for v in vehicles if "moving" in str(v["status"]).lower()]
    stationary       = [v for v in vehicles if any(w in str(v["status"]).lower()
                        for w in ("stop","park","idle"))]
    offline_vehicles = [v for v in vehicles if "offline" in str(v["status"]).lower()]

    # ── Message 1: Summary (matches GPS1) ───────────────────
    msg1 = (
        f":car: *MKV CAR RENTAL — GPS2: AL ETIQAN Daily Report*\n"
        f"*Date:* {date_str}  |  *Timezone:* {TIMEZONE}\n"
        f"─────────────────────────────\n"
        f":white_check_mark: *Online:* {online} / {total}\n"
        f":red_circle: *Offline:* {offline}\n"
        f":blue_car: *Moving Now:* {moving}\n"
        f":chart_with_upwards_trend: *Moved Today:* {moving} vehicles\n"
        f":round_pushpin: *Total Km Driven Today:* {total_km:,.1f} km\n"
        f"─────────────────────────────\n"
        f":page_facing_up: *File:* `{fname}`\n"
        f"_Generated at {datetime.now(DUBAI_OFFSET).strftime('%Y-%m-%d %H:%M')} (Dubai time)_"
    )

    # ── Message 2: Moving vehicles (matches GPS1: Vehicle | Km Today | Zone)
    mov_table = "```\n"
    mov_table += f"{'Vehicle':<35} {'Km Today':>10} {'Zone':<20}\n"
    mov_table += "─" * 68 + "\n"
    for v in sorted(moving_vehicles, key=lambda x: x["name"]):
        dk     = v.get("daily_km", 0)
        km_str = f"{dk:.1f} km" if dk > 0 else "—"
        mov_table += f"{v['name'][:34]:<35} {km_str:>10} {'—':<20}\n"
    mov_table += "```"
    msg2 = (
        f":blue_car: *GPS2 — Vehicles That Moved Today ({len(moving_vehicles)})*\n"
        f"{mov_table}"
    )

    # ── Message 3: Parked (matches GPS1: Vehicle | Parked (hrs) | Zone)
    park_table = "```\n"
    park_table += f"{'Vehicle':<35} {'Parked (hrs)':>12} {'Zone':<20}\n"
    park_table += "─" * 70 + "\n"
    for v in sorted(stationary, key=lambda x: x.get("duration_mins", 0), reverse=True):
        mins  = v.get("duration_mins", 0)
        p_hrs = f"{mins/60:.1f} hrs" if mins >= 60 else f"{mins} min"
        park_table += f"{v['name'][:34]:<35} {p_hrs:>12} {'—':<20}\n"
    park_table += "```"
    msg3 = (
        f":parking: *GPS2 — Parked Vehicles — Online ({len(stationary)})*\n"
        f"{park_table}\n"
        f"_:red_circle: {len(offline_vehicles)} vehicle(s) offline — excluded from list_"
    )

    # ── Message 4: Speeding alert (matches GPS1) ─────────────
    speeding = [v for v in vehicles if float(str(v.get("speed_kmh","0")).replace("—","0") or 0) > 120]
    if speeding:
        spd_table = "```\n"
        spd_table += f"{'Vehicle':<35} {'Speed (kph)':>12}\n"
        spd_table += "─" * 50 + "\n"
        for v in sorted(speeding, key=lambda x: float(str(x.get("speed_kmh","0")).replace("—","0") or 0), reverse=True):
            spd_table += f"{v['name'][:34]:<35} {v.get('speed_kmh','—'):>12}\n"
        spd_table += "```"
        msg4 = (
            f":rotating_light: *GPS2 — Speeding Alert ({len(speeding)} vehicle(s) over 120 kph)*\n"
            f"{spd_table}"
        )
    else:
        msg4 = ":white_check_mark: *GPS2 — No speeding events recorded*"

    try:
        hdrs = {"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"}
        for m in [msg1, msg2, msg3, msg4]:
            r = requests.post("https://slack.com/api/chat.postMessage",
                              headers=hdrs,
                              json={"channel": SLACK_CHANNEL, "text": m}, timeout=15)
            if not r.json().get("ok"):
                print(f"  [Slack] Error: {r.json().get('error')}")
        print("  [Slack] Posted 4 messages to #daily-gps-update")
    except Exception as e:
        print(f"  [Slack] Exception: {e}")


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="MKV — GPS2: AL ETIQAN Daily Report")
    parser.add_argument("--date", help="YYYY-MM-DD (default: yesterday)")
    args = parser.parse_args()

    if args.date:
        report_date = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=DUBAI_OFFSET)
    else:
        report_date = datetime.now(DUBAI_OFFSET) - timedelta(days=1)
    report_date = report_date.replace(hour=0, minute=0, second=0, microsecond=0)
    date_str    = report_date.strftime("%Y-%m-%d")

    print(f"\n{'='*55}")
    print(f"  MKV CAR RENTAL — GPS2: AL ETIQAN Report")
    print(f"  Date   : {date_str}")
    print(f"{'='*55}")

    vehicles = fetch_vehicle_list()
    if not vehicles:
        print("ERROR: Could not fetch vehicle list.")
        sys.exit(1)

    # Load yesterday's odometer snapshot (same logic as GPS1)
    snapshot = load_snapshot()

    vehicles = enrich_with_live_status(vehicles)

    # Apply snapshot delta to get daily_km
    apply_snapshot_km(vehicles, snapshot)

    # Save today's odometer snapshot for tomorrow's run
    save_snapshot(vehicles, date_str)

    moving = [v for v in vehicles if "moving" in str(v["status"]).lower()]
    parked = [v for v in vehicles if any(w in str(v["status"]).lower() for w in ("stop","park","idle"))]

    print(f"\n  {'Vehicle':<35} {'Status':<12} {'Duration':<12} {'Speed':>6}")
    print(f"  {'-'*68}")
    for v in vehicles:
        print(f"  {v['name']:<35} {v['status']:<12} "
              f"{fmt_duration(v.get('duration_mins',0)):<12} {v.get('speed_kmh','—'):>5} kph")

    print(f"\n  Moving : {len(moving)}  |  Parked/Stopped : {len(parked)}")

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    build_summary(wb, vehicles, date_str)
    build_moving(wb, vehicles, date_str)
    build_parked(wb, vehicles, date_str)
    build_fleet(wb, vehicles, date_str)

    fname = f"MKV_GPS2_ETIQAN_{date_str}.xlsx"
    out   = os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)
    wb.save(out)

    print(f"\n{'='*55}")
    print(f"  Report saved : {fname}")
    print(f"  Tabs : GPS2 Summary | GPS2 Daily Movement | GPS2 Parked Vehicles | GPS2 Fleet Status")
    print(f"{'='*55}\n")

    post_to_slack(date_str, vehicles, fname)


if __name__ == "__main__":
    main()
