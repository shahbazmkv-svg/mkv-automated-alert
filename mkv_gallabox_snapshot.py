import requests
import json
import os
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Credentials (env vars preferred; literals as fallback) ────────────────────
ACCOUNT_ID           = os.environ.get("GALLABOX_ACCOUNT_ID", "66e3f05033e71154d5fdd76c")
API_KEY              = os.environ.get("GALLABOX_API_KEY",    "69e7694e2da59f609317986b")
API_SECRET           = os.environ.get("GALLABOX_API_SECRET", "984394d316324482a8615eba6742b3ab")
WEBHOOK_LEAD_REPORT  = os.environ.get("WEBHOOK_LEADS",       "https://hooks.slack.com/services/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")
WEBHOOK_CUST_SERVICE = "https://hooks.slack.com/services/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"

# ── Store path: relative to script — works on GitHub Actions (Linux) & local ──
MTD_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mtd_store.json")

GALLABOX_HEADERS = {"apiKey": API_KEY, "apiSecret": API_SECRET, "Content-Type": "application/json"}
BASE_URL = "https://server.gallabox.com/devapi/accounts/" + ACCOUNT_ID

GALLABOX_CHANNELS = [
    {"name": "MKV Luxury Main",       "id": "675a90ddda3020e52915beff"},
    {"name": "MKV Luxury Car Rental", "id": "66e930025e9ef7252ccc8a25"},
    {"name": "Rent to Own",           "id": "699d8cca452cc56936e21e45"},
]

# ── Time references (Dubai = UTC+4) ──────────────────────────────────────────
dubai_tz        = timezone(timedelta(hours=4))
utc_tz          = timezone.utc
now_dubai       = datetime.now(dubai_tz)
yesterday_dubai = now_dubai - timedelta(days=1)

report_dt     = now_dubai.strftime("%d %b %Y | %I:%M %p Dubai Time")
yesterday_str = yesterday_dubai.strftime("%d %b %Y")
yesterday_key = yesterday_dubai.strftime("%Y-%m-%d")
cur_month     = now_dubai.strftime("%Y-%m")

# Yesterday full day in UTC
yday_utc_start = yesterday_dubai.replace(hour=0,  minute=0,  second=0,  microsecond=0).astimezone(utc_tz)
yday_utc_end   = yesterday_dubai.replace(hour=23, minute=59, second=59, microsecond=0).astimezone(utc_tz)


# ══════════════════════════════════════════════════════════════════════════════
# MTD STORE
#
#  One JSON file committed to the repo by GitHub Actions after every run.
#  Structure:
#  {
#    "month": "2026-05",
#    "days": {
#      "2026-05-01": { "a_r_Rafithath-MKV": 9, "a_t_Rafithath-MKV": 8,
#                      "s_Google Ads": 5, "g_Lead created": 6, ... },
#      "2026-05-02": { ... },
#      ...
#    }
#  }
#
#  Rules:
#  • Each day is written ONCE (idempotent — re-running won't double-count).
#  • New month → store auto-resets.
#  • MTD = sum of ALL days currently in store["days"].
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
    except Exception as e:
        print("  [WARN] Could not load MTD store (" + str(e) + ") — starting fresh")
    return {"month": cur_month, "days": {}}


def save_mtd_store(store):
    store["month"] = cur_month          # self-heal: always stamp correct month
    with open(MTD_STORE, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, ensure_ascii=False)


def append_day_to_store(store, day_key, snap):
    """
    Write snap for day_key ONLY if that day is not already stored.
    Idempotent: running twice on the same day will NOT overwrite or double-count.
    """
    if day_key in store["days"]:
        print("  [STORE] " + day_key + " already in store — skipping overwrite")
    else:
        store["days"][day_key] = snap
        print("  [STORE] Saved " + day_key)
    save_mtd_store(store)
    return store


def sum_mtd(store):
    """Add up every day in the store into one flat dict."""
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
        r = requests.get(BASE_URL + "/contacts/" + contact_id,
                         headers=GALLABOX_HEADERS, timeout=10)
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


def fetch_yesterday_conversations():
    """
    Fetch ONLY yesterday's conversations from Gallabox.

    - Pulls up to 5 pages x 100 per channel (500 per channel).
    - Filters strictly by createdAt within [yday_utc_start, yday_utc_end].
    - Stops paging a channel the moment convs go older than yesterday
      (API returns newest-first, so once we're past the window we're done).
    - Deduplicates across channels by conversation ID.
    """
    all_convs = []
    seen_ids  = set()

    for ch in GALLABOX_CHANNELS:
        print("    Channel: " + ch["name"])
        for page in range(1, 6):
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
                data  = r.json()
                convs = data if isinstance(data, list) else data.get("data", [])
                if not convs:
                    break

                past_window = False
                for conv in convs:
                    ts = parse_utc(conv.get("createdAt", ""))
                    if ts is None:
                        continue
                    if ts < yday_utc_start:
                        # Older than yesterday — no point paging further
                        past_window = True
                        break
                    if ts > yday_utc_end:
                        continue            # future / today — skip
                    conv_id = conv.get("_id") or conv.get("id", "")
                    if conv_id in seen_ids:
                        continue
                    seen_ids.add(conv_id)
                    all_convs.append({"conv": conv, "ts": ts})

                if past_window:
                    break

            except Exception as e:
                print("    [ERROR] " + ch["name"] + " p" + str(page) + ": " + str(e))
                break

    print("  Total yesterday convs found: " + str(len(all_convs)))
    return all_convs


# ══════════════════════════════════════════════════════════════════════════════
# BUILD SNAPSHOT  (agents / sources / stages + flat store dict)
# ══════════════════════════════════════════════════════════════════════════════

def build_snapshot(convs):
    """
    Enrich convs with contact details and return:
      agents  — { name: {recd, trig} }
      sources — { source: count }
      stages  — { stage: count }
      snap    — flat dict for MTD store  { "a_r_X": n, "s_X": n, ... }
    """
    contact_ids = list(set(
        c["conv"].get("contactId") or c["conv"].get("contact", {}).get("id", "")
        for c in convs
        if c["conv"].get("contactId") or c["conv"].get("contact", {}).get("id", "")
    ))
    contact_cache = {}
    if contact_ids:
        with ThreadPoolExecutor(max_workers=10) as executor:
            fut_map = {executor.submit(fetch_contact_details, cid): cid for cid in contact_ids}
            for fut in as_completed(fut_map):
                cid = fut_map[fut]
                try:    contact_cache[cid] = fut.result()
                except: contact_cache[cid] = {"triggered": False, "source": "Instagram DMs", "stage": "Unknown"}

    agents = {}; sources = {}; stages = {}; snap = {}

    for item in convs:
        conv  = item["conv"]
        uname = (conv.get("user") or {}).get("name") or ""
        agent = uname.strip() or "Unassigned"
        cid   = conv.get("contactId") or conv.get("contact", {}).get("id", "")
        det   = contact_cache.get(cid, {"triggered": False, "source": "Instagram DMs", "stage": "Unknown"})
        src   = det["source"]
        stg   = det["stage"]
        trig  = 1 if det["triggered"] else 0

        agents.setdefault(agent, {"recd": 0, "trig": 0})
        agents[agent]["recd"] += 1
        agents[agent]["trig"] += trig
        sources[src] = sources.get(src, 0) + 1
        stages[stg]  = stages.get(stg,  0) + 1

        snap["a_r_" + agent] = snap.get("a_r_" + agent, 0) + 1
        snap["a_t_" + agent] = snap.get("a_t_" + agent, 0) + trig
        snap["s_"   + src]   = snap.get("s_"   + src,   0) + 1
        snap["g_"   + stg]   = snap.get("g_"   + stg,   0) + 1

    return agents, sources, stages, snap


def unpack_flat(flat):
    """Unpack a flat MTD dict back into agents / sources / stages."""
    agents = {}; sources = {}; stages = {}
    for k, v in flat.items():
        if   k.startswith("a_r_"): n = k[4:]; agents.setdefault(n, {"recd":0,"trig":0}); agents[n]["recd"] += v
        elif k.startswith("a_t_"): n = k[4:]; agents.setdefault(n, {"recd":0,"trig":0}); agents[n]["trig"] += v
        elif k.startswith("s_"):   sources[k[2:]] = sources.get(k[2:], 0) + v
        elif k.startswith("g_"):   stages[k[2:]]  = stages.get(k[2:],  0) + v
    return agents, sources, stages


# ══════════════════════════════════════════════════════════════════════════════
# SLACK MESSAGE
# ══════════════════════════════════════════════════════════════════════════════

def build_slack_message(yday_agents, yday_sources, yday_stages,
                        mtd_agents,  mtd_sources,  mtd_stages,
                        days_in_mtd):

    # Agent table
    all_agents = sorted(set(list(yday_agents.keys()) + list(mtd_agents.keys())))
    ag  = "{:<20} {:>6} {:>6} {:>7} {:>7}\n".format("Agent", "F.Rec", "F.Trg", "M.Rec", "M.Trg")
    ag += "-" * 50 + "\n"
    for name in all_agents:
        y = yday_agents.get(name, {"recd": 0, "trig": 0})
        m = mtd_agents.get(name,  {"recd": 0, "trig": 0})
        ag += "{:<20} {:>6} {:>6} {:>7} {:>7}\n".format(
            name[:20], y["recd"], y["trig"], m["recd"], m["trig"])
    ag += "-" * 50 + "\n"
    ag += "{:<20} {:>6} {:>6} {:>7} {:>7}".format(
        "TOTAL",
        sum(v["recd"] for v in yday_agents.values()),
        sum(v["trig"] for v in yday_agents.values()),
        sum(v["recd"] for v in mtd_agents.values()),
        sum(v["trig"] for v in mtd_agents.values()),
    )

    # Source table
    SOURCE_ORDER = ["Google Ads", "Facebook / Instagram", "Instagram DMs", "OneClickDrive", "Website"]
    all_srcs = SOURCE_ORDER + [s for s in sorted(
        set(list(yday_sources.keys()) + list(mtd_sources.keys()))
    ) if s not in SOURCE_ORDER]
    src  = "{:<24} {:>5}  {:>7}\n".format("Source", "FTD", "MTD") + "-" * 38 + "\n"
    for s in all_srcs:
        if yday_sources.get(s, 0) > 0 or mtd_sources.get(s, 0) > 0:
            src += "{:<24} {:>5}  {:>7}\n".format(s[:24], yday_sources.get(s, 0), mtd_sources.get(s, 0))
    src += "-" * 38 + "\n"
    src += "{:<24} {:>5}  {:>7}".format("TOTAL", sum(yday_sources.values()), sum(mtd_sources.values()))

    # Stage table
    STAGE_ORDER = ["Lead created", "Qualified lead", "Converted lead", "Unknown"]
    all_stgs = STAGE_ORDER + [s for s in sorted(
        set(list(yday_stages.keys()) + list(mtd_stages.keys()))
    ) if s not in STAGE_ORDER]
    stg  = "{:<24} {:>5}  {:>7}\n".format("Stage", "FTD", "MTD") + "-" * 38 + "\n"
    for s in all_stgs:
        if yday_stages.get(s, 0) > 0 or mtd_stages.get(s, 0) > 0:
            stg += "{:<24} {:>5}  {:>7}\n".format(s[:24], yday_stages.get(s, 0), mtd_stages.get(s, 0))
    stg += "-" * 38 + "\n"
    stg += "{:<24} {:>5}  {:>7}".format("TOTAL", sum(yday_stages.values()), sum(mtd_stages.values()))

    return (
        ":bar_chart: *MKV LUXURY — LEADS & AGENTS REPORT*\n"
        + ":calendar: " + report_dt + "\n"
        + "_FTD = " + yesterday_str + " (00:01–23:59) | MTD = 1st–" + yesterday_str + " (" + str(days_in_mtd) + " days)_\n\n"
        + "*:dart: AGENT PERFORMANCE*\n```" + ag + "```\n"
        + "*:globe_with_meridians: LEAD SOURCE*\n```" + src + "```\n"
        + "*:chart_with_upwards_trend: LEAD STAGE*\n```" + stg + "```"
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
    print("=" * 56)
    print("  MKV LUXURY - DAILY LEADS REPORT")
    print("  " + report_dt)
    print("  Reporting for yesterday: " + yesterday_str)
    print("=" * 56)

    # Step 1 — Load MTD store from repo
    print("\n[1/4] Loading MTD store...")
    store = load_mtd_store()
    print("  Month: " + store["month"] + " | Days in store: " + str(len(store.get("days", {}))))

    # Step 2 — Fetch ONLY yesterday from Gallabox API
    print("\n[2/4] Fetching yesterday from Gallabox API...")
    print("  Window (UTC): " + yday_utc_start.strftime("%Y-%m-%d %H:%M") +
          " → " + yday_utc_end.strftime("%Y-%m-%d %H:%M"))

    yday_convs = fetch_yesterday_conversations()

    if yday_convs:
        yday_agents, yday_sources, yday_stages, yday_snap = build_snapshot(yday_convs)
    else:
        # Fallback: use whatever is in the store for yesterday (handles re-runs)
        print("  No API convs — falling back to stored snapshot for " + yesterday_key)
        existing = store.get("days", {}).get(yesterday_key, {})
        yday_snap = existing
        yday_agents = {}; yday_sources = {}; yday_stages = {}
        for k, v in existing.items():
            if   k.startswith("a_r_"): n = k[4:]; yday_agents.setdefault(n, {"recd":0,"trig":0}); yday_agents[n]["recd"] += v
            elif k.startswith("a_t_"): n = k[4:]; yday_agents.setdefault(n, {"recd":0,"trig":0}); yday_agents[n]["trig"] += v
            elif k.startswith("s_"):   yday_sources[k[2:]] = yday_sources.get(k[2:], 0) + v
            elif k.startswith("g_"):   yday_stages[k[2:]]  = yday_stages.get(k[2:],  0) + v

    yday_total = sum(v["recd"] for v in yday_agents.values())
    print("  Yesterday leads: " + str(yday_total))

    # Step 3 — MTD = store (1st → day-before-yesterday) + FTD (yesterday)
    #
    #  If today is 11th running at 11 AM:
    #    FTD  = 10th  00:01–23:59  (fresh from API this run)
    #    store = 1st…9th           (saved by all prior daily runs)
    #    MTD  = 1st…9th + 10th  =  1st…10th  ✅
    #
    #  After reporting, 10th is appended to the store so tomorrow
    #  the store holds 1st…10th and FTD will be 11th.
    #
    print("\n[3/4] Computing MTD...")
    prior_flat = sum_mtd(store)                      # 1st → day-before-yesterday
    days_prior = len(store["days"])

    # MTD flat = prior store + FTD snap (yesterday)
    mtd_flat = dict(prior_flat)
    for k, v in yday_snap.items():
        mtd_flat[k] = mtd_flat.get(k, 0) + v
    mtd_agents, mtd_sources, mtd_stages = unpack_flat(mtd_flat)

    days_in_mtd = days_prior + 1
    mtd_total   = sum(v["recd"] for v in mtd_agents.values())
    print("  Store (prior days): " + str(days_prior) + " | FTD: " +
          str(sum(v["recd"] for v in yday_agents.values())) +
          " | MTD total: " + str(mtd_total))

    # Append FTD (yesterday) to store NOW — ready for tomorrow's run
    store = append_day_to_store(store, yesterday_key, yday_snap)

    # Step 4 — Send to Slack
    print("\n[4/4] Sending to Slack...")
    msg = build_slack_message(
        yday_agents, yday_sources, yday_stages,
        mtd_agents,  mtd_sources,  mtd_stages,
        days_in_mtd,
    )
    send_to_slack(msg, [WEBHOOK_LEAD_REPORT])

    print("\n" + "=" * 56)
    print("  Done.")
    print("=" * 56)


if __name__ == "__main__":
    main()
