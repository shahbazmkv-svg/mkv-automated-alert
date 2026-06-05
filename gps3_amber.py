"""
MKV CAR RENTAL - GPS3: AmberConnect Daily Report
Output: MKV_GPS3_AMBER_YYYY-MM-DD.xlsx + Slack post
Usage:
    python gps3_amber.py
    python gps3_amber.py --date 2026-06-04
"""

import requests, argparse, os, sys, time, re
import json as _json, sqlite3, shutil, tempfile
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

AMBER_BASE    = "https://fleet.amberconnect.ai"
AMBER_FLEET   = os.getenv("AMBER_FLEET_ID", "0370168")
AMBER_USER    = os.getenv("AMBER_USER",     "muneer-fleet@mkvluxury.com")
AMBER_PASS    = os.getenv("AMBER_PASS",     "Mkv12340@")
AMBER_SESSION = os.getenv("AMBER_SESSION",  "")
SLACK_TOKEN   = os.getenv("SLACK_TOKEN",    "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL",  "C0B6Y6EG85D")
TIMEZONE      = "Asia/Dubai"
DUBAI_OFFSET  = timezone(timedelta(hours=4))

UA = {
    "User-Agent":       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer":          f"{AMBER_BASE}/device-management",
    "X-Requested-With": "XMLHttpRequest",
}

ORANGE = "E8520A"; DARK = "1A1A2E"; WHITE = "FFFFFF"
LIGHT_ORG = "FEF0E7"; SEP = "FDE8D8"
GREEN = "2E6B4F"; GOLD = "C9A84C"; GREY = "999999"

def fwhite(bold=True, sz=11): return Font(name="Arial", bold=bold, color=WHITE, size=sz)
def fdark(bold=False, sz=10): return Font(name="Arial", bold=bold, color=DARK, size=sz)
def fill(color): return PatternFill("solid", fgColor=color)
def thin_border():
    s = Side(style="thin", color="CCCCCC")
    return Border(left=s, right=s, top=s, bottom=s)
def center(): return Alignment(horizontal="center", vertical="center", wrap_text=True)
def left_align(): return Alignment(horizontal="left", vertical="center", wrap_text=True)
def fmt_duration(mins):
    return f"{mins} min" if mins < 60 else f"{mins//60}h {mins%60}m"


# ── Session cookie ─────────────────────────────────────────────
def get_session_cookie():
    cookie_paths = [
        os.path.expanduser(r"~\AppData\Local\Google\Chrome\User Data\Default\Network\Cookies"),
        os.path.expanduser(r"~\AppData\Local\Google\Chrome\User Data\Default\Cookies"),
    ]
    for cp in cookie_paths:
        if not os.path.exists(cp):
            continue
        for attempt in range(3):
            tmp = None
            try:
                tmp = tempfile.mktemp(suffix=".db")
                shutil.copy2(cp, tmp)
                conn = sqlite3.connect(tmp)
                cur  = conn.cursor()
                cur.execute("""SELECT value FROM cookies
                    WHERE host_key LIKE '%amberconnect%' AND name = 'PHPSESSID'
                    ORDER BY last_access_utc DESC LIMIT 1""")
                row = cur.fetchone()
                conn.close()
                if row and row[0]:
                    print("  [GPS3] Cookie auto-read from Chrome")
                    return row[0]
                break
            except Exception:
                time.sleep(1)
            finally:
                if tmp and os.path.exists(tmp):
                    try: os.unlink(tmp)
                    except: pass
    if AMBER_SESSION:
        print("  [GPS3] Using cookie from .env")
        return AMBER_SESSION
    print("  [GPS3] ERROR: No session cookie. Log into AmberConnect in Chrome first.")
    return None

def make_session(cookie):
    s = requests.Session()
    s.headers.update(UA)
    s.cookies.set("PHPSESSID", cookie, domain="fleet.amberconnect.ai")
    return s


# ── Snapshot ───────────────────────────────────────────────────
SNAPSHOT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gps3_amber_snapshot.json")

def load_snapshot():
    try:
        if os.path.exists(SNAPSHOT_FILE):
            with open(SNAPSHOT_FILE) as f: return _json.load(f)
    except: pass
    return {}

def save_snapshot(vehicles, date_str):
    snap = {"date": date_str, "odometers": {v["device_id"]: v.get("odometer_km", 0) for v in vehicles if v.get("device_id")}}
    try:
        with open(SNAPSHOT_FILE, "w") as f: _json.dump(snap, f)
        print(f"  [GPS3] Snapshot saved ({len(snap['odometers'])} vehicles)")
    except Exception as e: print(f"  [GPS3] Snapshot error: {e}")

def apply_snapshot_km(vehicles, snapshot):
    today = datetime.now(DUBAI_OFFSET).strftime("%Y-%m-%d")
    yesterday = (datetime.now(DUBAI_OFFSET) - timedelta(days=1)).strftime("%Y-%m-%d")
    if snapshot.get("date") not in (today, yesterday):
        print("  [GPS3] No valid snapshot — km shows 0 today"); return
    for v in vehicles:
        prev = snapshot.get("odometers", {}).get(v.get("device_id",""))
        curr = v.get("odometer_km", 0)
        if prev is not None and curr > 0:
            v["daily_km"] = max(0.0, round(curr - prev, 1))


# ── Fetch data ─────────────────────────────────────────────────
def fetch_vehicles(session):
    r = session.post(f"{AMBER_BASE}/ajax/birdsEye",
                     data={"choosen_lang": "en", "timezone": "Asia/Dubai"}, timeout=20)
    print(f"  [GPS3] birdsEye: {r.status_code} len={len(r.text)}")
    if r.status_code != 200 or r.text.strip().startswith("<!"):
        print("  [GPS3] Session expired — update AMBER_SESSION in .env"); return []
    try: data = r.json()
    except Exception as e: print(f"  [GPS3] JSON error: {e}"); return []

    vehicles = []
    for d in data.get("devices", []):
        if not isinstance(d, list) or len(d) < 7: continue
        speed  = float(d[7]) if len(d) > 7 else 0
        engine = str(d[6]).upper()
        odo    = float(d[14]) if len(d) > 14 else 0
        status = "Moving" if speed > 0 else ("Stopped" if engine == "ON" else "Offline")
        vehicles.append({
            "device_id":   str(d[2]), "name": str(d[5]), "plate": "",
            "status":      status,    "engine": engine,
            "speed_kmh":   str(int(speed)),
            "lat": str(d[0]),         "lng": str(d[1]),
            "odometer_km": round(odo, 1),
            "daily_km":    0.0,       "duration_mins": 0,
            "last_update": datetime.now(DUBAI_OFFSET).strftime("%Y-%m-%d %H:%M"),
        })
    print(f"  [GPS3] {len(vehicles)} vehicles")
    return vehicles


# ── Excel ──────────────────────────────────────────────────────
def _title(ws, title, date_str, n):
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=n)
    t = ws.cell(1,1); t.value = f"MKV CAR RENTAL  -  GPS3: AMBERCONNECT  -  {title}"
    t.font = fwhite(bold=True, sz=14); t.fill = fill(ORANGE); t.alignment = center()
    ws.row_dimensions[1].height = 30
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=n)
    s = ws.cell(2,1)
    s.value = f"Report Date: {date_str}  |  Source: fleet.amberconnect.ai  |  Generated: {datetime.now(DUBAI_OFFSET).strftime('%Y-%m-%d %H:%M')} (Dubai)"
    s.font = fwhite(bold=False, sz=9); s.fill = fill(DARK); s.alignment = center()
    ws.row_dimensions[2].height = 18

def _sep(ws, text, n, row=3):
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=n)
    c = ws.cell(row,1); c.value = text
    c.font = Font(name="Arial", bold=True, size=11, color=ORANGE)
    c.fill = fill(SEP); c.alignment = left_align(); ws.row_dimensions[row].height = 20

def _hdrs(ws, headers, widths, row=4):
    ws.append(headers)
    for col in range(1, len(headers)+1):
        c = ws.cell(row,col); c.font = fwhite(bold=True, sz=11)
        c.fill = fill(ORANGE); c.alignment = center(); c.border = thin_border()
    ws.row_dimensions[row].height = 22
    for i, w in enumerate(widths, 1): ws.column_dimensions[get_column_letter(i)].width = w

def _row_style(ws, rn, n, alt=False):
    for col in range(1, n+1):
        c = ws.cell(rn, col); c.font = fdark(sz=10)
        c.border = thin_border(); c.alignment = left_align()
        if alt: c.fill = fill(LIGHT_ORG)

def _footer(ws, rn, n, label="TOTAL", label2=""):
    ws.cell(rn,1).value = label; ws.cell(rn,2).value = label2
    for col in range(1, n+1):
        c = ws.cell(rn,col); c.font = fwhite(bold=True, sz=10)
        c.fill = fill(ORANGE); c.border = thin_border(); c.alignment = center()

def build_summary(wb, vehicles, date_str):
    ws = wb.create_sheet("GPS3 Summary", 0)
    ws.sheet_view.showGridLines = False
    for col, w in [("A",3),("B",32),("C",22)]: ws.column_dimensions[col].width = w
    _title(ws, "DAILY SUMMARY", date_str, 3)
    total   = len(vehicles)
    moving  = sum(1 for v in vehicles if v["status"]=="Moving")
    offline = sum(1 for v in vehicles if v["status"]=="Offline")
    total_km = sum(v.get("daily_km",0) for v in vehicles)
    ws["B4"] = "Fleet Summary  -  GPS3: AmberConnect"
    ws["B4"].font = Font(name="Arial", bold=True, size=12, color=ORANGE)
    for ri, (label, val) in enumerate([
        ("Total Vehicles", total), ("Online", total-offline), ("Offline", offline),
        ("Moving Now", f"{moving} vehicles"), ("Parked/Stopped", f"{total-moving-offline} vehicles"),
        ("Total Km Today", f"{total_km:,.1f} km"), ("Report Date", date_str),
        ("Source", "AmberConnect GPS (fleet.amberconnect.ai)"),
        ("Generated At", datetime.now(DUBAI_OFFSET).strftime("%Y-%m-%d %H:%M")),
        ("Timezone", TIMEZONE),
    ], 5):
        ws.cell(ri,2).value = label; ws.cell(ri,2).font = Font(name="Arial", bold=True, size=10)
        ws.cell(ri,2).fill = fill(SEP); ws.cell(ri,3).value = str(val)
        ws.cell(ri,3).font = Font(name="Arial", size=10); ws.row_dimensions[ri].height = 20

def build_movement(wb, vehicles, date_str):
    ws = wb.create_sheet("GPS3 Daily Movement")
    hdrs = ["#","Vehicle Name","Plate/ID","Device ID","Moved Today","Status","Speed (km/h)","Km Today","Last Update"]
    wids = [5,30,14,16,14,12,13,12,22]; n = len(hdrs)
    _title(ws, "DAILY MOVEMENT", date_str, n)
    moved_n = sum(1 for v in vehicles if v.get("daily_km",0)>0 or v["status"]=="Moving")
    _sep(ws, f"GPS3: AmberConnect  -  {moved_n} vehicle(s) moved today", n)
    _hdrs(ws, hdrs, wids)
    ds = 5
    for i, v in enumerate(vehicles, 1):
        rn = ds+i-1; sl = v["status"].lower(); dk = v.get("daily_km",0)
        moved = dk>0 or v["status"]=="Moving"; alt = (i%2==0)
        ws.cell(rn,1).value=i; ws.cell(rn,2).value=v["name"]; ws.cell(rn,3).value=v["plate"]
        ws.cell(rn,4).value=v["device_id"]; ws.cell(rn,5).value="Yes" if moved else "No"
        ws.cell(rn,6).value=v["status"]; ws.cell(rn,7).value=v["speed_kmh"]
        ws.cell(rn,8).value=f"{dk:.1f}" if dk>0 else "-"; ws.cell(rn,9).value=v["last_update"]
        _row_style(ws,rn,n,alt)
        ws.cell(rn,5).font=Font(name="Arial",bold=True,size=10,color=GREEN if moved else GREY)
        sc=ws.cell(rn,6); sc.font=Font(name="Arial",bold=True,size=10,
            color=GREEN if "moving" in sl else GREY if "offline" in sl else GOLD)
    ws.freeze_panes="A5"; _footer(ws,ds+len(vehicles),n,"TOTAL",f"{moved_n} moved")

def build_parked(wb, vehicles, date_str):
    ws = wb.create_sheet("GPS3 Parked Vehicles")
    parked = [v for v in vehicles if v["status"]!="Moving"]
    hdrs = ["#","Vehicle Name","Plate/ID","Device ID","Status","Engine","Parked (hrs)","Online","Last Update"]
    wids = [5,30,14,16,12,10,14,10,22]; n = len(hdrs)
    _title(ws, "PARKED VEHICLES", date_str, n)
    _sep(ws, f"GPS3: AmberConnect  -  {len(parked)} vehicle(s) parked/stopped", n)
    _hdrs(ws, hdrs, wids)
    ds = 5
    for i, v in enumerate(sorted(parked, key=lambda x: x.get("duration_mins",0), reverse=True), 1):
        rn=ds+i-1; offline=v["status"]=="Offline"; mins=v.get("duration_mins",0)
        p_hrs=f"{mins/60:.1f} hrs" if mins>=60 else f"{mins} min"; alt=(i%2==0)
        ws.cell(rn,1).value=i; ws.cell(rn,2).value=v["name"]; ws.cell(rn,3).value=v["plate"]
        ws.cell(rn,4).value=v["device_id"]; ws.cell(rn,5).value=v["status"]
        ws.cell(rn,6).value=v.get("engine","-"); ws.cell(rn,7).value=p_hrs
        ws.cell(rn,8).value="Offline" if offline else "Online"; ws.cell(rn,9).value=v["last_update"]
        _row_style(ws,rn,n,alt)
        cc = GREY if offline else GOLD
        ws.cell(rn,5).font=Font(name="Arial",bold=True,size=10,color=cc)
        ws.cell(rn,7).font=Font(name="Arial",bold=True,size=10,color=cc)
        ws.cell(rn,8).font=Font(name="Arial",bold=True,size=10,color=GREY if offline else GREEN)
    ws.freeze_panes="A5"; _footer(ws,ds+len(parked),n,"TOTAL",f"{len(parked)} parked")

def build_fleet(wb, vehicles, date_str):
    ws = wb.create_sheet("GPS3 Fleet Status")
    hdrs = ["#","Vehicle Name","Plate/ID","Device ID","Status","Engine","Speed (km/h)","Km Today","Odometer (km)","Last Update"]
    wids = [5,30,14,16,12,10,13,12,16,22]; n = len(hdrs)
    _title(ws, "FLEET STATUS", date_str, n)
    _sep(ws, f"GPS3: AmberConnect  -  All {len(vehicles)} Vehicles", n)
    _hdrs(ws, hdrs, wids)
    ds = 5
    for i, v in enumerate(vehicles, 1):
        rn=ds+i-1; sl=v["status"].lower(); dk=v.get("daily_km",0); alt=(i%2==0)
        ws.cell(rn,1).value=i; ws.cell(rn,2).value=v["name"]; ws.cell(rn,3).value=v["plate"]
        ws.cell(rn,4).value=v["device_id"]; ws.cell(rn,5).value=v["status"]
        ws.cell(rn,6).value=v.get("engine","-"); ws.cell(rn,7).value=v["speed_kmh"]
        ws.cell(rn,8).value=f"{dk:.1f}" if dk>0 else "-"
        ws.cell(rn,9).value=v.get("odometer_km",""); ws.cell(rn,10).value=v["last_update"]
        _row_style(ws,rn,n,alt)
        ws.cell(rn,5).font=Font(name="Arial",bold=True,size=10,
            color=GREEN if "moving" in sl else GREY if "offline" in sl else GOLD)
    ws.freeze_panes="A5"; _footer(ws,ds+len(vehicles),n,"TOTAL",f"{len(vehicles)} vehicles")


# ── Slack ──────────────────────────────────────────────────────
def post_to_slack(date_str, vehicles, fname):
    if not SLACK_TOKEN: return
    total=len(vehicles); moving=[v for v in vehicles if v["status"]=="Moving"]
    offline=[v for v in vehicles if v["status"]=="Offline"]
    stationary=[v for v in vehicles if v["status"]=="Stopped"]
    total_km=sum(v.get("daily_km",0) for v in vehicles)
    msg1=(f":car: *MKV CAR RENTAL - GPS3: AmberConnect Daily Report*\n"
          f"*Date:* {date_str}  |  *Timezone:* {TIMEZONE}\n"
          f"------------------------------\n"
          f":white_check_mark: *Online:* {total-len(offline)} / {total}\n"
          f":red_circle: *Offline:* {len(offline)}\n"
          f":blue_car: *Moving Now:* {len(moving)}\n"
          f":round_pushpin: *Total Km Today:* {total_km:,.1f} km\n"
          f"------------------------------\n"
          f":page_facing_up: *File:* `{fname}`\n"
          f"_Generated at {datetime.now(DUBAI_OFFSET).strftime('%Y-%m-%d %H:%M')} (Dubai time)_")
    t2="```\n"+f"{'Vehicle':<35} {'Km Today':>10} {'Zone':<20}\n"+"- "*34+"\n"
    for v in sorted(moving, key=lambda x: x["name"]):
        dk=v.get("daily_km",0); km=f"{dk:.1f} km" if dk>0 else "-"
        t2+=f"{v['name'][:34]:<35} {km:>10} {'-':<20}\n"
    t2+="```"; msg2=f":blue_car: *GPS3 - Vehicles That Moved Today ({len(moving)})*\n{t2}"
    t3="```\n"+f"{'Vehicle':<35} {'Parked (hrs)':>12} {'Zone':<20}\n"+"- "*34+"\n"
    for v in sorted(stationary, key=lambda x: x.get("duration_mins",0), reverse=True):
        mins=v.get("duration_mins",0); p=f"{mins/60:.1f} hrs" if mins>=60 else f"{mins} min"
        t3+=f"{v['name'][:34]:<35} {p:>12} {'-':<20}\n"
    t3+="```"; msg3=(f":parking: *GPS3 - Parked Vehicles ({len(stationary)})*\n{t3}\n"
                     f"_:red_circle: {len(offline)} offline_")
    speeding=[v for v in vehicles if float(v.get("speed_kmh","0") or 0)>120]
    msg4=(":white_check_mark: *GPS3 - No speeding events*" if not speeding else
          f":rotating_light: *GPS3 - Speeding Alert ({len(speeding)} vehicles over 120 kph)*")
    hdrs={"Authorization":f"Bearer {SLACK_TOKEN}","Content-Type":"application/json"}
    for m in [msg1,msg2,msg3,msg4]:
        r=requests.post("https://slack.com/api/chat.postMessage",headers=hdrs,
                        json={"channel":SLACK_CHANNEL,"text":m},timeout=15)
        if not r.json().get("ok"): print(f"  [Slack] {r.json().get('error')}")
    print("  [Slack] Posted 4 messages")


# ── Main ───────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD (default: yesterday)")
    args = parser.parse_args()
    if args.date:
        report_date = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=DUBAI_OFFSET)
    else:
        report_date = datetime.now(DUBAI_OFFSET) - timedelta(days=1)
    report_date = report_date.replace(hour=0, minute=0, second=0, microsecond=0)
    date_str = report_date.strftime("%Y-%m-%d")

    print(f"\n{'='*55}\n  MKV - GPS3: AmberConnect Report\n  Date: {date_str}\n{'='*55}")

    cookie = get_session_cookie()
    if not cookie: sys.exit(1)
    session  = make_session(cookie)
    snapshot = load_snapshot()
    vehicles = fetch_vehicles(session)
    if not vehicles:
        print("ERROR: No vehicles — session may be expired."); sys.exit(1)

    apply_snapshot_km(vehicles, snapshot)
    save_snapshot(vehicles, date_str)

    for v in vehicles:
        print(f"  {v['name']:<35} {v['status']:<12} {v['speed_kmh']:>5} kph")

    wb = openpyxl.Workbook(); wb.remove(wb.active)
    build_summary(wb, vehicles, date_str)
    build_movement(wb, vehicles, date_str)
    build_parked(wb, vehicles, date_str)
    build_fleet(wb, vehicles, date_str)

    fname = f"MKV_GPS3_AMBER_{date_str}.xlsx"
    out   = os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)
    wb.save(out)
    print(f"\n  Report saved: {fname}\n{'='*55}\n")
    post_to_slack(date_str, vehicles, fname)

if __name__ == "__main__":
    main()
