import requests
import json
import os
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Credentials ───────────────────────────────────────────────────────────────
ACCOUNT_ID           = "66e3f05033e71154d5fdd76c"
API_KEY              = "69e7694e2da59f609317986b"
API_SECRET           = "984394d316324482a8615eba6742b3ab"
WEBHOOK_LEAD_REPORT  = "https://hooks.slack.com/services/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"
WEBHOOK_CUST_SERVICE = "https://hooks.slack.com/services/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"
MTD_STORE            = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mtd_store.json")

GALLABOX_HEADERS = {"apiKey": API_KEY, "apiSecret": API_SECRET, "Content-Type": "application/json"}
BASE_URL         = "https://server.gallabox.com/devapi/accounts/" + ACCOUNT_ID

GALLABOX_CHANNELS = [
    {"name": "MKV Luxury Main",       "id": "675a90ddda3020e52915beff"},
    {"name": "MKV Luxury Car Rental", "id": "66e930025e9ef7252ccc8a25"},
    {"name": "Rent to Own",           "id": "699d8cca452cc56936e21e45"},
]

# ── Time setup ────────────────────────────────────────────────────────────────
dubai_tz        = timezone(timedelta(hours=4))
utc_tz          = timezone.utc
now_dubai       = datetime.now(dubai_tz)
yesterday_dubai = now_dubai - timedelta(days=1)
report_dt       = now_dubai.strftime("%d %b %Y | %I:%M %p Dubai Time")
yesterday_str   = yesterday_dubai.strftime("%d %b %Y")
yesterday_key   = yesterday_dubai.strftime("%Y-%m-%d")
cur_month       = now_dubai.strftime("%Y-%m")
days_in_mtd     = yesterday_dubai.day

# Yesterday full day window in UTC (Dubai is UTC+4)
yday_utc_start = yesterday_dubai.replace(hour=0,  minute=0,  second=0,  microsecond=0).astimezone(utc_tz)
yday_utc_end   = yesterday_dubai.replace(hour=23, minute=59, second=59, microsecond=0).astimezone(utc_tz)

# ══════════════════════════════════════════════════════════════════════════════
# MTD STORE
# ══════════════════════════════════════════════════════════════════════════════

def load_mtd_store():
    try:
        if os.path.exists(MTD_STORE):
            with open(MTD_STORE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("month") != cur_month:
                print("  New month — resetting MTD store")
                return {"month": cur_month, "days": {}}
            return data
    except:
        pass
    return {"month": cur_month, "days": {}}

def save_mtd_store(store):
    store["month"] = cur_month   # always stamp current month
    with open(MTD_STORE, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, ensure_ascii=False)

def update_and_get_mtd(store, snap):
    """
    Save yesterday snapshot (idempotent — won't overwrite if already saved).
    Return sum of all days in the store as flat MTD dict.
    """
    if yesterday_key not in store["days"]:
        store["days"][yesterday_key] = snap
        save_mtd_store(store)
        print("  MTD store: saved " + yesterday_key)
    else:
        print("  MTD store: " + yesterday_key + " already stored — not overwriting")
    mtd = {}
    for day_data in store["days"].values():
        for k, v in day_data.items():
            mtd[k] = mtd.get(k, 0) + v
    return mtd

# ══════════════════════════════════════════════════════════════════════════════
# GALLABOX API — CONVERSATIONS
#
# IMPORTANT: conversations are sorted by updatedAt (last message), NOT createdAt.
# So we CANNOT use early-stop. We must fetch ALL pages and filter by createdAt.
# ══════════════════════════════════════════════════════════════════════════════

def parse_utc(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except:
        return None

def fetch_all_conversations():
    """
    Fetch ALL conversations across all channels (all pages, no limit).
    Returns every conversation regardless of date — caller filters by createdAt.
    Conversations are sorted by updatedAt desc, so we cannot break early.
    """
    all_convs = []
    seen_ids  = set()

    for ch in GALLABOX_CHANNELS:
        page = 1
        while True:
            params = {
                "limit":       100,
                "page":        page,
                "channelId":   ch["id"],
                "channelType": "whatsapp",
            }
            try:
                r = requests.get(BASE_URL + "/conversations",
                                 headers=GALLABOX_HEADERS, params=params, timeout=15)
                r.raise_for_status()
                raw   = r.json()
                convs = raw if isinstance(raw, list) else raw.get("data", [])
            except Exception as e:
                print("  [ERROR] " + ch["name"] + " page " + str(page) + ": " + str(e))
                break

            if not convs:
                break

            new_found = False
            for conv in convs:
                cid = conv.get("_id") or conv.get("id", "")
                if cid in seen_ids:
                    continue
                seen_ids.add(cid)
                new_found = True
                ts = parse_utc(conv.get("createdAt", ""))
                if ts:
                    all_convs.append({"conv": conv, "ts": ts})

            print("  " + ch["name"] + " page " + str(page) +
                  ": " + str(len(convs)) + " convs | total so far: " + str(len(all_convs)))

            if not new_found or len(convs) < 100:
                break
            page += 1

    return all_convs

def fetch_contact_detail(contact_id):
    """Fetch individual contact for lead_source, lead_stage, tags, owner."""
    try:
        r = requests.get(BASE_URL + "/contacts/" + contact_id,
                         headers=GALLABOX_HEADERS, timeout=10)
        r.raise_for_status()
        d     = r.json()
        tags  = [t.get("name", "").lower() for t in (d.get("tags") or [])]
        kv    = d.get("fieldValuesKV") or {}
        src   = (kv.get("lead_source") or "").strip()
        stg   = (kv.get("lead_stage")  or "").strip()
        own   = d.get("contactOwner") or d.get("user") or {}
        agent = (own.get("name") if isinstance(own, dict) else str(own)).strip() or "Unassigned"
        if not src: src = "Instagram DMs"
        if not stg: stg = "Unknown"
        return {"agent": agent, "source": src, "stage": stg,
                "triggered": "triggered" in tags}
    except:
        return {"agent": "Unassigned", "source": "Instagram DMs",
                "stage": "Unknown", "triggered": False}

def normalize_source(s):
    MAP = {
        "google ads": "Google Ads", "googleads": "Google Ads",
        "facebook": "Facebook / Instagram", "instagram": "Facebook / Instagram",
        "facebook / instagram": "Facebook / Instagram",
        "instagram dms": "Instagram DMs", "instagramdms": "Instagram DMs",
        "oneclickdrive": "OneClickDrive", "one click drive": "OneClickDrive",
        "website": "Website",
    }
    return MAP.get(s.lower(), s) if s else "Instagram DMs"

def normalize_stage(s):
    MAP = {
        "lead created": "Lead created", "qualified lead": "Qualified lead",
        "converted lead": "Converted lead", "unknown": "Unknown",
    }
    return MAP.get(s.lower(), s) if s else "Unknown"

def get_agent_from_conv(conv):
    u = conv.get("user") or conv.get("assignedUser") or {}
    if isinstance(u, dict):
        return (u.get("name") or "").strip() or "Unassigned"
    return str(u).strip() or "Unassigned"

def get_contact_id(conv):
    return (conv.get("contactId") or
            (conv.get("contact") or {}).get("_id", "") or
            (conv.get("contact") or {}).get("id", ""))

# ══════════════════════════════════════════════════════════════════════════════
# PROCESS — FTD + MTD
# ══════════════════════════════════════════════════════════════════════════════

def process_gallabox(store):
    # Step 1 — fetch ALL conversations, filter yesterday by createdAt
    all_convs  = fetch_all_conversations()
    ytd_convs  = [c for c in all_convs
                  if yday_utc_start <= c["ts"] <= yday_utc_end]
    print("\n  Total convs fetched : " + str(len(all_convs)))
    print("  Yesterday convs     : " + str(len(ytd_convs)))

    ftd_agents = {}; ftd_sources = {}; ftd_stages = {}; ytd_snap = {}
    existing_ytd = store.get("days", {}).get(yesterday_key, {})

    if len(ytd_convs) == 0 and existing_ytd:
        print("  No API convs for yesterday — restoring FTD from store")
        for k, v in existing_ytd.items():
            if   k.startswith("a_r_"): n=k[4:]; ftd_agents.setdefault(n,{"recd":0,"trig":0}); ftd_agents[n]["recd"]+=v
            elif k.startswith("a_t_"): n=k[4:]; ftd_agents.setdefault(n,{"recd":0,"trig":0}); ftd_agents[n]["trig"]+=v
            elif k.startswith("s_"):   ftd_sources[k[2:]]=ftd_sources.get(k[2:],0)+v
            elif k.startswith("g_"):   ftd_stages[k[2:]] =ftd_stages.get(k[2:], 0)+v
        ytd_snap = dict(existing_ytd)
    else:
        # Step 2 — enrich contacts in parallel
        contact_ids = list({get_contact_id(c["conv"]) for c in ytd_convs} - {""})
        print("  Unique contacts to enrich: " + str(len(contact_ids)))

        contact_cache = {}
        with ThreadPoolExecutor(max_workers=10) as ex:
            futures = {ex.submit(fetch_contact_detail, cid): cid for cid in contact_ids}
            for fut in as_completed(futures):
                cid = futures[fut]
                try:    contact_cache[cid] = fut.result()
                except: contact_cache[cid] = {"agent":"Unassigned","source":"Instagram DMs","stage":"Unknown","triggered":False}

        # Step 3 — count one entry per unique contact (not per conversation)
        seen_contacts = set()
        for item in ytd_convs:
            conv = item["conv"]
            cid  = get_contact_id(conv)
            if cid in seen_contacts:
                continue
            seen_contacts.add(cid)

            detail = contact_cache.get(cid, {"agent":"Unassigned","source":"Instagram DMs","stage":"Unknown","triggered":False})
            agent  = get_agent_from_conv(conv)
            if agent == "Unassigned":
                agent = detail["agent"]
            src = normalize_source(detail["source"])
            stg = normalize_stage(detail["stage"])
            ti  = 1 if detail["triggered"] else 0

            ftd_agents.setdefault(agent, {"recd":0,"trig":0})
            ftd_agents[agent]["recd"] += 1
            ftd_agents[agent]["trig"] += ti
            ftd_sources[src] = ftd_sources.get(src, 0) + 1
            ftd_stages[stg]  = ftd_stages.get(stg,  0) + 1
            ytd_snap["a_r_"+agent] = ytd_snap.get("a_r_"+agent, 0) + 1
            ytd_snap["a_t_"+agent] = ytd_snap.get("a_t_"+agent, 0) + ti
            ytd_snap["s_"+src]     = ytd_snap.get("s_"+src,     0) + 1
            ytd_snap["g_"+stg]     = ytd_snap.get("g_"+stg,     0) + 1

    # Step 4 — MTD store
    mtd_flat = update_and_get_mtd(store, ytd_snap)
    mtd_agents={}; mtd_sources={}; mtd_stages={}
    for k, v in mtd_flat.items():
        if   k.startswith("a_r_"): n=k[4:]; mtd_agents.setdefault(n,{"recd":0,"trig":0}); mtd_agents[n]["recd"]+=v
        elif k.startswith("a_t_"): n=k[4:]; mtd_agents.setdefault(n,{"recd":0,"trig":0}); mtd_agents[n]["trig"]+=v
        elif k.startswith("s_"):   mtd_sources[k[2:]]=mtd_sources.get(k[2:],0)+v
        elif k.startswith("g_"):   mtd_stages[k[2:]] =mtd_stages.get(k[2:], 0)+v

    return {"ftd_agents":ftd_agents, "ftd_sources":ftd_sources, "ftd_stages":ftd_stages,
            "mtd_agents":mtd_agents, "mtd_sources":mtd_sources, "mtd_stages":mtd_stages}

# ══════════════════════════════════════════════════════════════════════════════
# SLACK MESSAGE
# ══════════════════════════════════════════════════════════════════════════════

def build_msg_gallabox(g):
    fa=g["ftd_agents"]; fs=g["ftd_sources"]; fg=g["ftd_stages"]
    ma=g["mtd_agents"]; ms=g["mtd_sources"]; mg=g["mtd_stages"]

    all_agents = sorted(set(list(fa.keys())+list(ma.keys())))
    ag  = "{:<20} {:>6} {:>6} {:>7} {:>7}\n".format("Agent","F.Rec","F.Trg","M.Rec","M.Trg")
    ag += "-"*50+"\n"
    for n in all_agents:
        f=fa.get(n,{"recd":0,"trig":0}); m=ma.get(n,{"recd":0,"trig":0})
        ag += "{:<20} {:>6} {:>6} {:>7} {:>7}\n".format(n[:20],f["recd"],f["trig"],m["recd"],m["trig"])
    ag += "-"*50+"\n"
    ag += "{:<20} {:>6} {:>6} {:>7} {:>7}".format("TOTAL",
        sum(v["recd"] for v in fa.values()), sum(v["trig"] for v in fa.values()),
        sum(v["recd"] for v in ma.values()), sum(v["trig"] for v in ma.values()))

    SOURCE_ORDER=["Google Ads","Facebook / Instagram","Instagram DMs","OneClickDrive","Website"]
    all_srcs=SOURCE_ORDER+[s for s in sorted(set(list(fs.keys())+list(ms.keys()))) if s not in SOURCE_ORDER]
    src = "{:<24} {:>5}  {:>7}\n".format("Source","FTD","MTD")+"-"*38+"\n"
    for s in all_srcs:
        if fs.get(s,0)>0 or ms.get(s,0)>0:
            src += "{:<24} {:>5}  {:>7}\n".format(s[:24],fs.get(s,0),ms.get(s,0))
    src += "-"*38+"\n"
    src += "{:<24} {:>5}  {:>7}".format("TOTAL",sum(fs.values()),sum(ms.values()))

    STAGE_ORDER=["Lead created","Qualified lead","Converted lead","Unknown"]
    all_stgs=STAGE_ORDER+[s for s in sorted(set(list(fg.keys())+list(mg.keys()))) if s not in STAGE_ORDER]
    stg = "{:<24} {:>5}  {:>7}\n".format("Stage","FTD","MTD")+"-"*38+"\n"
    for s in all_stgs:
        if fg.get(s,0)>0 or mg.get(s,0)>0:
            stg += "{:<24} {:>5}  {:>7}\n".format(s[:24],fg.get(s,0),mg.get(s,0))
    stg += "-"*38+"\n"
    stg += "{:<24} {:>5}  {:>7}".format("TOTAL",sum(fg.values()),sum(mg.values()))

    return (
        ":bar_chart: *MKV LUXURY — LEADS & AGENTS REPORT*\n"
        + ":calendar: " + report_dt + "\n"
        + "_FTD = " + yesterday_str + " (00:01–23:59) | MTD = 1st–" + yesterday_str
        + " (" + str(days_in_mtd) + " days)_\n\n"
        + "*:dart: AGENT PERFORMANCE*\n```" + ag + "```\n"
        + "*:globe_with_meridians: LEAD SOURCE*\n```" + src + "```\n"
        + "*:chart_with_upwards_trend: LEAD STAGE*\n```" + stg + "```"
    )

def send_to_slack(message, webhooks):
    for wh in webhooks:
        try:
            r = requests.post(wh, json={"text": message}, timeout=10)
            if r.status_code == 200: print("  Slack OK")
            else: print("  Slack error " + str(r.status_code) + ": " + r.text[:200])
        except Exception as e:
            print("  Slack failed: " + str(e))

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("="*56)
    print("  MKV LUXURY - DAILY LEADS REPORT")
    print("  " + report_dt)
    print("  FTD date  : " + yesterday_key)
    print("  UTC window: " + yday_utc_start.strftime("%Y-%m-%d %H:%M") +
          " → " + yday_utc_end.strftime("%Y-%m-%d %H:%M"))
    print("="*56)

    print("\n[1/3] Loading MTD store...")
    store = load_mtd_store()
    print("  Month: " + store["month"] + " | Days stored: " + str(len(store.get("days",{}))))

    print("\n[2/3] Fetching conversations from Gallabox...")
    gallabox  = process_gallabox(store)
    ftd_total = sum(v["recd"] for v in gallabox["ftd_agents"].values())
    mtd_total = sum(v["recd"] for v in gallabox["mtd_agents"].values())
    print("\n  ✅ FTD leads: " + str(ftd_total) + " | MTD leads: " + str(mtd_total))

    print("\n[3/3] Sending to Slack...")
    msg = build_msg_gallabox(gallabox)
    send_to_slack(msg, [WEBHOOK_LEAD_REPORT])
    print("  Done.")
    print("="*56)

if __name__ == "__main__":
    main()
