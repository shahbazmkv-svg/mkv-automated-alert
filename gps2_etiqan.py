"""
MKV CAR RENTAL - GPS2: AL ETIQAN Daily Report (SIMPLIFIED)
Uses vehicle order from API as identifier - no device_id parsing needed
"""

import requests
import json
import os
import sys
import argparse
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

# ─── CONFIG ─────────────────────────────────────────
ETIQAN_BASE   = "http://track.etqanuae.com/api/api.php"
ETIQAN_KEY    = "8651C0D3A56F60178A09B3007B8BF32B"
SLACK_TOKEN   = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "C0B6Y6EG85D")
SNAPSHOT_FILE = "gps2_etiqan_snapshot.json"
DUBAI_OFFSET  = timezone(timedelta(hours=4))
TIMEZONE_NAME = "Asia/Dubai"

# ─── FETCH VEHICLES ─────────────────────────────────
def fetch_vehicles():
    """Get vehicles from ETIQAN API."""
    params = {
        "api": "user", "ver": "1.0", "key": ETIQAN_KEY,
        "cmd": "USER_GET_OBJECTS_STATUS",
    }
    try:
        r = requests.get(ETIQAN_BASE, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  [GPS2] API error: {e}")
        return []
    
    if not isinstance(data, list):
        print(f"  [GPS2] Unexpected response format")
        return []
    
    vehicles = []
    for obj in data:
        name = str(obj.get("name", "Unknown"))
        status_raw = str(obj.get("status", "")).lower()
        
        if "moving" in status_raw:
            status = "Moving"
        elif "stopped" in status_raw or "parked" in status_raw:
            status = "Stopped"
        else:
            status = "Offline"
        
        try:
            speed_kmh = float(obj.get("speed", 0))
        except:
            speed_kmh = 0
        
        try:
            odometer_km = float(obj.get("odometer", 0))
        except:
            odometer_km = 0
        
        vehicles.append({
            "name": name,
            "status": status,
            "speed_kmh": speed_kmh,
            "odometer_km": odometer_km,
            "daily_km": 0.0,
            "last_update": obj.get("dt_tracker", ""),
        })
        print(f"    {name}: {status} | speed={speed_kmh} | odo={odometer_km:.1f} km")
    
    print(f"  [GPS2] Fetched {len(vehicles)} vehicles")
    return vehicles


# ─── SNAPSHOT (uses vehicle NAME as key - safest) ────
def load_snapshot():
    """Load yesterday's snapshot."""
    try:
        with open(SNAPSHOT_FILE, "r") as f:
            return json.load(f)
    except:
        return {}


def save_snapshot(vehicles, date_str):
    """Save today's snapshot - using full vehicle name."""
    snap = {
        "date": date_str,
        "odometers": {v["name"]: v["odometer_km"] for v in vehicles}
    }
    try:
        with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump(snap, f, indent=2, ensure_ascii=False)
        print(f"  [GPS2] Snapshot saved with {len(snap['odometers'])} vehicles")
    except Exception as e:
        print(f"  [GPS2] Save error: {e}")


def calculate_deltas(vehicles, snapshot):
    """Calculate daily_km from odometer delta - simple name matching."""
    snap_date = snapshot.get("date", "")
    snap_odos = snapshot.get("odometers", {})
    
    today = datetime.now(DUBAI_OFFSET).strftime("%Y-%m-%d")
    yesterday = (datetime.now(DUBAI_OFFSET) - timedelta(days=1)).strftime("%Y-%m-%d")
    
    if snap_date not in (today, yesterday):
        print(f"  [GPS2] Snapshot date '{snap_date}' not today/yesterday - skipping")
        return
    
    print(f"  [GPS2] Computing deltas (snapshot: {snap_date})")
    for v in vehicles:
        name = v["name"]
        curr = v["odometer_km"]
        prev = snap_odos.get(name)
        
        if prev is not None and curr > 0:
            v["daily_km"] = max(0.0, round(curr - prev, 1))
            print(f"    {name}: {curr:.1f} - {prev:.1f} = {v['daily_km']:.1f} km")
        else:
            print(f"    {name}: NO MATCH (prev={prev})")


# ─── SLACK MESSAGES ─────────────────────────────────
def build_slack_messages(vehicles, date_str):
    """Build the 4 Slack messages."""
    total_km = sum(v["daily_km"] for v in vehicles)
    moved = [v for v in vehicles if v["daily_km"] > 0]
    parked = [v for v in vehicles if v["daily_km"] == 0 and v["status"] != "Offline"]
    online = sum(1 for v in vehicles if v["status"] != "Offline")
    offline = len(vehicles) - online
    moving_now = sum(1 for v in vehicles if v["status"] == "Moving")
    speeding = [v for v in vehicles if v["speed_kmh"] > 120]
    
    now = datetime.now(DUBAI_OFFSET).strftime("%Y-%m-%d %H:%M")
    
    # MSG 1
    msg1 = (
        f":etiqan: *MKV CAR RENTAL — GPS2: AL ETIQAN Daily Report*\n"
        f"*Date:* {date_str}  |  *Timezone:* {TIMEZONE_NAME}\n"
        f"─────────────────────────────\n"
        f":white_check_mark: *Online:* {online} / {len(vehicles)}\n"
        f":red_circle: *Offline:* {offline}\n"
        f":blue_car: *Moving Now:* {moving_now}\n"
        f":chart_with_upwards_trend: *Moved Today:* {len(moved)} vehicles\n"
        f":round_pushpin: *Total Km Driven Today:* {total_km:.1f} km\n"
        f":busts_in_silhouette: *In a Zone:* —\n"
        f":lock: *Engine Blocked:* —\n"
        f"─────────────────────────────\n"
        f":page_facing_up: *File:* `MKV_GPS2_ETIQAN_{date_str}.xlsx`\n"
        f"_Generated at {now} (Dubai time)_"
    )
    
    # MSG 2 - Vehicles Moved
    if moved:
        rows = "\n".join(f"{v['name']:<40} {v['daily_km']:>6.1f} km   —" for v in moved)
    else:
        rows = "No vehicles moving"
    
    msg2 = (
        f":blue_car: *GPS2 — Vehicles That Moved Today ({len(moved)})*\n"
        f"```\n"
        f"Vehicle                               Km Today  Zone\n"
        f"────────────────────────────────────────────────────────────────────\n"
        f"{rows}\n"
        f"```"
    )
    
    # MSG 3 - Parked
    if parked:
        rows = "\n".join(f"{v['name']:<40} {'0 min':>10}   —" for v in parked)
    else:
        rows = "No parked vehicles"
    
    msg3 = (
        f":parking: *GPS2 — Parked Vehicles — Online ({len(parked)})*\n"
        f"```\n"
        f"Vehicle                             Parked (hrs)  Zone\n"
        f"──────────────────────────────────────────────────────────────────────\n"
        f"{rows}\n"
        f"```\n"
        f"_:red_circle: {offline} vehicle(s) offline — excluded from list_"
    )
    
    # MSG 4 - Speeding
    if speeding:
        rows = "\n".join(f"{v['name']:<40} {v['speed_kmh']:>5} km/h" for v in speeding)
        msg4 = (
            f":rotating_light: *GPS2 — Speeding Alert ({len(speeding)} vehicles >120 km/h)*\n"
            f"```\n{rows}\n```"
        )
    else:
        msg4 = ":white_check_mark: *GPS2 — No speeding events recorded today*"
    
    return [msg1, msg2, msg3, msg4]


def post_to_slack(messages, dry_run=False):
    """Post messages to Slack."""
    print("\n" + "─" * 60)
    print("  SLACK MESSAGE PREVIEW")
    print("─" * 60)
    for i, m in enumerate(messages, 1):
        print(f"\n── MSG {i} ──────────────────────────")
        print(m)
    print("\n" + "─" * 60)
    
    if dry_run:
        print("  [DRY-RUN] Slack posting SKIPPED")
        return
    
    if not SLACK_TOKEN:
        print("  [GPS2] No SLACK_TOKEN, skipping Slack post")
        return
    
    headers = {"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"}
    for i, m in enumerate(messages, 1):
        try:
            r = requests.post(
                "https://slack.com/api/chat.postMessage",
                headers=headers,
                json={"channel": SLACK_CHANNEL, "text": m},
                timeout=15,
            )
            if r.json().get("ok"):
                print(f"  [Slack] MSG{i} posted ✓")
            else:
                print(f"  [Slack] MSG{i} error: {r.json().get('error')}")
        except Exception as e:
            print(f"  [Slack] MSG{i} exception: {e}")


# ─── EXCEL ──────────────────────────────────────────
def build_excel(vehicles, date_str):
    """Generate Excel workbook."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "GPS2 Fleet Status"
    
    headers = ["Vehicle", "Status", "Speed (km/h)", "Odometer (km)", "Daily Km", "Last Update"]
    for col, h in enumerate(headers, 1):
        c = ws.cell(1, col, h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1B2A5A")
    
    for row, v in enumerate(vehicles, 2):
        ws.cell(row, 1, v["name"])
        ws.cell(row, 2, v["status"])
        ws.cell(row, 3, v["speed_kmh"])
        ws.cell(row, 4, v["odometer_km"])
        ws.cell(row, 5, v["daily_km"])
        ws.cell(row, 6, v["last_update"])
    
    for col in range(1, 7):
        ws.column_dimensions[chr(64 + col)].width = 25
    
    fname = f"MKV_GPS2_ETIQAN_{date_str}.xlsx"
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)
    wb.save(out)
    return fname


# ─── MAIN ───────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="Test mode")
    args = parser.parse_args()
    
    if args.date:
        date_obj = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=DUBAI_OFFSET)
    else:
        date_obj = datetime.now(DUBAI_OFFSET)
    date_str = date_obj.strftime("%Y-%m-%d")
    
    print("\n" + "=" * 60)
    print(f"  MKV CAR RENTAL - GPS2: AL ETIQAN Report")
    print(f"  Date: {date_str}")
    if args.dry_run:
        print(f"  MODE: DRY-RUN (no Slack/save)")
    print("=" * 60)
    
    # Step 1: Fetch
    print("\n[1] Fetching vehicles...")
    vehicles = fetch_vehicles()
    if not vehicles:
        print("ERROR: No vehicles fetched")
        sys.exit(1)
    
    # Step 2: Load snapshot & calculate deltas
    print("\n[2] Loading snapshot...")
    snapshot = load_snapshot()
    if snapshot:
        print(f"  Snapshot date: {snapshot.get('date')}")
        print(f"  Snapshot keys: {list(snapshot.get('odometers', {}).keys())}")
        calculate_deltas(vehicles, snapshot)
    else:
        print("  No previous snapshot found")
    
    # Step 3: Build Slack messages
    print("\n[3] Building Slack messages...")
    messages = build_slack_messages(vehicles, date_str)
    
    # Step 4: Post to Slack
    post_to_slack(messages, dry_run=args.dry_run)
    
    # Step 5: Save snapshot & Excel (skip in dry-run)
    if not args.dry_run:
        print("\n[5] Saving snapshot & Excel...")
        save_snapshot(vehicles, date_str)
        fname = build_excel(vehicles, date_str)
        print(f"  Excel saved: {fname}")
    else:
        print("\n[5] Dry-run: snapshot & Excel SKIPPED")
        print(f"  Would save snapshot with these keys:")
        for v in vehicles:
            print(f"    '{v['name']}': {v['odometer_km']}")
    
    print("\n" + "=" * 60)
    print("  ✓ Report complete")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
