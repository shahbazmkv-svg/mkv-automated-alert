import requests
import json
import os
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==============================================================================
# TEST MODE
#   True  -> bot token + chat.postMessage -> #mkv-test-automation (C0B0TGBDCDU)
#            MTD store NOT written to disk
#   False -> webhook -> #mkv-daily-lead-report (C0ABN1ZKSGN)
#            MTD store saved normally
# ==============================================================================
TEST_MODE = False

# ==============================================================================
# SLACK CHANNEL IDs
# ==============================================================================
CHANNEL_LEAD_REPORT = "C0ABN1ZKSGN"   # #mkv-daily-lead-report  (live)
CHANNEL_TEST        = "C0B0TGBDCDU"   # #mkv-test-automation    (test)

ACTIVE_CHANNEL = CHANNEL_TEST if TEST_MODE else CHANNEL_LEAD_REPORT

# ==============================================================================
# CREDENTIALS - env vars (GitHub Secrets) with local fallbacks
# ==============================================================================
ACCOUNT_ID  = os.environ.get("GALLABOX_ACCOUNT_ID", "66e3f05033e71154d5fdd76c")
API_KEY     = os.environ.get("GALLABOX_API_KEY",    "69e7694e2da59f609317986b")
API_SECRET  = os.environ.get("GALLABOX_API_SECRET", "984394d316324482a8615eba6742b3ab")

# Bot token: used in TEST_MODE via chat.postMessage to exact channel ID
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")

# Webhooks: live mode only
WEBHOOK_LEAD_REPORT  = os.environ.get("WEBHOOK_LEADS",        "https://hooks.slack.com/services/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")
WEBHOOK_CUST_SERVICE = os.environ.get("WEBHOOK_CUST_SERVICE", "https://hooks.slack.com/services/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")

# MTD store path
MTD_STORE = os.environ.get("MTD_STORE_PATH", "mtd_store.json")

# ==============================================================================
# GALLABOX CONFIG
# ==============================================================================
GALLABOX_HEADERS = {"apiKey": API_KEY, "apiSecret": API_SECRET, "Content-Type": "application/json"}
BASE_URL = "https://server.gallabox.com/devapi/accounts/" + ACCOUNT_ID
GALLABOX_CHANNELS = [
    {"name": "MKV Luxury Main",       "id": "675a90ddda3020e52915beff"},
    {"name": "MKV Luxury Car Rental", "id": "66e930025e9ef7252ccc8a25"},
    {"name": "Rent to Own",           "id": "699d8cca452cc56936e21e45"},
]

# ==============================================================================
# DATE / TIME SETUP
# ==============================================================================
dubai_tz            = timezone(timedelta(hours=4))
utc_tz              = timezone.utc
now_dubai           = datetime.now(dubai_tz)
yesterday_dubai     = now_dubai - timedelta(days=1)
report_dt           = now_dubai.strftime("%d %b %Y | %I:%M %p Dubai Time")
yesterday_str       = yesterday_dubai.strftime("%d %b %Y")
yesterday_key       = yesterday_dubai.strftime("%Y-%m-%d")
cur_month           = now_dubai.strftime("%Y-%m")
yesterday_utc_start = yesterday_dubai.replace(hour=0,  minute=0,  second=0,  microsecond=0).astimezone(utc_tz)
yesterday_utc_end   = yesterday_dubai.replace(hour=23, minute=59, second=59, microsecond=0).astimezone(utc_tz)

# ==============================================================================
# MTD STORE
# ==============================================================================
def load_mtd_store():
    try:
        if os.path.exists(MTD_STORE):
            with open(MTD_STORE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("month") != cur_month:
                print("  New month detected - resetting MTD store")
                return {"month": cur_month, "days": {}}
            return data
    except:
        pass
    return {"month": cur_month, "days": {}}

def save_mtd_store(store):
    if TEST_MODE:
        print("  [TEST MODE] MTD store NOT written to disk")
        return
    with open(MTD_STORE, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, ensure_ascii=False)

def update_and_get_mtd(store, yesterday_snapshot):
    store["days"][yesterday_key] = yesterday_snapshot
    save_mtd_store(store)
    mtd = {}
    for day_data in store["days"].values():
        for k, v in day_data.items():
            mtd[k] = mtd.get(k, 0) + v
    return mtd

# ==============================================================================
# GALLABOX API
# ==============================================================================
def parse_utc(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except:
        return None

def fetch_contact_details(contact_id):
    try:
        r = requests.get(BASE_URL + "/contacts/" + contact_id, headers=GALLABOX_HEADERS, timeout=10)
        r.raise_for_status()
        d      = r.json()
        tags   = [t.get("name", "").lower() for t in (d.get("tags") or [])]
        kv     = d.get("fieldValuesKV", {}) or {}
        source = (kv.get("lead_source", "") or "").strip()
        stage  = (kv.get("lead_stage",  "") or "").strip()
        if not source: source = "Instagram DMs"
        if not stage:  stage  = "Unknown"
        return {"triggered": "triggered" in tags, "source": source, "stage": stage}
    except:
        return {"triggered": False, "source": "Instagram DMs", "stage": "Unknown"}

def fetch_all_conversations():
    all_convs = []; seen_ids = set()
    for ch in GALLABOX_CHANNELS:
        for page in range(1, 3):
            params = {"limit": 100, "page": page, "channelId": ch["id"], "channelType": "whatsapp"}
            try:
                r     = requests.get(BASE_URL + "/conversations", headers=GALLABOX_HEADERS, params=params, timeout=15)
                r.raise_for_status()
                data  = r.json()
                convs = data if isinstance(data, list) else data.get("data", [])
                if not convs: break
                new_found = False
                for conv in convs:
                    conv_id = conv.get("_id") or conv.get("id", "")
                    if conv_id in seen_ids: continue
                    seen_ids.add(conv_id); new_found = True
                    ts = parse_utc(conv.get("createdAt", ""))
                    if ts: all_convs.append({"conv": conv, "ts": ts})
                if not new_found: break
            except Exception as e:
                print("  [ERROR] Gallabox: " + str(e)); break
    return all_convs

# ==============================================================================
# DATA PROCESSING
# ==============================================================================
def process_gallabox(store):
    print("  Fetching conversations (for yesterday: " + yesterday_key + ")...")
    all_convs = fetch_all_conversations()
    ytd_convs = [c for c in all_convs if yesterday_utc_start <= c["ts"] <= yesterday_utc_end]
    print("  Yesterday conversations found via API: " + str(len(ytd_convs)))

    existing_ytd      = store.get("days", {}).get(yesterday_key, {})
    use_store_for_ftd = bool(existing_ytd) and len(ytd_convs) == 0
    if use_store_for_ftd:
        print("  Using stored data for yesterday FTD")

    contact_ids = list(set(
        c["conv"].get("contactId") or c["conv"].get("contact", {}).get("id", "")
        for c in ytd_convs
        if c["conv"].get("contactId") or c["conv"].get("contact", {}).get("id", "")
    ))
    contact_cache = {}
    if contact_ids:
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_id = {executor.submit(fetch_contact_details, cid): cid for cid in contact_ids}
            for future in as_completed(future_to_id):
                cid = future_to_id[future]
                try:    contact_cache[cid] = future.result()
                except: contact_cache[cid] = {"triggered": False, "source": "Instagram DMs", "stage": "Unknown"}

    def get_agent(conv):
        name = (conv.get("user") or {}).get("name") or ""
        return name.strip() or "Unassigned"
    def get_cid(conv):
        return conv.get("contactId") or conv.get("contact", {}).get("id", "")

    ftd_agents = {}; ftd_sources = {}; ftd_stages = {}
    ytd_snap   = {}

    if use_store_for_ftd:
        for k, v in existing_ytd.items():
            if   k.startswith("a_r_"): name = k[4:]; ftd_agents.setdefault(name, {"recd":0,"trig":0}); ftd_agents[name]["recd"] += v
            elif k.startswith("a_t_"): name = k[4:]; ftd_agents.setdefault(name, {"recd":0,"trig":0}); ftd_agents[name]["trig"] += v
            elif k.startswith("s_"):   ftd_sources[k[2:]] = ftd_sources.get(k[2:], 0) + v
            elif k.startswith("g_"):   ftd_stages[k[2:]]  = ftd_stages.get(k[2:],  0) + v
        ytd_snap = existing_ytd
    else:
        for item in ytd_convs:
            conv   = item["conv"]
            agent  = get_agent(conv)
            detail = contact_cache.get(get_cid(conv), {"triggered": False, "source": "Instagram DMs", "stage": "Unknown"})
            src    = detail["source"]; stg = detail["stage"]; trig = 1 if detail["triggered"] else 0
            ftd_agents.setdefault(agent, {"recd":0,"trig":0})
            ftd_agents[agent]["recd"] += 1
            ftd_agents[agent]["trig"] += trig
            ftd_sources[src] = ftd_sources.get(src, 0) + 1
            ftd_stages[stg]  = ftd_stages.get(stg,  0) + 1
            ytd_snap["a_r_" + agent] = ytd_snap.get("a_r_" + agent, 0) + 1
            ytd_snap["a_t_" + agent] = ytd_snap.get("a_t_" + agent, 0) + trig
            ytd_snap["s_"   + src]   = ytd_snap.get("s_"   + src,   0) + 1
            ytd_snap["g_"   + stg]   = ytd_snap.get("g_"   + stg,   0) + 1

    mtd_flat   = update_and_get_mtd(store, ytd_snap)
    mtd_agents = {}; mtd_sources = {}; mtd_stages = {}
    for k, v in mtd_flat.items():
        if   k.startswith("a_r_"): name = k[4:]; mtd_agents.setdefault(name, {"recd":0,"trig":0}); mtd_agents[name]["recd"] += v
        elif k.startswith("a_t_"): name = k[4:]; mtd_agents.setdefault(name, {"recd":0,"trig":0}); mtd_agents[name]["trig"] += v
        elif k.startswith("s_"):   mtd_sources[k[2:]] = mtd_sources.get(k[2:], 0) + v
        elif k.startswith("g_"):   mtd_stages[k[2:]]  = mtd_stages.get(k[2:],  0) + v

    return {
        "ftd_agents": ftd_agents, "ftd_sources": ftd_sources, "ftd_stages": ftd_stages,
        "mtd_agents": mtd_agents, "mtd_sources": mtd_sources, "mtd_stages": mtd_stages,
    }

# ==============================================================================
# SLACK SENDER
#   TEST_MODE  -> bot token + chat.postMessage -> CHANNEL_TEST (C0B0TGBDCDU)
#   Live mode  -> webhook  -> #mkv-daily-lead-report (C0ABN1ZKSGN)
# ==============================================================================
# ==============================================================================
# SLACK SENDER
#   TEST_MODE  -> bot token + chat.postMessage -> #mkv-test-automation (C0B0TGBDCDU)
#   Live mode  -> webhook  -> #mkv-daily-lead-report (C0ABN1ZKSGN)
#   Accepts a payload dict with 'text' (fallback) and 'blocks'
# ==============================================================================
def send_to_slack(payload):
    if TEST_MODE:
        if not SLACK_BOT_TOKEN:
            print("  [ERROR] SLACK_BOT_TOKEN not set - cannot post in TEST_MODE")
            return
        payload["channel"] = ACTIVE_CHANNEL
        r = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": "Bearer " + SLACK_BOT_TOKEN, "Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
        result = r.json()
        if result.get("ok"):
            print("  Slack OK -> #mkv-test-automation (C0B0TGBDCDU)")
        else:
            print("  Slack error: " + str(result.get("error")))
    else:
        r = requests.post(WEBHOOK_LEAD_REPORT, json=payload, timeout=10)
        if r.status_code == 200:
            print("  Slack OK -> #mkv-daily-lead-report (C0ABN1ZKSGN)")
        else:
            print("  Slack error: " + str(r.status_code))


# ==============================================================================
# MESSAGE BUILDER - Block Kit (mobile optimised)
#
# Agent table  : header row + one section per agent (2 fields: FTD col | MTD col)
# Source table : header + one section per source   (2 fields: name+FTD | MTD)
# Stage table  : header + one section per stage    (2 fields: name+FTD | MTD)
#
# Slack renders section.fields as a 2-column grid on all screen sizes.
# No fixed-width ASCII, no horizontal scroll on mobile.
# ==============================================================================
def build_msg_gallabox(g):
    fa = g["ftd_agents"]; fs = g["ftd_sources"]; fg = g["ftd_stages"]
    ma = g["mtd_agents"]; ms = g["mtd_sources"]; mg = g["mtd_stages"]

    def sec(text):
        return {"type": "section", "text": {"type": "mrkdwn", "text": text}}

    def divider():
        return {"type": "divider"}

    def context(text):
        return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}

    # Agent row: name(16) | F.Rec(6) | F.Trg(6) | M.Rec(6) | M.Trg(6)  = 42 chars
    def agent_row(name, frec, ftrg, mrec, mtrg):
        return "{:<16} {:>5} {:>5} {:>5} {:>5}".format(str(name)[:16], frec, ftrg, mrec, mtrg)

    # Source/Stage row: name(20) | FTD(5) | MTD(6)  = 31 chars
    def src_row(name, ftd, mtd):
        return "{:<20} {:>4} {:>5}".format(str(name)[:20], ftd, mtd)

    blocks = []

    # HEADER
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": "MKV Luxury - Leads & Agents Report", "emoji": True}
    })
    blocks.append(context(
        ":calendar: " + report_dt + "   |   _FTD = " + yesterday_str + "  |  MTD = month cumulative_"
    ))

    # AGENT PERFORMANCE
    blocks.append(divider())
    blocks.append(sec(":dart: *AGENT PERFORMANCE*\n_F.Rec=FTD rec | F.Trg=FTD trg | M.Rec=MTD rec | M.Trg=MTD trg_"))

    all_agents = sorted(set(list(fa.keys()) + list(ma.keys())))
    ag_lines = [
        agent_row("Agent", "F.Rec", "F.Trg", "M.Rec", "M.Trg"),
        "-" * 42,
    ]
    for name in all_agents:
        f = fa.get(name, {"recd": 0, "trig": 0})
        m = ma.get(name, {"recd": 0, "trig": 0})
        ag_lines.append(agent_row(name, f["recd"], f["trig"], m["recd"], m["trig"]))
    ag_lines.append("-" * 42)
    ag_lines.append(agent_row(
        "TOTAL",
        sum(v["recd"] for v in fa.values()), sum(v["trig"] for v in fa.values()),
        sum(v["recd"] for v in ma.values()), sum(v["trig"] for v in ma.values()),
    ))
    blocks.append(sec("```" + "\n".join(ag_lines) + "```"))

    # LEAD SOURCE
    blocks.append(divider())
    blocks.append(sec(":globe_with_meridians: *LEAD SOURCE*"))

    SOURCE_ORDER = ["Google Ads", "Facebook / Instagram", "Instagram DMs", "OneClickDrive", "Website"]
    all_srcs = SOURCE_ORDER + [s for s in sorted(set(list(fs.keys()) + list(ms.keys()))) if s not in SOURCE_ORDER]
    src_lines = [src_row("Source", "FTD", "MTD"), "-" * 31]
    for s in all_srcs:
        fv = fs.get(s, 0); mv = ms.get(s, 0)
        if fv > 0 or mv > 0:
            src_lines.append(src_row(s, fv, mv))
    src_lines.append("-" * 31)
    src_lines.append(src_row("TOTAL", sum(fs.values()), sum(ms.values())))
    blocks.append(sec("```" + "\n".join(src_lines) + "```"))

    # LEAD STAGE
    blocks.append(divider())
    blocks.append(sec(":chart_with_upwards_trend: *LEAD STAGE*"))

    STAGE_ORDER = ["Lead created", "Qualified lead", "Converted lead", "Unknown"]
    all_stgs = STAGE_ORDER + [s for s in sorted(set(list(fg.keys()) + list(mg.keys()))) if s not in STAGE_ORDER]
    stg_lines = [src_row("Stage", "FTD", "MTD"), "-" * 31]
    for s in all_stgs:
        fv = fg.get(s, 0); mv = mg.get(s, 0)
        if fv > 0 or mv > 0:
            stg_lines.append(src_row(s, fv, mv))
    stg_lines.append("-" * 31)
    stg_lines.append(src_row("TOTAL", sum(fg.values()), sum(mg.values())))
    blocks.append(sec("```" + "\n".join(stg_lines) + "```"))

    return {
        "text": "MKV Luxury - Leads & Agents Report | " + report_dt,
        "blocks": blocks,
    }


# ==============================================================================
# MAIN
# ==============================================================================
def main():
    print("="*56)
    print("  MKV LUXURY - DAILY LEADS REPORT")
    print("  " + report_dt)
    print("  Reporting for : " + yesterday_str)
    print("  TEST_MODE     : " + str(TEST_MODE))
    print("  Channel       : " + ("#mkv-test-automation (C0B0TGBDCDU)" if TEST_MODE else "#mkv-daily-lead-report (C0ABN1ZKSGN)"))
    print("  Post method   : " + ("bot token / chat.postMessage" if TEST_MODE else "webhook"))
    print("="*56)

    print("\n  Loading MTD store...")
    store = load_mtd_store()
    print("  Month: " + store["month"] + " | Days recorded: " + str(len(store.get("days",{}))))

    print("\n  Processing Gallabox data (yesterday)...")
    gallabox  = process_gallabox(store)
    ytd_total = sum(v["recd"] for v in gallabox["ftd_agents"].values())
    mtd_total = sum(v["recd"] for v in gallabox["mtd_agents"].values())
    print("  Yesterday leads: " + str(ytd_total) + " | MTD leads: " + str(mtd_total))

    print("\n  Sending to Slack...")
    msg = build_msg_gallabox(gallabox)
    send_to_slack(msg)

    print("="*56)
    print("  End of dashboard")
    print("="*56)

if __name__ == "__main__":
    main()
