"""
MKV CAR RENTAL — GPS3: AmberConnect Daily Report
Output : MKV_GPS3_AMBER_YYYY-MM-DD.xlsx + 4 Slack messages

Tabs:
  GPS3 Summary
  GPS3 Daily Movement
  GPS3 Parked Vehicles
  GPS3 Fleet Status

Auth:
  AmberConnect uses reCAPTCHA v3 — login is handled via PHPSESSID cookie.
  Set AMBER_SESSION in GitHub Secrets (refresh from Chrome > DevTools >
  Application > Cookies > fleet.amberconnect.ai when it expires).

Usage:
    python gps3_amber.py                      # live run
    python gps3_amber.py --date 2026-06-06    # specific date
    python gps3_amber.py --test               # dry-run with mock data, no Slack
"""

import requests
import argparse
import os
import sys
import json
import re
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────
AMBER_BASE     = "https://fleet.amberconnect.ai"
AMBER_FLEET_ID = os.getenv("AMBER_FLEET_ID", "0370168")
AMBER_USER     = os.getenv("AMBER_USER", "muneer-fleet@mkvluxury.com")
AMBER_PASS     = os.getenv("AMBER_PASS", "")
AMBER_SESSION  = os.getenv("AMBER_SESSION", "")   # PHPSESSID
AMBER_F_COOK   = os.getenv("AMBER_F_COOK",   "")  # f_cook
AMBER_P_COOK   = os.getenv("AMBER_P_COOK",   "")  # p_cook
AMBER_TAB      = os.getenv("AMBER_TAB",      '{"guid":"18b9140b-3c7d-34f0-c048-ad46c3f3c451","timestamp":1780902109374}')

SLACK_TOKEN    = os.getenv("SLACK_TOKEN", "")
SLACK_CHANNEL  = os.getenv("SLACK_CHANNEL", "C0B6Y6EG85D")
TIMEZONE       = "Asia/Dubai"
DUBAI_OFFSET   = timezone(timedelta(hours=4))

SNAPSHOT_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "gps3_amber_snapshot.json")

# ── birdsEye response array indices ─────────────────────────────
IDX_DEVICE_ID     = 2
IDX_VEHICLE_NAME  = 5
IDX_ENGINE_STATUS = 6   # 1=on/moving, 0=off/stopped
IDX_SPEED         = 7
IDX_ODOMETER      = 14  # metres

# ──────────────────────────────────────────────
# DRY-RUN / TEST MODE
# ──────────────────────────────────────────────
DRY_RUN = False   # overridden by --test flag in main()

# ──────────────────────────────────────────────
# STYLES
# ──────────────────────────────────────────────
NAVY       = "1B2A5A"
GOLD       = "C9A84C"
GREEN      = "2E6B4F"
PURPLE     = "4B0082"        # GPS3 accent colour (AmberConnect)
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
def fmt_duration(mins):
    if not mins:
        return "—"
    mins = int(mins)
    if mins < 60:
        return f"{mins} min"
    h = mins // 60
    m = mins % 60
    return f"{h}h {m}m"


# ──────────────────────────────────────────────
# MOCK DATA  (--test mode only)
# ──────────────────────────────────────────────
def get_mock_vehicles():
    now = datetime.now(DUBAI_OFFSET).strftime("%Y-%m-%d %H:%M")
    return [
        {
            "device_id": "AMB-001", "name": "A-8811 Lamborghini Urus",
            "status": "Moving",     "engine": 1,
            "speed_kmh": "112",     "duration_mins": 8,
            "daily_km": 98.4,       "avg_speed": 88.0,
            "odometer_km": 31200.0, "last_update": now,
        },
        {
            "device_id": "AMB-002", "name": "B-5502 McLaren 720S",
            "status": "Stopped",    "engine": 0,
            "speed_kmh": "0",       "duration_mins": 780,
            "daily_km": 0.0,        "avg_speed": 0.0,
            "odometer_km": 9800.0,  "last_update": now,
        },
        {
            "device_id": "AMB-003", "name": "C-1199 Porsche Cayenne",
            "status": "Stopped",    "engine": 0,
            "speed_kmh": "0",       "duration_mins": 1200,
            "daily_km": 0.0,        "avg_speed": 0.0,
            "odometer_km": 18500.0, "last_update": now,
        },
        {
            "device_id": "AMB-004", "name": "D-7730 Ferrari Roma",
            "status": "Moving",     "engine": 1,
            "speed_kmh": "145",     "duration_mins": 4,   # over 120 → speeding
            "daily_km": 54.7,       "avg_speed": 103.0,
            "odometer_km": 6600.0,  "last_update": now,
        },
        {
            "device_id": "AMB-005", "name": "E-3345 Rolls Royce Ghost",
            "status": "Offline",    "engine": 0,
            "speed_kmh": "0",       "duration_mins": 0,
            "daily_km": 0.0,        "avg_speed": 0.0,
            "odometer_km": 0.0,     "last_update": now,
        },
    ]


# ──────────────────────────────────────────────
# ODOMETER SNAPSHOT
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
        "odometers": {v["device_id"]: v.get("odometer_km", 0) for v in vehicles},
    }
    try:
        with open(SNAPSHOT_FILE, "w") as f:
            json.dump(snap, f)
        print(f"  [GPS3] Snapshot saved ({len(snap['odometers'])} vehicles)")
    except Exception as e:
        print(f"  [GPS3] Snapshot save error: {e}")


def apply_snapshot_km(vehicles, snapshot):
    today     = datetime.now(DUBAI_OFFSET).strftime("%Y-%m-%d")
    yesterday = (datetime.now(DUBAI_OFFSET) - timedelta(days=1)).strftime("%Y-%m-%d")
    snap_date = snapshot.get("date", "")
    snap_odos = snapshot.get("odometers", {})

    if snap_date not in (today, yesterday):
        print(f"  [GPS3] Snapshot date '{snap_date}' too old — daily_km will show 0")
        return

    for v in vehicles:
        did  = v.get("device_id", "")
        curr = v.get("odometer_km", 0)
        prev = snap_odos.get(did)
        if prev is not None and curr > 0:
            v["daily_km"] = max(0.0, round(curr - prev, 1))


# ──────────────────────────────────────────────
# SLACK — SESSION EXPIRY ALERT
# ──────────────────────────────────────────────
def post_session_alert(reason="unknown"):
    """
    Posts a Slack warning to #daily-gps-update when the
    AMBER_SESSION cookie is missing or has expired.
    """
    now_str = datetime.now(DUBAI_OFFSET).strftime("%Y-%m-%d %H:%M")
    msg = (
        f":warning: *GPS3 AmberConnect — Session Expired*\n"
        f"─────────────────────────────\n"
        f":date: *Date:* {datetime.now(DUBAI_OFFSET).strftime('%Y-%m-%d')}  |  "
        f"*Time:* {now_str} (Dubai)\n"
        f":x: *Reason:* {reason}\n"
        f"─────────────────────────────\n"
        f":wrench: *Action required:*\n"
        f"1. Open Chrome → go to `fleet.amberconnect.ai` and log in\n"
        f"2. Press F12 → Application → Cookies → `fleet.amberconnect.ai`\n"
        f"3. Copy the `PHPSESSID` value\n"
        f"4. Go to GitHub → repo Settings → Secrets → update `AMBER_SESSION`\n"
        f"5. Re-trigger the `gps3-amber` job from GitHub Actions\n"
        f"─────────────────────────────\n"
        f"_GPS3 report was NOT generated for today._"
    )
    print(f"\n  [GPS3] Posting session expiry alert to Slack...")
    print(msg)
    if not SLACK_TOKEN:
        print("  [GPS3] No SLACK_TOKEN — alert printed to log only.")
        return
    try:
        r = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {SLACK_TOKEN}",
                     "Content-Type": "application/json"},
            json={"channel": SLACK_CHANNEL, "text": msg},
            timeout=15,
        )
        if r.json().get("ok"):
            print("  [GPS3] Session alert posted to Slack ✓")
        else:
            print(f"  [GPS3] Slack alert failed: {r.json().get('error')}")
    except Exception as e:
        print(f"  [GPS3] Slack alert exception: {e}")


# ──────────────────────────────────────────────
# API — FETCH LIVE VEHICLE DATA
# ──────────────────────────────────────────────
def fetch_vehicles():
    """
    POST /ajax/birdsEye with PHPSESSID session cookie.
    Returns list of parsed vehicle dicts, or None if session expired.
    None  = session problem  →  alert was sent, script should exit
    []    = fetch ok but no vehicles returned
    """
    if not AMBER_SESSION:
        post_session_alert("AMBER_SESSION (PHPSESSID) secret is not set in GitHub Secrets")
        return None
    if not AMBER_F_COOK or not AMBER_P_COOK:
        post_session_alert("AMBER_F_COOK or AMBER_P_COOK secrets are missing from GitHub Secrets")

    session = requests.Session()
    # Send all session cookies together — AmberConnect requires the full set
    session.cookies.set("PHPSESSID",                AMBER_SESSION,   domain="fleet.amberconnect.ai")
    session.cookies.set("f_cook",                   AMBER_F_COOK,    domain="fleet.amberconnect.ai")
    session.cookies.set("p_cook",                   AMBER_P_COOK,    domain="fleet.amberconnect.ai")
    session.cookies.set("my-application-browser-tab", AMBER_TAB,     domain="fleet.amberconnect.ai")
    session.headers.update({
        "User-Agent":       "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "X-Requested-With": "XMLHttpRequest",
        "Referer":          f"{AMBER_BASE}/dashboard",
        "Content-Type":     "application/x-www-form-urlencoded",
    })

    try:
        r = session.post(
            f"{AMBER_BASE}/ajax/birdsEye",
            data={"fleet_id": AMBER_FLEET_ID, "action": "getVehicles"},
            timeout=30,
        )

        # Detect session expiry — AmberConnect returns HTTP 200 with HTML login
        # page when session is invalid (common PHP behaviour, not 401/302)
        content_type = r.headers.get("Content-Type", "")
        is_html      = "text/html" in content_type or r.text.strip().startswith("<")
        text_lower   = r.text.lower()[:500]
        expired = (
            r.status_code in (401, 302, 403)
            or is_html
            or "login" in text_lower
            or "session expired" in text_lower
            or "phpsessid" in text_lower
            or "sign in" in text_lower
        )
        if expired:
            post_session_alert(
                f"PHPSESSID cookie rejected by server "
                f"(HTTP {r.status_code}) — cookie has expired"
            )
            return None

        # ── DEBUG: print raw response so we can diagnose ────
        print(f"  [GPS3] HTTP {r.status_code} | Content-Type: {r.headers.get('Content-Type','?')}")
        print(f"  [GPS3] Response (first 500 chars): {r.text[:500]}")
        # ────────────────────────────────────────────────────

        try:
            raw = r.json()
        except Exception:
            print(f"  [GPS3] Response is not valid JSON — possible login page or error")
            post_session_alert(
                f"birdsEye returned non-JSON response (HTTP {r.status_code}) — "
                f"session may be invalid or API endpoint changed"
            )
            return None

        print(f"  [GPS3] JSON type: {type(raw).__name__} | "
              f"length/keys: {len(raw) if isinstance(raw, (list,dict)) else 'n/a'}")

        # birdsEye returns a list of arrays — handle dict wrapper too
        if isinstance(raw, dict):
            print(f"  [GPS3] Dict keys: {list(raw.keys())}")
            raw = raw.get("data", raw.get("vehicles", raw.get("result", [])))

        if not isinstance(raw, list):
            print(f"  [GPS3] Unexpected response structure: {type(raw)}")
            return []

        # Print first row so we can verify array indices
        if raw:
            print(f"  [GPS3] First row ({len(raw[0])} fields): {raw[0]}")

        vehicles = []
        now_str  = datetime.now(DUBAI_OFFSET).strftime("%Y-%m-%d %H:%M")
        for row in raw:
            try:
                if not isinstance(row, list) or len(row) <= IDX_ODOMETER:
                    print(f"  [GPS3] Skipping row (len={len(row) if isinstance(row,list) else 'non-list'}): {row}")
                    continue
                device_id  = str(row[IDX_DEVICE_ID])
                name       = str(row[IDX_VEHICLE_NAME]).strip() or device_id
                engine     = int(row[IDX_ENGINE_STATUS] or 0)
                speed      = float(row[IDX_SPEED] or 0)
                odo_metres = float(row[IDX_ODOMETER] or 0)
                odo_km     = round(odo_metres / 1000, 1)

                if engine == 1 or speed > 0:
                    status = "Moving"
                else:
                    status = "Stopped"

                vehicles.append({
                    "device_id":   device_id,
                    "name":        name,
                    "status":      status,
                    "engine":      engine,
                    "speed_kmh":   str(int(speed)),
                    "duration_mins": 0,
                    "daily_km":    0.0,
                    "avg_speed":   0.0,
                    "odometer_km": odo_km,
                    "last_update": now_str,
                })
            except Exception as e:
                print(f"  [GPS3] Row parse error: {e}")

        print(f"  [GPS3] Fetched {len(vehicles)} vehicles from birdsEye")
        return vehicles

    except Exception as e:
        print(f"  [GPS3] Fetch error: {e}")
        return []


# ──────────────────────────────────────────────
# EXCEL HELPERS
# ──────────────────────────────────────────────
def _title_block(ws, title, date_str, col_span):
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=col_span)
    t = ws.cell(1, 1)
    t.value = f"MKV CAR RENTAL  —  GPS3: AMBERCONNECT  —  {title}"
    t.font = fwhite(bold=True, sz=14)
    t.fill = fill(NAVY)
    t.alignment = center()
    ws.row_dimensions[1].height = 30

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=col_span)
    s = ws.cell(2, 1)
    s.value = (f"Report Date: {date_str}  |  Source: fleet.amberconnect.ai  |  "
               f"Generated: {datetime.now(DUBAI_OFFSET).strftime('%Y-%m-%d %H:%M')} (Dubai)")
    s.font = fwhite(bold=False, sz=9)
    s.fill = fill(GOLD)
    s.alignment = center()
    ws.row_dimensions[2].height = 18


def _section_row(ws, text, col_span, row=3, bg=None):
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=col_span)
    sep = ws.cell(row, 1)
    sep.value = text
    sep.font = Font(name="Arial", bold=True, size=11, color=NAVY)
    sep.fill = fill(bg or SEP_BLUE)
    sep.alignment = left_align()
    ws.row_dimensions[row].height = 20


def _write_headers(ws, headers, widths, row=4, hdr_color=None):
    hc = hdr_color or NAVY
    for col, h in enumerate(headers, 1):
        c = ws.cell(row, col, h)
        c.font = fwhite(bold=True, sz=11)
        c.fill = fill(hc)
        c.alignment = center()
        c.border = thin_border()
    ws.row_dimensions[row].height = 22
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _style_row(ws, row_n, col_span, alt=False):
    for col in range(1, col_span + 1):
        c = ws.cell(row_n, col)
        c.font = fdark(sz=10)
        c.border = thin_border()
        c.alignment = left_align()
        if alt:
            c.fill = fill(LIGHT_BLUE)


def _totals_row(ws, row_n, col_span, label="TOTAL", label2=""):
    ws.cell(row_n, 1).value = label
    ws.cell(row_n, 2).value = label2
    for col in range(1, col_span + 1):
        c = ws.cell(row_n, col)
        c.font = fwhite(bold=True, sz=10)
        c.fill = fill(NAVY)
        c.border = thin_border()
        c.alignment = center()


# ──────────────────────────────────────────────
# TAB 1 — Summary
# ──────────────────────────────────────────────
def build_summary(wb, vehicles, date_str):
    ws = wb.create_sheet("GPS3 Summary", 0)
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 3
    ws.column_dimensions["B"].width = 32
    ws.column_dimensions["C"].width = 22
    _title_block(ws, "DAILY SUMMARY", date_str, 3)

    total   = len(vehicles)
    offline = [v for v in vehicles if "offline" in str(v["status"]).lower()]
    online  = total - len(offline)
    moving  = [v for v in vehicles if "moving" in str(v["status"]).lower()]
    parked  = [v for v in vehicles if "stopped" in str(v["status"]).lower()]

    ws["B4"] = "Fleet Summary  —  GPS3: AmberConnect"
    ws["B4"].font = Font(name="Arial", bold=True, size=12, color=NAVY)

    stats = [
        ("Total Vehicles",    total),
        ("Online",            online),
        ("Offline",           len(offline)),
        ("Currently Moving",  f"{len(moving)} vehicles"),
        ("Parked / Stopped",  f"{len(parked)} vehicles"),
        ("Report Date",       date_str),
        ("Data Source",       "AmberConnect GPS  (fleet.amberconnect.ai)"),
        ("Fleet ID",          AMBER_FLEET_ID),
        ("Generated At",      datetime.now(DUBAI_OFFSET).strftime("%Y-%m-%d %H:%M")),
        ("Timezone",          TIMEZONE),
    ]
    for row_i, (label, val) in enumerate(stats, 5):
        ws.cell(row_i, 2).value = label
        ws.cell(row_i, 2).font  = Font(name="Arial", bold=True, size=10)
        ws.cell(row_i, 2).fill  = fill(SEP_BLUE)
        ws.cell(row_i, 3).value = str(val)
        ws.cell(row_i, 3).font  = Font(name="Arial", size=10)
        ws.row_dimensions[row_i].height = 20

    ws.cell(16, 2).value = "Vehicle Status Breakdown"
    ws.cell(16, 2).font  = Font(name="Arial", bold=True, size=11, color=NAVY)

    mini_hdrs = ["Vehicle Name", "Status", "Speed (kph)", "Daily km", "Odometer (km)"]
    col_widths = [30, 14, 12, 12, 16]
    for ci, h in enumerate(mini_hdrs, 2):
        c = ws.cell(17, ci, h)
        c.font = fwhite(bold=True, sz=10)
        c.fill = fill(NAVY)
        c.alignment = center()
        c.border = thin_border()
        ws.column_dimensions[get_column_letter(ci)].width = col_widths[ci-2]

    for ri, v in enumerate(vehicles, 18):
        status = str(v.get("status", "—"))
        sl = status.lower()
        row_bg = LIGHT_GRN if "moving" in sl else LIGHT_GOLD if "stopped" in sl else None
        vals = [
            v["name"], status,
            v.get("speed_kmh", "0"),
            f"{v.get('daily_km', 0):.1f}",
            f"{v.get('odometer_km', 0):.1f}",
        ]
        for ci, val in enumerate(vals, 2):
            c = ws.cell(ri, ci, str(val))
            c.border = thin_border()
            c.alignment = left_align()
            c.font = Font(name="Arial", size=10)
            if row_bg:
                c.fill = fill(row_bg)
        sc = ws.cell(ri, 3)
        if "moving" in sl:
            sc.font = Font(name="Arial", bold=True, size=10, color=GREEN)
        elif "offline" in sl:
            sc.font = Font(name="Arial", bold=True, size=10, color=GREY)
        else:
            sc.font = Font(name="Arial", bold=True, size=10, color=GOLD)


# ──────────────────────────────────────────────
# TAB 2 — Daily Movement
# ──────────────────────────────────────────────
def build_moving(wb, vehicles, date_str):
    ws       = wb.create_sheet("GPS3 Daily Movement")
    headers  = ["#", "Vehicle Name", "Device ID",
                "Moved Today", "Current Status", "Speed (km/h)",
                "Km Today", "Last Update"]
    widths   = [5, 32, 16, 14, 16, 14, 14, 22]
    col_span = len(headers)

    _title_block(ws, "DAILY MOVEMENT REPORT", date_str, col_span)
    moved_count = sum(1 for v in vehicles
                      if "moving" in str(v["status"]).lower() or v.get("daily_km", 0) > 0)
    _section_row(ws,
                 f"▌  GPS3: AmberConnect  —  {moved_count} vehicle(s) moved today  |  "
                 f"{len(vehicles) - moved_count} parked / offline",
                 col_span, row=3, bg="EAF4EE")
    _write_headers(ws, headers, widths, row=4, hdr_color=GREEN)

    data_start = 5
    for i, v in enumerate(vehicles, 1):
        rn     = data_start + i - 1
        status = str(v.get("status", "—"))
        sl     = status.lower()
        moved  = "moving" in sl or v.get("daily_km", 0) > 0
        alt    = (i % 2 == 0)
        dk     = v.get("daily_km", 0)

        ws.cell(rn, 1).value = i
        ws.cell(rn, 2).value = v.get("name", "")
        ws.cell(rn, 3).value = v.get("device_id", "")
        ws.cell(rn, 4).value = "✓ Yes" if moved else "No"
        ws.cell(rn, 5).value = status
        ws.cell(rn, 6).value = v.get("speed_kmh", "0") if "moving" in sl else "0"
        ws.cell(rn, 7).value = f"{dk:.1f}" if dk > 0 else "—"
        ws.cell(rn, 8).value = v.get("last_update", "")
        _style_row(ws, rn, col_span, alt=alt)

        ws.cell(rn, 4).font = Font(name="Arial", bold=True, size=10,
                                    color=GREEN if moved else GREY)
        sc = ws.cell(rn, 5)
        if "moving" in sl:
            sc.font = Font(name="Arial", bold=True, size=10, color=GREEN)
        elif "offline" in sl:
            sc.font = Font(name="Arial", bold=True, size=10, color=GREY)
        else:
            sc.font = Font(name="Arial", bold=True, size=10, color=GOLD)

    ws.freeze_panes = "A5"
    _totals_row(ws, data_start + len(vehicles), col_span,
                label="TOTAL", label2=f"{moved_count} moved  |  {len(vehicles)-moved_count} parked/offline")


# ──────────────────────────────────────────────
# TAB 3 — Parked Vehicles
# ──────────────────────────────────────────────
def build_parked(wb, vehicles, date_str):
    ws      = wb.create_sheet("GPS3 Parked Vehicles")
    parked  = [v for v in vehicles if "stopped" in str(v["status"]).lower()
               or "offline" in str(v["status"]).lower()]
    headers = ["#", "Vehicle Name", "Device ID",
               "Status", "Parked / Stopped For", "Online", "Last Update"]
    widths  = [5, 32, 16, 14, 22, 10, 22]
    col_span = len(headers)

    _title_block(ws, "PARKED VEHICLES REPORT", date_str, col_span)
    _section_row(ws,
                 f"▌  GPS3: AmberConnect  —  {len(parked)} vehicle(s) currently parked / stopped",
                 col_span, row=3, bg=SEP_BLUE)
    _write_headers(ws, headers, widths, row=4, hdr_color=NAVY)

    data_start = 5
    for i, v in enumerate(sorted(parked,
                                  key=lambda x: x.get("duration_mins", 0),
                                  reverse=True), 1):
        rn      = data_start + i - 1
        status  = str(v.get("status", "—"))
        sl      = status.lower()
        offline = "offline" in sl
        alt     = (i % 2 == 0)

        ws.cell(rn, 1).value = i
        ws.cell(rn, 2).value = v.get("name", "")
        ws.cell(rn, 3).value = v.get("device_id", "")
        ws.cell(rn, 4).value = status
        ws.cell(rn, 5).value = fmt_duration(v.get("duration_mins", 0))
        ws.cell(rn, 6).value = "Offline" if offline else "Online"
        ws.cell(rn, 7).value = v.get("last_update", "")
        _style_row(ws, rn, col_span, alt=alt)

        for col, color in [(4, GREY if offline else GOLD),
                            (5, GREY if offline else GOLD),
                            (6, GREY if offline else GREEN)]:
            ws.cell(rn, col).font = Font(name="Arial", bold=True, size=10, color=color)

    ws.freeze_panes = "A5"
    _totals_row(ws, data_start + len(parked), col_span,
                label="TOTAL", label2=f"{len(parked)} parked / stopped")


# ──────────────────────────────────────────────
# TAB 4 — Fleet Status
# ──────────────────────────────────────────────
def build_fleet(wb, vehicles, date_str):
    ws       = wb.create_sheet("GPS3 Fleet Status")
    headers  = ["#", "Vehicle Name", "Device ID",
                "Status", "Speed (km/h)", "Km Today",
                "Odometer (km)", "Last Update"]
    widths   = [5, 32, 16, 14, 13, 12, 16, 22]
    col_span = len(headers)

    _title_block(ws, "FULL FLEET STATUS", date_str, col_span)
    _section_row(ws, f"▌  GPS3: AmberConnect  —  All {len(vehicles)} Vehicles",
                 col_span, row=3)
    _write_headers(ws, headers, widths, row=4)

    data_start = 5
    for i, v in enumerate(vehicles, 1):
        rn     = data_start + i - 1
        status = str(v.get("status", "—"))
        sl     = status.lower()
        alt    = (i % 2 == 0)
        dk     = v.get("daily_km", 0)

        ws.cell(rn, 1).value = i
        ws.cell(rn, 2).value = v.get("name", "")
        ws.cell(rn, 3).value = v.get("device_id", "")
        ws.cell(rn, 4).value = status
        ws.cell(rn, 5).value = v.get("speed_kmh", "0")
        ws.cell(rn, 6).value = f"{dk:.1f}" if dk > 0 else "—"
        ws.cell(rn, 7).value = f"{v.get('odometer_km', 0):.1f}"
        ws.cell(rn, 8).value = v.get("last_update", "")
        _style_row(ws, rn, col_span, alt=alt)

        sc = ws.cell(rn, 4)
        if "moving" in sl:
            sc.font = Font(name="Arial", bold=True, size=10, color=GREEN)
        elif "offline" in sl:
            sc.font = Font(name="Arial", bold=True, size=10, color=GREY)
        else:
            sc.font = Font(name="Arial", bold=True, size=10, color=GOLD)

    ws.freeze_panes = "A5"
    _totals_row(ws, data_start + len(vehicles), col_span,
                label="TOTAL", label2=f"{len(vehicles)} vehicles")


# ──────────────────────────────────────────────
# SLACK — 4 messages matching GPS1 format exactly
# ──────────────────────────────────────────────
def post_to_slack(date_str, vehicles, fname):
    if not SLACK_TOKEN and not DRY_RUN:
        print("  [WARN] No SLACK_TOKEN — skipping Slack post.")
        return

    total    = len(vehicles)
    offline  = sum(1 for v in vehicles if "offline" in str(v["status"]).lower())
    online   = total - offline
    moving   = sum(1 for v in vehicles if "moving"  in str(v["status"]).lower())
    total_km = sum(v.get("daily_km", 0) for v in vehicles)

    moving_vehicles  = [v for v in vehicles if "moving"  in str(v["status"]).lower()]
    stationary       = [v for v in vehicles if "stopped" in str(v["status"]).lower()]
    offline_vehicles = [v for v in vehicles if "offline" in str(v["status"]).lower()]
    moved_count      = sum(1 for v in vehicles
                           if v.get("daily_km", 0) > 0
                           or "moving" in str(v.get("status","")).lower())

    # ── MSG 1: Summary (matches GPS1 layout exactly) ─────────
    msg1 = (
        f":amber_alert: *MKV CAR RENTAL — GPS3: AmberConnect Daily Report*\n"
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
        f"_Generated at {datetime.now(DUBAI_OFFSET).strftime('%Y-%m-%d %H:%M')} (Dubai time)_"
    )

    # ── MSG 2: Vehicles That Moved (GPS1: Vehicle | Km Today | Zone)
    mov_table = "```\n"
    mov_table += f"{'Vehicle':<35} {'Km Today':>10} {'Zone':<20}\n"
    mov_table += "─" * 68 + "\n"
    for v in sorted(moving_vehicles, key=lambda x: x["name"]):
        dk     = v.get("daily_km", 0)
        km_str = f"{dk:.1f} km" if dk > 0 else "—"
        mov_table += f"{v['name'][:34]:<35} {km_str:>10} {'—':<20}\n"
    if not moving_vehicles:
        mov_table += f"{'No vehicles moving':<35}\n"
    mov_table += "```"
    msg2 = (
        f":blue_car: *GPS3 — Vehicles That Moved Today ({len(moving_vehicles)})*\n"
        f"{mov_table}"
    )

    # ── MSG 3: Parked Vehicles (GPS1: Vehicle | Parked (hrs) | Zone)
    park_table = "```\n"
    park_table += f"{'Vehicle':<35} {'Parked (hrs)':>12} {'Zone':<20}\n"
    park_table += "─" * 70 + "\n"
    for v in sorted(stationary, key=lambda x: x.get("duration_mins", 0), reverse=True):
        mins  = v.get("duration_mins", 0)
        p_str = f"{mins/60:.1f} hrs" if mins >= 60 else f"{mins} min"
        park_table += f"{v['name'][:34]:<35} {p_str:>12} {'—':<20}\n"
    if not stationary:
        park_table += f"{'No vehicles parked':<35}\n"
    park_table += "```"
    msg3 = (
        f":parking: *GPS3 — Parked Vehicles — Online ({len(stationary)})*\n"
        f"{park_table}\n"
        f"_:red_circle: {len(offline_vehicles)} vehicle(s) offline — excluded from list_"
    )

    # ── MSG 4: Speeding Alert (GPS1: Vehicle | Max Speed | Avg Speed | Trip km)
    speeding = [v for v in vehicles
                if float(str(v.get("speed_kmh", "0")).replace("—", "0") or 0) > 120]
    if speeding:
        spd_table = "```\n"
        spd_table += f"{'Vehicle':<35} {'Max Speed':>10} {'Avg Speed':>10} {'Trip km':>8}\n"
        spd_table += "─" * 68 + "\n"
        for v in sorted(speeding,
                         key=lambda x: float(str(x.get("speed_kmh","0")).replace("—","0") or 0),
                         reverse=True):
            spd = float(str(v.get("speed_kmh", "0")).replace("—", "0") or 0)
            avg = v.get("avg_speed", 0) or 0
            km  = v.get("daily_km",  0) or 0
            spd_table += f"{v['name'][:34]:<35} {spd:>8.0f} km {avg:>8.0f} km {km:>6.1f}km\n"
        spd_table += "```"
        msg4 = (
            f":rotating_light: *GPS3 — Speeding Alert — Today ({len(speeding)} vehicle(s) over 120 kph)*\n"
            f"{spd_table}"
        )
    else:
        msg4 = ":white_check_mark: *GPS3 — No speeding events recorded today*"

    # ── Terminal preview ─────────────────────────────────────
    print("\n" + "─"*60)
    print("  SLACK MESSAGE PREVIEW")
    print("─"*60)
    for i, m in enumerate([msg1, msg2, msg3, msg4], 1):
        print(f"\n── MSG {i} ──────────────────────────────────────")
        print(m)
    print("\n" + "─"*60)

    if DRY_RUN:
        print("  [DRY-RUN] Slack posting SKIPPED — messages shown above.")
        return

    try:
        hdrs = {"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"}
        for m in [msg1, msg2, msg3, msg4]:
            r = requests.post(
                "https://slack.com/api/chat.postMessage",
                headers=hdrs,
                json={"channel": SLACK_CHANNEL, "text": m},
                timeout=15,
            )
            if not r.json().get("ok"):
                print(f"  [Slack] Error: {r.json().get('error')}")
        print("  [Slack] Posted 4 messages to #daily-gps-update ✓")
    except Exception as e:
        print(f"  [Slack] Exception: {e}")


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    global DRY_RUN

    parser = argparse.ArgumentParser(description="MKV — GPS3: AmberConnect Daily Report")
    parser.add_argument("--date", help="YYYY-MM-DD (default: today)")
    parser.add_argument("--test", action="store_true",
                        help="Dry-run with mock vehicles — no live API calls, no Slack posting")
    args = parser.parse_args()

    DRY_RUN = args.test

    if args.date:
        report_date = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=DUBAI_OFFSET)
    else:
        report_date = datetime.now(DUBAI_OFFSET)
    report_date = report_date.replace(hour=0, minute=0, second=0, microsecond=0)
    date_str    = report_date.strftime("%Y-%m-%d")

    print(f"\n{'='*55}")
    print(f"  MKV CAR RENTAL — GPS3: AmberConnect Report")
    print(f"  Date   : {date_str}")
    if DRY_RUN:
        print(f"  MODE   : *** DRY-RUN / TEST (mock data, no Slack) ***")
    print(f"{'='*55}")

    if DRY_RUN:
        vehicles = get_mock_vehicles()
        print(f"  [TEST] Loaded {len(vehicles)} mock vehicles")
    else:
        snapshot = load_snapshot()
        vehicles = fetch_vehicles()
        if vehicles is None:
            # Session expired — alert already posted to Slack by fetch_vehicles()
            print("ERROR: Session expired. Slack alert sent. Update AMBER_SESSION in GitHub Secrets.")
            sys.exit(1)
        if not vehicles:
            print("ERROR: No vehicles returned from AmberConnect.")
            sys.exit(1)
        apply_snapshot_km(vehicles, snapshot)
        save_snapshot(vehicles, date_str)

    moving = [v for v in vehicles if "moving"  in str(v["status"]).lower()]
    parked = [v for v in vehicles if "stopped" in str(v["status"]).lower()]

    print(f"\n  {'Vehicle':<35} {'Status':<10} {'km Today':>8} {'Speed':>6}")
    print(f"  {'-'*65}")
    for v in vehicles:
        print(f"  {v['name']:<35} {v['status']:<10} "
              f"{v.get('daily_km', 0):>7.1f}  {v.get('speed_kmh','0'):>5} kph")
    print(f"\n  Moving : {len(moving)}  |  Parked : {len(parked)}")

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    build_summary(wb, vehicles, date_str)
    build_moving(wb, vehicles, date_str)
    build_parked(wb, vehicles, date_str)
    build_fleet(wb, vehicles, date_str)

    fname = f"MKV_GPS3_AMBER_{date_str}.xlsx"
    out   = os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)
    wb.save(out)

    print(f"\n{'='*55}")
    print(f"  Report saved : {fname}")
    print(f"  Tabs : GPS3 Summary | GPS3 Daily Movement | GPS3 Parked Vehicles | GPS3 Fleet Status")
    print(f"{'='*55}\n")

    post_to_slack(date_str, vehicles, fname)


if __name__ == "__main__":
    main()
