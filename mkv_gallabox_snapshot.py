import requests
import json
import os
from datetime import datetime, timedelta
import pytz

# ─────────────────────────────────────────────
#  CONFIG — all values injected via GitHub Secrets
# ─────────────────────────────────────────────
GALLABOX_API_KEY    = os.environ["GALLABOX_API_KEY"]
GALLABOX_API_SECRET = os.environ["GALLABOX_API_SECRET"]
GALLABOX_ACCOUNT_ID = os.environ["GALLABOX_ACCOUNT_ID"]
WEBHOOK_LEADS       = os.environ["WEBHOOK_LEADS"]

GALLABOX_BASE_URL   = "https://server.gallabox.com/devapi"

CHANNELS = {
    "Main":        "675a90ddda3020e52915beff",
    "Car Rental":  "66e930025e9ef7252ccc8a25",
    "Rent to Own": "699d8cca452cc56936e21e45",
}

STORE_FILE  = "mtd_store.json"
DUBAI_TZ    = pytz.timezone("Asia/Dubai")

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def dubai_now():
    return datetime.now(DUBAI_TZ)

def today_str():
    return dubai_now().strftime("%Y-%m-%d")

def month_start_str():
    d = dubai_now()
    return d.replace(day=1).strftime("%Y-%m-%d")

def month_label():
    return dubai_now().strftime("%B %Y")

def load_store():
    if os.path.exists(STORE_FILE):
        try:
            with open(STORE_FILE) as f:
                return json.load(f)
        except:
            pass
    return {}

def save_store(data):
    with open(STORE_FILE, "w") as f:
        json.dump(data, f, indent=2)

def post_slack(webhook, payload):
    try:
        r = requests.post(webhook, json=payload, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"  ⚠ Slack error: {e}")
        return False

# ─────────────────────────────────────────────
#  GALLABOX API
# ─────────────────────────────────────────────
def fetch_contacts(channel_id, from_date, to_date):
    """
    Fetch contacts (leads) from Gallabox for a given channel and date range.
    Returns list of contact records.
    """
    url = f"{GALLABOX_BASE_URL}/contacts"
    headers = {
        "apiKey":    GALLABOX_API_KEY,
        "apiSecret": GALLABOX_API_SECRET,
        "accountId": GALLABOX_ACCOUNT_ID,
        "Content-Type": "application/json",
    }
    params = {
        "channelId": channel_id,
        "fromDate":  from_date,
        "toDate":    to_date,
        "limit":     1000,
        "page":      1,
    }
    all_contacts = []
    while True:
        try:
            r = requests.get(url, headers=headers, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  ⚠ Gallabox API error [{channel_id}]: {e}")
            break

        # Handle both list and paginated response
        if isinstance(data, list):
            all_contacts.extend(data)
            break
        elif isinstance(data, dict):
            contacts = (
                data.get("contacts") or
                data.get("data") or
                data.get("results") or
                []
            )
            all_contacts.extend(contacts)
            total = data.get("total") or data.get("totalCount") or 0
            if len(all_contacts) >= total or not contacts:
                break
            params["page"] += 1
        else:
            break

    return all_contacts

def count_leads(channel_id, from_date, to_date):
    contacts = fetch_contacts(channel_id, from_date, to_date)
    return len(contacts)

# ─────────────────────────────────────────────
#  MTD STORE MANAGEMENT
# ─────────────────────────────────────────────
def get_mtd_counts(store, today, month_start):
    """
    MTD = stored cumulative up to yesterday + today's fresh count.
    Recalculates fully from API for accuracy.
    """
    mtd = {}
    for name, cid in CHANNELS.items():
        count = count_leads(cid, month_start, today)
        mtd[name] = count
        print(f"  MTD [{name}]: {count}")
    return mtd

def get_today_counts(today):
    counts = {}
    for name, cid in CHANNELS.items():
        count = count_leads(cid, today, today)
        counts[name] = count
        print(f"  Today [{name}]: {count}")
    return counts

# ─────────────────────────────────────────────
#  SLACK MESSAGE BUILDER
# ─────────────────────────────────────────────
def build_leads_message(today_counts, mtd_counts, today, month_start):
    now_str     = dubai_now().strftime("%d %b %Y | %I:%M %p")
    month       = month_label()

    # Totals
    today_total = sum(today_counts.values())
    mtd_total   = sum(mtd_counts.values())

    # Today rows
    today_rows = ""
    for name in CHANNELS:
        c = today_counts.get(name, 0)
        bar = "🟢" if c > 0 else "⚪"
        today_rows += f"{bar} *{name}:* {c} lead{'s' if c != 1 else ''}\n"

    # MTD rows
    mtd_rows = ""
    for name in CHANNELS:
        c = mtd_counts.get(name, 0)
        mtd_rows += f"• *{name}:* {c}\n"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📊 MKV DAILY LEADS REPORT — {today}"}
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Generated: {now_str}  |  via GitHub Actions"}]
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*📥 TODAY'S NEW LEADS — {today}*\n\n{today_rows.strip()}\n\n*Total Today: {today_total} lead{'s' if today_total != 1 else ''}*"
            }
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*📈 MTD LEADS — {month}*\n_(From {month_start} to {today})_\n\n{mtd_rows.strip()}\n\n*MTD Total: {mtd_total} lead{'s' if mtd_total != 1 else ''}*"
            }
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "MKV Car Rental — Gallabox Lead Tracker"}]
        },
    ]

    return {
        "text": f"📊 MKV Leads Report — Today: {today_total} | MTD: {mtd_total}",
        "blocks": blocks
    }

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    now         = dubai_now()
    today       = today_str()
    month_start = month_start_str()

    print("=" * 56)
    print(f"  MKV GALLABOX SNAPSHOT ({today})")
    print(f"  Sent at: {now.strftime('%d %b %Y | %I:%M %p')} Dubai Time")
    print("=" * 56)

    store = load_store()

    print("  Fetching today's leads...")
    today_counts = get_today_counts(today)

    print("  Fetching MTD leads...")
    mtd_counts = get_mtd_counts(store, today, month_start)

    print("  Sending to Slack...")
    msg = build_leads_message(today_counts, mtd_counts, today, month_start)
    ok  = post_slack(WEBHOOK_LEADS, msg)
    print(f"  Slack {'OK' if ok else 'FAILED'} -> #mkv-daily-lead-report")

    # Save MTD to store
    store[f"mtd_{today}"]   = mtd_counts
    store[f"today_{today}"] = today_counts
    save_store(store)

    print("=" * 56)
    print("  Done")
    print("=" * 56)

if __name__ == "__main__":
    main()
