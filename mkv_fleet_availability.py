"""
MKV Luxury – Fleet Availability Daily Post
Fetches all vehicles from Appic get-all-vehicles.php and posts
a formatted availability snapshot to Slack.

Slack block text limit = 3000 chars. With 229 vehicles the list
is split across multiple blocks automatically.
"""

import os
import json
import requests
from datetime import datetime, timezone, timedelta

# ── Config ────────────────────────────────────────────────────────────────────
SLACK_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL = "C0AVCCCG0S0"   # #mkvtest — switch to live channel when ready
DUBAI_TZ      = timezone(timedelta(hours=4))
HEADERS       = {"User-Agent": "Mozilla/5.0 (MKV-Monitor/1.0)"}
BLOCK_CHAR_LIMIT = 2900          # Slack limit is 3000, use 2900 for safety

ALL_VEHICLES_URL = "https://www.appicfleet.com/appiccar-apis-mkv/get-all-vehicles.php"

CONTACT_FOOTER = (
    "📱 +971 52 940 9280\n"
    "📱 +971 56 279 4545\n"
    "☎️  +971 4 238 8987\n"
    "🌐 https://www.mkvluxury.com/\n"
    "📸 https://www.instagram.com/mkvluxurydubai/\n"
    "✉️  contact@mkvluxury.com\n"
    "📍 Al Jreena Street 41, Al Qouz Industrial Third, Dubai, UAE"
)

# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch_all_vehicles():
    try:
        r = requests.get(ALL_VEHICLES_URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
        print(f"Appic: issuccess={data.get('issuccess')} | vehicles={len(data.get('data', []))}")
        if not data.get("issuccess"):
            print(f"Appic error: {data.get('msg')}")
            return []
        return data.get("data", [])
    except Exception as e:
        print(f"Appic API error: {e}")
        return []

# ── Format vehicle name ───────────────────────────────────────────────────────

def format_vehicle_name(v: dict) -> str:
    make  = v.get("make", "").strip().upper()
    model = v.get("model", "").strip().upper()
    year  = str(v.get("year", "")).strip()
    color = v.get("color", "").strip().upper()
    plate = v.get("plate", "").strip()

    # Remove make prefix from model if duplicated
    if model.startswith(make):
        model = model[len(make):].strip()

    parts = []
    if make:  parts.append(make)
    if model: parts.append(model)
    if year and year not in ("0", ""):
        parts.append(f"({year})")
    if color and color not in ("", "N/A"):
        parts.append(color)
    if plate:
        parts.append(f"[{plate}]")

    return " ".join(parts) if parts else v.get("vehicle_name", "UNKNOWN")

# ── Split text into Slack-safe blocks ─────────────────────────────────────────

def text_to_blocks(text: str) -> list:
    """
    Split a long text string into multiple Slack section blocks,
    each within the 3000 char limit. Splits on newlines only.
    """
    blocks = []
    lines  = text.split("\n")
    chunk  = ""

    for line in lines:
        candidate = chunk + line + "\n"
        if len(candidate) > BLOCK_CHAR_LIMIT:
            if chunk:
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"```{chunk.rstrip()}```"}
                })
            chunk = line + "\n"
        else:
            chunk = candidate

    if chunk.strip():
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"```{chunk.rstrip()}```"}
        })

    return blocks

# ── Build Slack message ───────────────────────────────────────────────────────

def build_fleet_message(vehicles: list) -> dict:
    now      = datetime.now(DUBAI_TZ)
    date_str = now.strftime("%B %d, %Y").upper()
    day_str  = now.strftime("%A").upper()
    total    = len(vehicles)

    # Available = has availability=Available AND dailyrent > 0
    available     = [v for v in vehicles
                     if v.get("availability", "").lower() == "available"
                     and float(v.get("dailyrent", 0) or 0) > 0]
    not_available = [v for v in vehicles
                     if v.get("availability", "").lower() != "available"
                     or float(v.get("dailyrent", 0) or 0) == 0]

    # employeeAssigned=true → likely rented/out; else NRV/inactive
    rented = [v for v in not_available if v.get("employeeAssigned") == "true"]
    nrv    = [v for v in not_available if v.get("employeeAssigned") != "true"]

    # ── Summary block (always fits in one block) ──
    summary_lines = [
        f"❝{date_str} {day_str}❞",
        "",
        f"✦ Total : {total}",
        "",
        f"✦ Rented STR : {len(rented)}",
        "",
        f"✦ Garage : 0",
        "",
        f"✦ Service : 0",
        "",
        f"✦ Available : {len(available)}",
        "",
        f"✦ Lease : 0",
        "",
        f"✦ Longterm : 0",
        "",
        f"✦ NRV : {len(nrv)}",
    ]
    summary_text = "\n".join(summary_lines)

    # ── Vehicle list ──
    vehicle_lines = []
    for i, v in enumerate(available, 1):
        vehicle_lines.append(f"{i}. {format_vehicle_name(v)}")
    vehicle_lines.append("")
    vehicle_lines.append("For inquiries please contact this number")
    vehicle_lines.append("")
    vehicle_lines.append(CONTACT_FOOTER)
    vehicle_text = "\n".join(vehicle_lines)

    # ── Assemble blocks ──
    blocks = []

    # Summary (single block — always short)
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"```{summary_text}```"}
    })
    blocks.append({"type": "divider"})

    # Vehicle list split across as many blocks as needed
    blocks.extend(text_to_blocks(vehicle_text))

    # Footer context
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn",
                      "text": "MKV Fleet Availability • Auto-posted daily at 10:00 AM Dubai time"}]
    })

    return {"blocks": blocks, "text": f"MKV Fleet Availability — {date_str} {day_str}"}

# ── Slack post ────────────────────────────────────────────────────────────────

def post_to_slack(message: dict):
    payload = {"channel": SLACK_CHANNEL, **message}
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}",
                 "Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=15,
    )
    result = r.json()
    if not result.get("ok"):
        print(f"Slack error: {result.get('error')}")
        # Print blocks for debugging
        print("Blocks sent:")
        for i, b in enumerate(message.get("blocks", [])):
            txt = b.get("text", {}).get("text", "")
            print(f"  Block {i}: {len(txt)} chars")
        raise SystemExit(1)
    print("✅ Fleet availability posted to Slack successfully.")

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Fetching MKV fleet from Appic...")
    vehicles = fetch_all_vehicles()

    if not vehicles:
        print("⚠️  No vehicles returned.")
        now = datetime.now(DUBAI_TZ)
        warn = {
            "blocks": [{"type": "section", "text": {"type": "mrkdwn",
                "text": f"⚠️ *MKV Fleet Post — {now.strftime('%B %d, %Y').upper()}*\nNo fleet data from Appic. Check API connection."}}],
            "text": "MKV Fleet — API Warning"
        }
        post_to_slack(warn)
        raise SystemExit(1)

    message = build_fleet_message(vehicles)
    print(f"Blocks generated: {len(message['blocks'])}")
    post_to_slack(message)
