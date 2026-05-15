import requests
import json
import os
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ══════════════════════════════════════════════════════════════════════════════
# TEST MODE
#   True  → posts to #mkvtest (C0AVCCCG0S0), does NOT write mtd_store.json
#   False → live mode: posts to #mkv-daily-lead-report, saves MTD store
# ══════════════════════════════════════════════════════════════════════════════
TEST_MODE = True

# ══════════════════════════════════════════════════════════════════════════════
# SLACK CHANNEL IDs
# ══════════════════════════════════════════════════════════════════════════════
CHANNEL_LEAD_REPORT = "C0ABN1ZKSGN"   # #mkv-daily-lead-report   (live)
CHANNEL_TEST        = "C0B0TGBDCDU"   # #mkv-test-automation     (test)

# Active channel — auto-selected by TEST_MODE
ACTIVE_CHANNEL = CHANNEL_TEST if TEST_MODE else CHANNEL_LEAD_REPORT

# ══════════════════════════════════════════════════════════════════════════════
# CREDENTIALS — read from GitHub Secrets (env vars) with hardcoded fallbacks
#               for local development only
# ══════════════════════════════════════════════════════════════════════════════
ACCOUNT_ID  = os.environ.get("GALLABOX_ACCOUNT_ID", "66e3f05033e71154d5fdd76c")
API_KEY     = os.environ.get("GALLABOX_API_KEY",    "69e7694e2da59f609317986b")
API_SECRET  = os.environ.get("GALLABOX_API_SECRET", "984394d316324482a8615eba6742b3ab")

# Webhooks — TEST_MODE routes to #mkvtest webhook, live routes to lead report
WEBHOOK_LEAD_REPORT  = os.environ.get("WEBHOOK_LEADS",       "https://hooks.slack.com/services/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")
WEBHOOK_CUST_SERVICE = os.environ.get("WEBHOOK_CUST_SERVICE", "https://hooks.slack.com/services/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")
WEBHOOK_TEST         = os.environ.get("WEBHOOK_TEST",         "https://hooks.slack.com/services/T0ABTFCEZSL/B0B0TGBDCDU/REPLACE_WITH_TEST_AUTOMATION_WEBHOOK")  # #mkv-test-automation

# Active webhook — auto-selected by TEST_MODE
ACTIVE_WEBHOOK = WEBHOOK_TEST if TEST_MODE else WEBHOOK_LEAD_REPORT

# MTD store: GitHub Actions workspace (repo root) or local fallback
MTD_STORE = os.environ.get("MTD_STORE_PATH", "mtd_store.json")

# ══════════════════════════════════════════════════════════════════════════════
# GALLABOX CONFIG
# ══════════════════════════════════════════════════════════════════════════════
GALLABOX_HEADERS = {"apiKey": API_KEY, "apiSecret": API_SECRET, "Content-Type": "application/json"}
BASE_URL = "https://server.gallabox.com/devapi/accounts/" + ACCOUNT_ID
GALLABOX_CHANNELS = [
    {"name": "MKV Luxury Main",       "id": "675a90ddda3020e52915beff"},
    {"name": "MKV Luxury Car Rental", "id": "66e930025e9ef7252ccc8a25"},
    {"name": "Rent to Own",           "id": "699d8cca452cc56936e21e45"},
]

# ══════════════════════════════════════════════════════════════════════════════
# DATE / TIME SETUP
# ══════════════════════════════════════════════════════════════════════════════
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

# ══════════════════════════════════════════════════════════════════════════════
# MTD STORE
# ══════════════════════════════════════════════════════════════════════════════
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

# ══════════════════════════════════════════════════════════════════════════════
# GALLABOX API
# ══════════════════════════════════════════════════════════════════════════════
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

# ══════════════════════════════════════════════════════════════════════════════
# DATA PROCESSING
# ══════════════════════════════════════════════════════════════════════════════
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

# ══════════════════════════════════════════════════════════════════════════════
# SLACK — message builder (unchanged) + sender (TEST_MODE aware)
# ══════════════════════════════════════════════════════════════════════════════
def send_to_slack(message, webhooks):
    channel_name = "#mkv-test-automation (C0B0TGBDCDU)" if TEST_MODE else "#mkv-daily-lead-report"
    for wh in webhooks:
        try:
            r = requests.post(wh, json={"text": message}, timeout=10)
            if r.status_code == 200: print("  Slack OK → " + channel_name)
            else: print("  Slack error: " + str(r.status_code))
        except Exception as e:
            print("  Slack failed: " + str(e))

def build_msg_gallabox(g):
    fa = g["ftd_agents"]; fs = g["ftd_sources"]; fg = g["ftd_stages"]
    ma = g["mtd_agents"]; ms = g["mtd_sources"]; mg = g["mtd_stages"]

    all_agents = sorted(set(list(fa.keys()) + list(ma.keys())))
    ag  = "{:<20} {:>6} {:>6} {:>7} {:>7}\n".format("Agent","Y.Rec","Y.Trg","M.Rec","M.Trg")
    ag += "-"*50 + "\n"
    for name in all_agents:
        f = fa.get(name, {"recd":0,"trig":0}); m = ma.get(name, {"recd":0,"trig":0})
        ag += "{:<20} {:>6} {:>6} {:>7} {:>7}\n".format(name[:20], f["recd"], f["trig"], m["recd"], m["trig"])
    ag += "-"*50 + "\n"
    ag += "{:<20} {:>6} {:>6} {:>7} {:>7}".format(
        "TOTAL",
        sum(v["recd"] for v in fa.values()), sum(v["trig"] for v in fa.values()),
        sum(v["recd"] for v in ma.values()), sum(v["trig"] for v in ma.values()))

    SOURCE_ORDER = ["Google Ads","Facebook / Instagram","Instagram DMs","OneClickDrive","Website"]
    all_srcs = SOURCE_ORDER + [s for s in sorted(set(list(fs.keys())+list(ms.keys()))) if s not in SOURCE_ORDER]
    src  = "{:<24} {:>5}  {:>7}\n".format("Source","FTD","MTD") + "-"*38 + "\n"
    for s in all_srcs:
        if fs.get(s,0) > 0 or ms.get(s,0) > 0:
            src += "{:<24} {:>5}  {:>7}\n".format(s[:24], fs.get(s,0), ms.get(s,0))
    src += "-"*38 + "\n"
    src += "{:<24} {:>5}  {:>7}".format("TOTAL", sum(fs.values()), sum(ms.values()))

    STAGE_ORDER = ["Lead created","Qualified lead","Converted lead","Unknown"]
    all_stgs = STAGE_ORDER + [s for s in sorted(set(list(fg.keys())+list(mg.keys()))) if s not in STAGE_ORDER]
    stg  = "{:<24} {:>5}  {:>7}\n".format("Stage","FTD","MTD") + "-"*38 + "\n"
    for s in all_stgs:
        if fg.get(s,0) > 0 or mg.get(s,0) > 0:
            stg += "{:<24} {:>5}  {:>7}\n".format(s[:24], fg.get(s,0), mg.get(s,0))
    stg += "-"*38 + "\n"
    stg += "{:<24} {:>5}  {:>7}".format("TOTAL", sum(fg.values()), sum(mg.values()))

    return (
        ":bar_chart: *MKV LUXURY — LEADS & AGENTS REPORT*\n"
        + ":calendar: " + report_dt + "\n"
        + "_FTD = Yesterday (" + yesterday_str + ") | MTD = Month cumulative_\n\n"
        + "*:dart: AGENT PERFORMANCE*\n" + "```" + ag + "```\n"
        + "*:globe_with_meridians: LEAD SOURCE*\n" + "```" + src + "```\n"
        + "*:chart_with_upwards_trend: LEAD STAGE*\n" + "```" + stg + "```"
    )

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    print("="*56)
    print("  MKV LUXURY - DAILY LEADS REPORT")
    print("  " + report_dt)
    print("  Reporting for: " + yesterday_str)
    print("  TEST_MODE : " + str(TEST_MODE))
    print("  Channel   : " + ("#mkv-test-automation (C0B0TGBDCDU)" if TEST_MODE else "#mkv-daily-lead-report"))
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
    send_to_slack(msg, [ACTIVE_WEBHOOK])

    print("="*56)
    print("  End of dashboard")
    print("="*56)

if __name__ == "__main__":
    main()
