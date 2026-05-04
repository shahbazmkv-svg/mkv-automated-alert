import requests
import json
import os
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

ACCOUNT_ID           = "66e3f05033e71154d5fdd76c"
API_KEY              = "69e7694e2da59f609317986b"
API_SECRET           = "984394d316324482a8615eba6742b3ab"
WEBHOOK_LEAD_REPORT  = "https://hooks.slack.com/services/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"
WEBHOOK_CUST_SERVICE = "https://hooks.slack.com/services/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"

# ── FIX: Use GitHub-compatible path for MTD store ──
MTD_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mtd_store.json")

GALLABOX_HEADERS = {"apiKey": API_KEY, "apiSecret": API_SECRET, "Content-Type": "application/json"}
BASE_URL = "https://server.gallabox.com/devapi/accounts/" + ACCOUNT_ID

GALLABOX_CHANNELS = [
    {"name": "MKV Luxury Main",       "id": "675a90ddda3020e52915beff"},
    {"name": "MKV Luxury Car Rental", "id": "66e930025e9ef7252ccc8a25"},
    {"name": "Rent to Own",           "id": "699d8cca452cc56936e21e45"},
]

dubai_tz        = timezone(timedelta(hours=4))
utc_tz          = timezone.utc
now_dubai       = datetime.now(dubai_tz)
yesterday_dubai = now_dubai - timedelta(days=1)
report_dt       = now_dubai.strftime("%d %b %Y | %I:%M %p Dubai Time")
yesterday_str   = yesterday_dubai.strftime("%d %b %Y")
yesterday_key   = yesterday_dubai.strftime("%Y-%m-%d")
cur_month       = now_dubai.strftime("%Y-%m")
days_in_mtd     = yesterday_dubai.day  # number of days from 1st to yesterday

# Yesterday window in UTC (for API filtering)
yday_utc_start  = yesterday_dubai.replace(hour=0,  minute=1,  second=0, microsecond=0).astimezone(utc_tz)
yday_utc_end    = yesterday_dubai.replace(hour=23, minute=59, second=59, microsecond=0).astimezone(utc_tz)

# Month start in UTC
month_start_dubai = yesterday_dubai.replace(day=1, hour=0, minute=1, second=0, microsecond=0)
month_utc_start   = month_start_dubai.astimezone(utc_tz)

# ══════════════════════════════════════════════════════════════════════════════
# MTD STORE
# ══════════════════════════════════════════════════════════════════════════════

def load_mtd_store():
    try:
        if os.path.exists(MTD_STORE):
            with open(MTD_STORE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("month") != cur_month:
                print("  New month detected — resetting MTD store")
                return {"month": cur_month, "days": {}}
            return data
    except:
        pass
    return {"month": cur_month, "days": {}}

def save_mtd_store(store):
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
# GALLABOX API — CONTACTS (FIX: use /contacts endpoint filtered by createdAt)
# This matches exactly what the Gallabox dashboard counts as "FTD"
# ══════════════════════════════════════════════════════════════════════════════

def parse_utc(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except:
        return None

def fetch_contacts_in_window(utc_start, utc_end):
    """
    Fetch all contacts created within a UTC time window.
    Uses pagination to get ALL pages (fixes the 2-page limit bug).
    """
    contacts = []
    seen_ids = set()
    page = 1
    while True:
        params = {
            "limit": 100,
            "page": page,
            "createdAtFrom": utc_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "createdAtTo":   utc_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        try:
            r = requests.get(BASE_URL + "/contacts", headers=GALLABOX_HEADERS, params=params, timeout=15)
            r.raise_for_status()
            data  = r.json()
            items = data if isinstance(data, list) else data.get("data", data.get("contacts", []))
            if not items:
                break
            new_found = False
            for c in items:
                cid = c.get("_id") or c.get("id", "")
                if cid in seen_ids:
                    continue
                seen_ids.add(cid)
                new_found = True
                contacts.append(c)
            if not new_found:
                break
            # If fewer than limit returned, we're on the last page
            if len(items) < 100:
                break
            page += 1
        except Exception as e:
            print("  [ERROR] Contacts API page " + str(page) + ": " + str(e))
            break
    return contacts

def extract_contact_fields(contact):
    """Extract agent, lead source, lead stage, triggered from a contact record."""
    # Agent / contact owner
    owner = contact.get("contactOwner") or contact.get("user") or {}
    if isinstance(owner, dict):
        agent = (owner.get("name") or "").strip() or "Unassigned"
    else:
        agent = str(owner).strip() or "Unassigned"

    # Lead source & stage from fieldValuesKV or direct fields
    kv     = contact.get("fieldValuesKV") or {}
    source = (kv.get("lead_source") or contact.get("leadSource") or "").strip()
    stage  = (kv.get("lead_stage")  or contact.get("leadStatus") or "").strip()

    # Normalize source names to match Gallabox dashboard labels
    SOURCE_MAP = {
        "google ads":          "Google Ads",
        "googleads":           "Google Ads",
        "facebook":            "Facebook / Instagram",
        "instagram":           "Facebook / Instagram",
        "facebook / instagram":"Facebook / Instagram",
        "instagram dms":       "Instagram DMs",
        "instagramdms":        "Instagram DMs",
        "oneclickdrive":       "OneClickDrive",
        "one click drive":     "OneClickDrive",
        "website":             "Website",
    }
    STAGE_MAP = {
        "lead created":    "Lead created",
        "qualified lead":  "Qualified lead",
        "converted lead":  "Converted lead",
        "unknown":         "Unknown",
    }
    source = SOURCE_MAP.get(source.lower(), source) if source else "Instagram DMs"
    stage  = STAGE_MAP.get(stage.lower(),  stage)  if stage  else "Unknown"

    # Triggered tag
    tags      = [t.get("name", "").lower() for t in (contact.get("tags") or [])]
    triggered = "triggered" in tags

    return agent, source, stage, triggered

# ══════════════════════════════════════════════════════════════════════════════
# PROCESS — FTD (yesterday) + MTD (month-to-date via store)
# ══════════════════════════════════════════════════════════════════════════════

def process_gallabox(store):
    # ── Step 1: Fetch yesterday's contacts (FTD) ──
    print("  Fetching FTD contacts (" + yesterday_key + ")...")
    ftd_contacts = fetch_contacts_in_window(yday_utc_start, yday_utc_end)
    print("  FTD contacts found: " + str(len(ftd_contacts)))

    # ── Step 2: If no FTD from API, fall back to stored data ──
    existing_ytd      = store.get("days", {}).get(yesterday_key, {})
    use_store_for_ftd = bool(existing_ytd) and len(ftd_contacts) == 0
    if use_store_for_ftd:
        print("  No API results — using stored FTD data")

    ftd_agents = {}; ftd_sources = {}; ftd_stages = {}
    ytd_snap   = {}

    if use_store_for_ftd:
        # Rebuild FTD dicts from store
        for k, v in existing_ytd.items():
            if   k.startswith("a_r_"): name = k[4:]; ftd_agents.setdefault(name, {"recd":0,"trig":0}); ftd_agents[name]["recd"] += v
            elif k.startswith("a_t_"): name = k[4:]; ftd_agents.setdefault(name, {"recd":0,"trig":0}); ftd_agents[name]["trig"] += v
            elif k.startswith("s_"):   ftd_sources[k[2:]] = ftd_sources.get(k[2:], 0) + v
            elif k.startswith("g_"):   ftd_stages[k[2:]]  = ftd_stages.get(k[2:],  0) + v
        ytd_snap = dict(existing_ytd)
    else:
        # Process live FTD contacts
        for contact in ftd_contacts:
            agent, src, stg, trig = extract_contact_fields(contact)
            trig_int = 1 if trig else 0
            ftd_agents.setdefault(agent, {"recd":0,"trig":0})
            ftd_agents[agent]["recd"] += 1
            ftd_agents[agent]["trig"] += trig_int
            ftd_sources[src] = ftd_sources.get(src, 0) + 1
            ftd_stages[stg]  = ftd_stages.get(stg,  0) + 1
            ytd_snap["a_r_" + agent] = ytd_snap.get("a_r_" + agent, 0) + 1
            ytd_snap["a_t_" + agent] = ytd_snap.get("a_t_" + agent, 0) + trig_int
            ytd_snap["s_"   + src]   = ytd_snap.get("s_"   + src,   0) + 1
            ytd_snap["g_"   + stg]   = ytd_snap.get("g_"   + stg,   0) + 1

    # ── Step 3: Update MTD store and build MTD dicts ──
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
# BUILD SLACK MESSAGE
# ══════════════════════════════════════════════════════════════════════════════

def build_msg_gallabox(g):
    fa = g["ftd_agents"]; fs = g["ftd_sources"]; fg = g["ftd_stages"]
    ma = g["mtd_agents"]; ms = g["mtd_sources"]; mg = g["mtd_stages"]

    # Agent table — FIX: headers now correctly labeled FTD / MTD
    all_agents = sorted(set(list(fa.keys()) + list(ma.keys())))
    ag  = "{:<20} {:>6} {:>6} {:>7} {:>7}\n".format("Agent","F.Rec","F.Trg","M.Rec","M.Trg")
    ag += "-"*50 + "\n"
    for name in all_agents:
        f = fa.get(name, {"recd":0,"trig":0}); m = ma.get(name, {"recd":0,"trig":0})
        ag += "{:<20} {:>6} {:>6} {:>7} {:>7}\n".format(name[:20], f["recd"], f["trig"], m["recd"], m["trig"])
    ag += "-"*50 + "\n"
    ag += "{:<20} {:>6} {:>6} {:>7} {:>7}".format(
        "TOTAL",
        sum(v["recd"] for v in fa.values()), sum(v["trig"] for v in fa.values()),
        sum(v["recd"] for v in ma.values()), sum(v["trig"] for v in ma.values()))

    # Source table — FIX: header label corrected to FTD
    SOURCE_ORDER = ["Google Ads","Facebook / Instagram","Instagram DMs","OneClickDrive","Website"]
    all_srcs = SOURCE_ORDER + [s for s in sorted(set(list(fs.keys())+list(ms.keys()))) if s not in SOURCE_ORDER]
    src  = "{:<24} {:>5}  {:>7}\n".format("Source","FTD","MTD") + "-"*38 + "\n"
    for s in all_srcs:
        if fs.get(s,0) > 0 or ms.get(s,0) > 0:
            src += "{:<24} {:>5}  {:>7}\n".format(s[:24], fs.get(s,0), ms.get(s,0))
    src += "-"*38 + "\n"
    src += "{:<24} {:>5}  {:>7}".format("TOTAL", sum(fs.values()), sum(ms.values()))

    # Stage table — FIX: header label corrected to FTD
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
        + "_FTD = " + yesterday_str + " (00:01–23:59) | MTD = 1st–" + yesterday_str + " (" + str(days_in_mtd) + " days)_\n\n"
        + "*:dart: AGENT PERFORMANCE*\n" + "```" + ag + "```\n"
        + "*:globe_with_meridians: LEAD SOURCE*\n" + "```" + src + "```\n"
        + "*:chart_with_upwards_trend: LEAD STAGE*\n" + "```" + stg + "```"
    )

def send_to_slack(message, webhooks):
    for wh in webhooks:
        try:
            r = requests.post(wh, json={"text": message}, timeout=10)
            if r.status_code == 200: print("  Slack OK")
            else: print("  Slack error: " + str(r.status_code))
        except Exception as e:
            print("  Slack failed: " + str(e))

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("="*56)
    print("  MKV LUXURY - DAILY LEADS REPORT")
    print("  " + report_dt)
    print("  Reporting for: " + yesterday_str)
    print("="*56)

    print("\n  Loading MTD store...")
    store = load_mtd_store()
    print("  Month: " + store["month"] + " | Days recorded: " + str(len(store.get("days",{}))))

    print("\n  Processing Gallabox data (yesterday)...")
    gallabox  = process_gallabox(store)
    ytd_total = sum(v["recd"] for v in gallabox["ftd_agents"].values())
    mtd_total = sum(v["recd"] for v in gallabox["mtd_agents"].values())
    print("  FTD leads: " + str(ytd_total) + " | MTD leads: " + str(mtd_total))

    print("\n  Sending to Slack...")
    msg = build_msg_gallabox(gallabox)
    send_to_slack(msg, [WEBHOOK_LEAD_REPORT])
    print("  Done — sent to #mkv-daily-lead-report")

    print("="*56)
    print("  End of report")
    print("="*56)

if __name__ == "__main__":
    main()
