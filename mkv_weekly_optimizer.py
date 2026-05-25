"""
MKV Weekly Optimizer — Monday Consolidated Report
==================================================
Version : 2.0  (26 May 2026)

Mirrors the daily report pattern — 3 separate Slack posts:
  Post 1 → 🔵 Google Ads Weekly (7-day) + Gallabox Google leads
  Post 2 → 🟦 Meta Ads Weekly (MKV Luxury + Lease to Own) + Gallabox Meta leads
  Post 3 → 📊 Correlation Summary + Optimization Actions

Channel : #mkv-marketing-team (C0AASQKLY59)
Schedule: Every Monday 08:00 AM Dubai time (04:00 UTC)

GitHub Actions cron: 0 4 * * 1
Run manually       : python mkv_weekly_optimizer.py
Force run (any day): python mkv_weekly_optimizer.py --now
Test mode          : TEST_MODE=true python mkv_weekly_optimizer.py --now

GitHub Secrets required:
  SLACK_BOT_TOKEN
  GOOGLE_ADS_DEVELOPER_TOKEN
  GOOGLE_ADS_CLIENT_ID
  GOOGLE_ADS_CLIENT_SECRET
  GOOGLE_ADS_REFRESH_TOKEN
  GOOGLE_ADS_CUSTOMER_ID          (3847584613)
  GOOGLE_ADS_LOGIN_CUSTOMER_ID
  GALLABOX_API_KEY                (6a1064ed5a8546db4ab5870b)
  GALLABOX_API_SECRET             (e9e9903954a645f3adf7be9a86d7a4d2)
  GALLABOX_ACCOUNT_ID             (66e3f05033e71154d5fdd76c)
  META_TOKEN                      (contents of meta_token.txt)

WoW delta stored in: mkv_weekly_store.json  (committed to repo via workflow)
"""

import os
import json
import sys
import time
import urllib.request
import urllib.parse
import requests
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from google.ads.googleads.client import GoogleAdsClient
    from google.ads.googleads.errors import GoogleAdsException
    GOOGLE_ADS_AVAILABLE = True
except ImportError:
    GOOGLE_ADS_AVAILABLE = False
    print("⚠️  google-ads not installed — Google data skipped")

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════

SLACK_TOKEN       = os.environ.get("SLACK_BOT_TOKEN", "")
TEST_MODE         = os.environ.get("TEST_MODE", "false").lower() == "true"
SLACK_CHANNEL     = "C0B0TGBDCDU" if TEST_MODE else "C0AASQKLY59"

# Google Ads
DEVELOPER_TOKEN   = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN", "")
CLIENT_ID         = os.environ.get("GOOGLE_ADS_CLIENT_ID", "")
CLIENT_SECRET     = os.environ.get("GOOGLE_ADS_CLIENT_SECRET", "")
REFRESH_TOKEN     = os.environ.get("GOOGLE_ADS_REFRESH_TOKEN", "")
CUSTOMER_ID       = os.environ.get("GOOGLE_ADS_CUSTOMER_ID", "3847584613")
LOGIN_CUSTOMER_ID = os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "")

# Meta — token comes from GitHub Secret (mirrors meta_token.txt content)
META_TOKEN        = os.environ.get("META_TOKEN", "")
MKV_ACCOUNT       = "699611181993619"
RTO_ACCOUNT       = "900731551390821"

# Gallabox
GALLABOX_ACCOUNT  = os.environ.get("GALLABOX_ACCOUNT_ID", "66e3f05033e71154d5fdd76c")
GALLABOX_KEY      = os.environ.get("GALLABOX_API_KEY",    "6a1064ed5a8546db4ab5870b")
GALLABOX_SECRET   = os.environ.get("GALLABOX_API_SECRET", "e9e9903954a645f3adf7be9a86d7a4d2")
GALLABOX_HEADERS  = {"apiKey": GALLABOX_KEY, "apiSecret": GALLABOX_SECRET, "Content-Type": "application/json"}
GALLABOX_BASE     = f"https://server.gallabox.com/devapi/accounts/{GALLABOX_ACCOUNT}"
GALLABOX_CHANNELS = [
    {"name": "MKV Luxury Main",       "id": "675a90ddda3020e52915beff"},
    {"name": "MKV Luxury Car Rental", "id": "66e930025e9ef7252ccc8a25"},
    {"name": "Rent to Own",           "id": "699d8cca452cc56936e21e45"},
]

# Lead source → channel bucket (matches Gallabox fieldValuesKV lead_source)
SOURCE_TO_CHANNEL = {
    "Google Ads"           : "google",
    "Facebook / Instagram" : "meta",
    "Instagram DMs"        : "meta",
    "Facebook"             : "meta",
    "Instagram"            : "meta",
    "OneClickDrive"        : "organic",
    "Website"              : "organic",
}

WEEKLY_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mkv_weekly_store.json")
dubai_tz     = timezone(timedelta(hours=4))

# ══════════════════════════════════════════════════════════════════════════════
# DATE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def week_range():
    """Mon–Sun of LAST week as YYYY-MM-DD strings."""
    today = datetime.now(dubai_tz).date()
    start = today - timedelta(days=today.weekday() + 7)
    end   = start + timedelta(days=6)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

def week_label():
    s, e = week_range()
    return (f"{datetime.strptime(s,'%Y-%m-%d').strftime('%d %b')} – "
            f"{datetime.strptime(e,'%Y-%m-%d').strftime('%d %b %Y')}")

# ══════════════════════════════════════════════════════════════════════════════
# WoW DELTA STORE
# ══════════════════════════════════════════════════════════════════════════════

def load_store():
    try:
        if os.path.exists(WEEKLY_STORE):
            with open(WEEKLY_STORE) as f:
                return json.load(f)
    except:
        pass
    return {}

def save_store(data):
    try:
        with open(WEEKLY_STORE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"  ⚠️  Store save failed: {e}")

def wow(cur, prev, prefix="AED ", is_count=False):
    """Returns '  (+AED 123 ↑)' style delta string."""
    if not prev:
        return ""
    diff  = cur - prev
    if diff == 0:
        return ""
    arrow = "↑" if diff > 0 else "↓"
    if is_count:
        return f"  ({'+' if diff>0 else ''}{int(diff)} {arrow})"
    return f"  ({'+' if diff>0 else ''}{prefix}{abs(diff):,.0f} {arrow})"

# ══════════════════════════════════════════════════════════════════════════════
# GOOGLE ADS — 7-DAY
# ══════════════════════════════════════════════════════════════════════════════

def get_google_client():
    if not GOOGLE_ADS_AVAILABLE or not DEVELOPER_TOKEN:
        return None
    try:
        return GoogleAdsClient.load_from_dict({
            "developer_token"  : DEVELOPER_TOKEN,
            "client_id"        : CLIENT_ID,
            "client_secret"    : CLIENT_SECRET,
            "refresh_token"    : REFRESH_TOKEN,
            "login_customer_id": LOGIN_CUSTOMER_ID,
            "use_proto_plus"   : True,
        })
    except Exception as e:
        print(f"  ❌ Google client: {e}")
        return None

def g_query(client, gaql):
    svc = client.get_service("GoogleAdsService")
    return svc.search(customer_id=CUSTOMER_ID, query=gaql)

def fetch_google_summary(client):
    if not client: return {}
    s, e = week_range()
    try:
        rows = g_query(client, f"""
            SELECT metrics.impressions, metrics.clicks,
                   metrics.cost_micros, metrics.conversions, metrics.ctr
            FROM customer
            WHERE segments.date BETWEEN '{s}' AND '{e}'
        """)
        impr = clks = cost = conv = 0
        for r in rows:
            impr += r.metrics.impressions
            clks += r.metrics.clicks
            cost += r.metrics.cost_micros / 1_000_000
            conv += r.metrics.conversions
        ctr = round(clks/impr*100,2) if impr else 0
        cpc = round(cost/clks,2)     if clks  else 0
        cpa = round(cost/conv,2)     if conv   else 0
        print(f"  ✅ Google 7d: AED {cost:.2f} | {clks} clicks | {conv:.1f} conv | CTR {ctr}%")
        return {"impressions":int(impr),"clicks":int(clks),"cost":round(cost,2),
                "conversions":round(conv,1),"ctr":ctr,"cpc":cpc,"cpa":cpa}
    except Exception as e:
        print(f"  ❌ Google summary: {e}"); return {}

def fetch_google_campaigns(client):
    if not client: return []
    s, e = week_range()
    try:
        rows = g_query(client, f"""
            SELECT campaign.name, metrics.impressions, metrics.clicks,
                   metrics.cost_micros, metrics.conversions, metrics.ctr
            FROM campaign
            WHERE segments.date BETWEEN '{s}' AND '{e}'
              AND campaign.status = 'ENABLED'
              AND metrics.cost_micros > 0
            ORDER BY metrics.cost_micros DESC LIMIT 6
        """)
        camps = []
        for r in rows:
            cost = r.metrics.cost_micros / 1_000_000
            conv = r.metrics.conversions
            clks = r.metrics.clicks
            impr = r.metrics.impressions
            camps.append({
                "name": r.campaign.name[:38],
                "cost": round(cost,2), "clicks": int(clks),
                "conv": round(conv,1), "ctr": round(clks/impr*100,2) if impr else 0,
                "cpa" : round(cost/conv,2) if conv else 0,
            })
        return camps
    except Exception as e:
        print(f"  ⚠️  Google campaigns: {e}"); return []

def fetch_google_search_terms(client):
    if not client: return []
    s, e = week_range()
    try:
        rows = g_query(client, f"""
            SELECT search_term_view.search_term,
                   metrics.clicks, metrics.cost_micros, metrics.conversions
            FROM search_term_view
            WHERE segments.date BETWEEN '{s}' AND '{e}'
              AND metrics.clicks > 0
            ORDER BY metrics.clicks DESC LIMIT 5
        """)
        return [{"term": r.search_term_view.search_term[:42],
                 "clicks": r.metrics.clicks,
                 "conv":   round(r.metrics.conversions,1),
                 "cost":   round(r.metrics.cost_micros/1_000_000,2)} for r in rows]
    except Exception as e:
        print(f"  ⚠️  Google search terms: {e}"); return []

# ══════════════════════════════════════════════════════════════════════════════
# META ADS — 7-DAY (via META_TOKEN secret = meta_token.txt content)
# ══════════════════════════════════════════════════════════════════════════════

def meta_api(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    r   = urllib.request.urlopen(req, timeout=30)
    return json.loads(r.read().decode())

def check_token_expiry(token):
    try:
        d = meta_api(
            f"https://graph.facebook.com/debug_token"
            f"?input_token={token}&access_token={token}"
        )
        exp = d.get("data",{}).get("expires_at", 0)
        if exp == 0: return 999
        return max(0, (exp - int(time.time())) // 86400)
    except:
        return None

def fetch_meta_account(account_id, token, name):
    s, e      = week_range()
    time_range = urllib.parse.quote(json.dumps({"since": s, "until": e}))
    try:
        d = meta_api(
            f"https://graph.facebook.com/v20.0/act_{account_id}/insights"
            f"?fields=spend,impressions,clicks,reach,actions,frequency"
            f"&time_range={time_range}&level=account&access_token={token}"
        )
        if not d.get("data"):
            print(f"  ⚠️  No Meta data: {name}"); return {}
        row   = d["data"][0]
        spent = round(float(row.get("spend",0)),2)
        impr  = int(row.get("impressions",0))
        clks  = int(row.get("clicks",0))
        reach = int(row.get("reach",0))
        freq  = round(float(row.get("frequency",0)),2)
        results = max(
            (int(a.get("value",0)) for a in row.get("actions",[])
             if a["action_type"]=="onsite_conversion.total_messaging_connection"),
            default=0)
        ctr = round(clks/impr*100,2) if impr else 0
        cpr = round(spent/results,2)  if results else 0
        print(f"  ✅ Meta 7d {name}: AED {spent} | {results} results | CTR {ctr}% | Freq {freq}")
        return {"spent":spent,"impressions":impr,"clicks":clks,"reach":reach,
                "results":results,"ctr":ctr,"cpr":cpr,"frequency":freq}
    except Exception as e:
        print(f"  ❌ Meta {name}: {e}"); return {}

def fetch_meta_campaigns(account_id, token):
    s, e      = week_range()
    tr_enc    = urllib.parse.quote(json.dumps({"since":s,"until":e}))
    tr_raw    = f'{{"since":"{s}","until":"{e}"}}'
    try:
        d = meta_api(
            f"https://graph.facebook.com/v20.0/act_{account_id}/campaigns"
            f"?fields=name,insights.time_range({tr_raw}){{spend,impressions,clicks,actions}}"
            f"&access_token={token}"
        )
        camps = []
        for c in d.get("data",[]):
            ins = c.get("insights",{}).get("data",[])
            if not ins: continue
            i   = ins[0]
            sp  = round(float(i.get("spend",0)),2)
            if sp == 0: continue
            clks = int(i.get("clicks",0))
            impr = int(i.get("impressions",0))
            res  = max(
                (int(a.get("value",0)) for a in i.get("actions",[])
                 if a["action_type"]=="onsite_conversion.total_messaging_connection"),
                default=0)
            camps.append({
                "name":c.get("name","")[:38],"spend":sp,"clicks":clks,
                "results":res,"ctr":round(clks/impr*100,2) if impr else 0,
                "cpr":round(sp/res,2) if res else 0,
            })
        camps.sort(key=lambda x: x["spend"], reverse=True)
        return camps[:4]
    except Exception as e:
        print(f"  ⚠️  Meta campaigns: {e}"); return []

def fetch_meta_placement(account_id, token):
    s, e = week_range()
    tr   = urllib.parse.quote(json.dumps({"since":s,"until":e}))
    try:
        d = meta_api(
            f"https://graph.facebook.com/v20.0/act_{account_id}/insights"
            f"?fields=spend,impressions,clicks,reach"
            f"&time_range={tr}&breakdowns=publisher_platform"
            f"&level=account&access_token={token}"
        )
        rows = []
        for row in sorted(d.get("data",[]),key=lambda x:float(x.get("spend",0)),reverse=True)[:4]:
            sp   = round(float(row.get("spend",0)),2)
            if sp == 0: continue
            plat = row.get("publisher_platform","").replace("_"," ").title()
            clks = int(row.get("clicks",0))
            impr = int(row.get("impressions",0))
            reach= int(row.get("reach",0))
            ctr  = round(clks/impr*100,2) if impr else 0
            rows.append(f"• {plat:<14}  AED {sp:,.0f} | {clks:,} clicks | Reach {reach:,} | CTR {ctr}%")
        return rows
    except Exception as e:
        print(f"  ⚠️  Meta placement: {e}"); return []

# ══════════════════════════════════════════════════════════════════════════════
# GALLABOX — 7-DAY LEADS
# ══════════════════════════════════════════════════════════════════════════════

def parse_utc(ts):
    try: return datetime.fromisoformat(ts.replace("Z","+00:00"))
    except: return None

def fetch_contact(contact_id):
    try:
        r = requests.get(GALLABOX_BASE+"/contacts/"+contact_id,
                         headers=GALLABOX_HEADERS, timeout=10)
        r.raise_for_status()
        d  = r.json()
        kv = d.get("fieldValuesKV",{}) or {}
        src  = (kv.get("lead_source","") or "").strip() or "Instagram DMs"
        stg  = (kv.get("lead_stage","")  or "").strip() or "Unknown"
        tags = [t.get("name","").lower() for t in (d.get("tags") or [])]
        return {"source":src,"stage":stg,"triggered":"triggered" in tags}
    except:
        return {"source":"Instagram DMs","stage":"Unknown","triggered":False}

def fetch_gallabox_weekly():
    s, e     = week_range()
    start_dt = datetime.strptime(s,"%Y-%m-%d").replace(hour=0,minute=0,second=0,tzinfo=timezone.utc)
    end_dt   = datetime.strptime(e,"%Y-%m-%d").replace(hour=23,minute=59,second=59,tzinfo=timezone.utc)
    print(f"  Fetching Gallabox {s} → {e}...")

    all_convs = []; seen = set()
    for ch in GALLABOX_CHANNELS:
        for page in range(1, 7):
            try:
                r = requests.get(GALLABOX_BASE+"/conversations",
                    headers=GALLABOX_HEADERS,
                    params={"limit":100,"page":page,"channelId":ch["id"],"channelType":"whatsapp"},
                    timeout=15)
                r.raise_for_status()
                data  = r.json()
                convs = data if isinstance(data,list) else data.get("data",[])
                if not convs: break
                new = False
                for c in convs:
                    cid = c.get("_id") or c.get("id","")
                    if cid in seen: continue
                    seen.add(cid)
                    ts = parse_utc(c.get("createdAt",""))
                    if ts and start_dt <= ts <= end_dt:
                        all_convs.append({"conv":c,"ts":ts}); new = True
                if not new: break
            except Exception as ex:
                print(f"  [ERR] Gallabox {ch['name']} p{page}: {ex}"); break

    print(f"  Conversations in window: {len(all_convs)}")
    if not all_convs:
        return {"sources":{},"stages":{},"agents":{},"total":0}

    cids = list(set(
        c["conv"].get("contactId") or c["conv"].get("contact",{}).get("id","")
        for c in all_convs if c["conv"].get("contactId") or c["conv"].get("contact",{}).get("id","")
    ))
    cache = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(fetch_contact, cid): cid for cid in cids}
        for fut in as_completed(futs):
            cid = futs[fut]
            try:    cache[cid] = fut.result()
            except: cache[cid] = {"source":"Instagram DMs","stage":"Unknown","triggered":False}

    sources = {}; stages = {}; agents = {}
    for item in all_convs:
        conv  = item["conv"]
        cid   = conv.get("contactId") or conv.get("contact",{}).get("id","")
        det   = cache.get(cid, {"source":"Instagram DMs","stage":"Unknown"})
        src   = det["source"]; stg = det["stage"]
        agent = (conv.get("user") or {}).get("name","").strip() or "Unassigned"
        sources[src]  = sources.get(src,0)  + 1
        stages[stg]   = stages.get(stg,0)   + 1
        agents[agent] = agents.get(agent,0) + 1

    return {"sources":sources,"stages":stages,"agents":agents,"total":len(all_convs)}

# ══════════════════════════════════════════════════════════════════════════════
# CORRELATION ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def correlate(google, meta_mkv, meta_rto, gal):
    src = gal.get("sources",{})
    stg = gal.get("stages",{})

    g_leads  = src.get("Google Ads",0)
    m_leads  = sum(src.get(k,0) for k in ["Facebook / Instagram","Instagram DMs","Facebook","Instagram"])
    org_leads= sum(v for k,v in src.items() if SOURCE_TO_CHANNEL.get(k,"organic")=="organic")

    g_spend  = google.get("cost",0)
    m_spend  = meta_mkv.get("spent",0) + meta_rto.get("spent",0)
    total_sp = g_spend + m_spend
    total_l  = gal["total"]

    g_cpl    = round(g_spend/g_leads,2) if g_leads else 0
    m_cpl    = round(m_spend/m_leads,2) if m_leads else 0
    b_cpl    = round(total_sp/total_l,2) if total_l else 0

    qual     = stg.get("Qualified lead",0) + stg.get("Converted lead",0)
    qual_pct = round(qual/total_l*100,1) if total_l else 0
    conv_pct = round(stg.get("Converted lead",0)/total_l*100,1) if total_l else 0

    if g_cpl and m_cpl:
        winner = "🔵 Google" if g_cpl <= m_cpl else "🟦 Meta"
    elif g_cpl: winner = "🔵 Google"
    elif m_cpl: winner = "🟦 Meta"
    else:       winner = "—"

    return {
        "g_leads":g_leads,"m_leads":m_leads,"org_leads":org_leads,
        "g_spend":g_spend,"m_spend":m_spend,
        "g_cpl":g_cpl,"m_cpl":m_cpl,"b_cpl":b_cpl,
        "qual_pct":qual_pct,"conv_pct":conv_pct,"winner":winner,
        "total_spend":total_sp,"total_leads":total_l,
    }

# ══════════════════════════════════════════════════════════════════════════════
# RECOMMENDATIONS
# ══════════════════════════════════════════════════════════════════════════════

def recommendations(google, meta_mkv, meta_rto, gal, corr):
    recs = []

    if meta_mkv.get("frequency",0) >= 3.0:
        recs.append(("🔴",1,"Refresh Meta creatives — MKV Luxury",
            f"Frequency {meta_mkv['frequency']} ≥ 3.0 = audience fatigue. New visuals needed this week."))
    if meta_rto.get("frequency",0) >= 3.0:
        recs.append(("🔴",1,"Refresh Meta creatives — Lease to Own",
            f"Frequency {meta_rto['frequency']} ≥ 3.0 = audience fatigue."))

    if google.get("conversions",0)==0 and google.get("cost",0)>0:
        recs.append(("🔴",1,"Check Google conversion tracking",
            "Zero conversions recorded despite spend. Verify tags & landing pages."))

    if corr["g_cpl"] > 150:
        recs.append(("🔴",2,"Review Google keywords & negative list",
            f"Google CPL AED {corr['g_cpl']:,.0f} is above AED 150 target. Pause low-performers."))
    if corr["m_cpl"] > 150:
        recs.append(("🔴",2,"Review Meta audience targeting",
            f"Meta CPL AED {corr['m_cpl']:,.0f} is above AED 150 target."))

    if google.get("ctr",0) < 2.0 and google.get("impressions",0) > 500:
        recs.append(("🟡",3,"Rewrite Google Ad headlines",
            f"CTR {google['ctr']}% is below 2% benchmark. Test 3 new headline variants."))

    if 0 < corr["g_cpl"] < corr["m_cpl"]*0.7 and corr["m_cpl"] > 0:
        recs.append(("💡",3,"Shift budget: Google is 30%+ cheaper per lead",
            f"Google CPL AED {corr['g_cpl']:,.0f} vs Meta CPL AED {corr['m_cpl']:,.0f}. Reallocate AED 500–1,000/wk."))
    elif 0 < corr["m_cpl"] < corr["g_cpl"]*0.7 and corr["g_cpl"] > 0:
        recs.append(("💡",3,"Shift budget: Meta is 30%+ cheaper per lead",
            f"Meta CPL AED {corr['m_cpl']:,.0f} vs Google CPL AED {corr['g_cpl']:,.0f}. Reallocate AED 500–1,000/wk."))

    if corr["qual_pct"] < 30 and corr["total_leads"] > 10:
        recs.append(("🟡",4,"Tighten targeting — low lead quality",
            f"Only {corr['qual_pct']}% qualified/converted. Narrow geo & audience segments."))

    if 0 < corr["g_cpl"] < 80 and corr["g_leads"] > 5:
        recs.append(("✅",5,"Scale Google budget — strong CPL",
            f"CPL AED {corr['g_cpl']:,.0f} is below AED 80. Safe to increase budget 20–30%."))
    if 0 < corr["m_cpl"] < 80 and meta_mkv.get("frequency",0) < 2.5 and corr["m_leads"] > 5:
        recs.append(("✅",5,"Scale Meta budget — efficient & low frequency",
            f"Meta CPL AED {corr['m_cpl']:,.0f}, frequency {meta_mkv.get('frequency',0)} — room to grow."))

    if corr["org_leads"] > corr["g_leads"] + corr["m_leads"]:
        recs.append(("💡",6,"Invest in SEO — organic outperforming paid",
            f"Organic leads ({corr['org_leads']}) exceed paid ({corr['g_leads']+corr['m_leads']})."))

    recs.sort(key=lambda x: x[1])
    return recs[:7]

# ══════════════════════════════════════════════════════════════════════════════
# SLACK HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def post_slack(blocks, fallback="MKV Weekly Report"):
    client = WebClient(token=SLACK_TOKEN)
    try:
        client.chat_postMessage(
            channel=SLACK_CHANNEL, blocks=blocks,
            text=fallback, unfurl_links=False, unfurl_media=False)
        print(f"  ✅  Posted → {SLACK_CHANNEL}")
    except SlackApiError as e:
        print(f"  ❌  Slack: {e.response['error']}")

def divider(): return {"type":"divider"}
def section(txt): return {"type":"section","text":{"type":"mrkdwn","text":txt}}
def header(txt):  return {"type":"header","text":{"type":"plain_text","text":txt,"emoji":True}}
def ctx(txt):     return {"type":"context","elements":[{"type":"mrkdwn","text":txt}]}

def mode_tag():
    now = datetime.now(dubai_tz).strftime("%d %b %Y %H:%M GST")
    return f"_MKV Luxury Car Rental  •  Weekly Optimizer v2.0  •  {now}  •  {'🧪 TEST' if TEST_MODE else '🚀 LIVE'}_"

# ══════════════════════════════════════════════════════════════════════════════
# POST 1 — GOOGLE ADS WEEKLY
# ══════════════════════════════════════════════════════════════════════════════

def post_google(google, camps, terms, gal, corr, prev, label):
    prev_g = prev.get("google",{})
    prev_c = prev.get("corr",{})

    if google:
        g_text = (
            f"*Spend:* AED {google['cost']:,.2f}{wow(google['cost'],prev_g.get('cost',0))}    "
            f"*Clicks:* {google['clicks']:,}{wow(google['clicks'],prev_g.get('clicks',0),is_count=True)}    "
            f"*Conv:* {google['conversions']}{wow(google['conversions'],prev_g.get('conversions',0),is_count=True)}\n"
            f"*CTR:* {google['ctr']}%    *CPC:* AED {google['cpc']}    *CPA:* AED {google['cpa']}\n"
            f"*WA Leads (Google source):* {corr['g_leads']}{wow(corr['g_leads'],prev_c.get('g_leads',0),is_count=True)}    "
            f"*Cost/WA Lead:* {'AED '+str(corr['g_cpl']) if corr['g_cpl'] else '—'}"
        )
    else:
        g_text = "_Google Ads data unavailable this week_"

    camp_text = "_No campaign data_"
    if camps:
        lines = []
        for c in camps[:5]:
            flag = " ⚡" if c["cpa"] > 300 else (" ✅" if 0 < c["cpa"] < 100 else "")
            lines.append(
                f"• `{c['name'][:36]}`\n"
                f"  AED {c['cost']:,.0f} | {c['clicks']} clicks | {c['conv']} conv | "
                f"CTR {c['ctr']}% | CPA AED {c['cpa']:,.0f}{flag}"
            )
        camp_text = "\n".join(lines)

    term_text = "_No search term data_"
    if terms:
        lines = [f"• \"{t['term']}\"  —  {t['clicks']} clicks | {t['conv']} conv | AED {t['cost']:,.0f}" for t in terms]
        term_text = "\n".join(lines)

    blocks = [
        header(f"🔵 Google Ads Weekly — {label}"),
        divider(),
        section(f"*📈 7-Day Performance*\n{g_text}"),
        divider(),
        section(f"*🏆 Top Campaigns*\n{camp_text}"),
        divider(),
        section(f"*🔍 Top Search Terms*\n{term_text}"),
        divider(),
        ctx(mode_tag()),
    ]
    post_slack(blocks, f"MKV Google Ads Weekly — {label}")

# ══════════════════════════════════════════════════════════════════════════════
# POST 2 — META ADS WEEKLY
# ══════════════════════════════════════════════════════════════════════════════

def post_meta(meta_mkv, meta_rto, mkv_camps, placements, gal, corr, prev, label, token_days):
    prev_m = prev.get("meta",{})
    prev_c = prev.get("corr",{})

    def fmt(data, name, prev_key):
        p = prev_m.get(prev_key,{})
        if not data.get("spent",0):
            return f"_No {name} data this week_"
        freq_warn = "  ⚠️ _Audience fatigue_" if data.get("frequency",0) >= 3.0 else ""
        return (
            f"*Spend:* AED {data['spent']:,.2f}{wow(data['spent'],p.get('spent',0))}    "
            f"*Results:* {data['results']}{wow(data['results'],p.get('results',0),is_count=True)}    "
            f"*CPR:* AED {data['cpr']:,.2f}\n"
            f"*Impressions:* {data['impressions']:,}    *Reach:* {data['reach']:,}    "
            f"*CTR:* {data['ctr']}%    *Freq:* {data.get('frequency',0)}{freq_warn}\n"
            f"*WA Leads (Meta source):* {corr['m_leads']}{wow(corr['m_leads'],prev_c.get('m_leads',0),is_count=True)}    "
            f"*Cost/WA Lead:* {'AED '+str(corr['m_cpl']) if corr['m_cpl'] else '—'}"
        )

    mkv_text = fmt(meta_mkv, "MKV Luxury",   "mkv_luxury")
    rto_text = fmt(meta_rto, "Lease to Own", "lease_to_own")

    camp_text = "_No campaign data_"
    if mkv_camps:
        lines = [
            f"• `{c['name'][:36]}`\n"
            f"  AED {c['spend']:,.0f} | {c['results']} results | CPR AED {c['cpr']:,.0f} | CTR {c['ctr']}%"
            for c in mkv_camps
        ]
        camp_text = "\n".join(lines)

    place_text = "\n".join(placements) if placements else "_No placement data_"

    token_warn = ""
    if token_days is not None and token_days < 7:
        token_warn = f"\n\n⚠️  *Meta token expires in {token_days} day(s) — refresh via .bat file now.*"

    blocks = [
        header(f"🟦 Meta Ads Weekly — {label}"),
        divider(),
        section(f"*🟦 MKV Luxury*\n{mkv_text}{token_warn}"),
        divider(),
        section(f"*🟪 Lease to Own*\n{rto_text}"),
        divider(),
        section(f"*🏆 MKV Luxury — Top Campaigns*\n{camp_text}"),
        divider(),
        section(f"*📺 Placement Breakdown (MKV Luxury)*\n{place_text}"),
        divider(),
        ctx(mode_tag()),
    ]
    post_slack(blocks, f"MKV Meta Ads Weekly — {label}")

# ══════════════════════════════════════════════════════════════════════════════
# POST 3 — CORRELATION SUMMARY + OPTIMIZATION ACTIONS
# ══════════════════════════════════════════════════════════════════════════════

def post_correlation(google, meta_mkv, meta_rto, gal, corr, recs, prev, label):
    prev_c = prev.get("corr",{})

    # Channel correlation table
    gsp = f"AED {corr['g_spend']:,.0f}"
    msp = f"AED {corr['m_spend']:,.0f}"
    tsp = f"AED {corr['total_spend']:,.0f}"
    gcpl = f"AED {corr['g_cpl']:,.0f}" if corr["g_cpl"] else "—"
    mcpl = f"AED {corr['m_cpl']:,.0f}" if corr["m_cpl"] else "—"
    bcpl = f"AED {corr['b_cpl']:,.0f}" if corr["b_cpl"] else "—"

    ch_tbl = (
        "```\n"
        f"{'Channel':<18} {'Spend':>9} {'Leads':>6} {'Cost/Lead':>10}\n"
        f"{'-'*47}\n"
        f"{'🔵 Google Ads':<18} {gsp:>9} {corr['g_leads']:>6} {gcpl:>10}\n"
        f"{'🟦 Meta (All)':<18} {msp:>9} {corr['m_leads']:>6} {mcpl:>10}\n"
        f"{'🌐 Organic':<18} {'—':>9} {corr['org_leads']:>6} {'—':>10}\n"
        f"{'-'*47}\n"
        f"{'TOTAL':<18} {tsp:>9} {corr['total_leads']:>6} {bcpl:>10}\n"
        "```"
    )

    funnel_text = (
        f"*Total Spend:* AED {corr['total_spend']:,.2f}"
        f"{wow(corr['total_spend'], prev_c.get('total_spend',0))}    "
        f"*Total Leads:* {corr['total_leads']}"
        f"{wow(corr['total_leads'], prev_c.get('total_leads',0), is_count=True)}\n"
        f"*Blended CPL:* AED {corr['b_cpl']:,.2f}    "
        f"*Qualified:* {corr['qual_pct']}%    "
        f"*Converted:* {corr['conv_pct']}%    "
        f"*Best Channel:* {corr['winner']}"
    )

    # Gallabox breakdown
    src = gal.get("sources",{}); stg = gal.get("stages",{}); agt = gal.get("agents",{})
    SO  = ["Google Ads","Facebook / Instagram","Instagram DMs","OneClickDrive","Website"]
    src_lines = [f"• {s:<26} {src[s]:>4}" for s in SO if src.get(s,0)]
    src_lines += [f"• {s:<26} {v:>4}" for s,v in src.items() if s not in SO and v]
    stg_lines  = [f"• {s:<22} {stg.get(s,0):>4}" for s in ["Lead created","Qualified lead","Converted lead","Unknown"] if stg.get(s,0)]
    top_agents = sorted(agt.items(), key=lambda x:x[1], reverse=True)[:5]
    agt_lines  = [f"• {a:<22} {n:>4} leads" for a,n in top_agents]

    gal_text = (
        f"*Total WA Leads:* {gal['total']}{wow(gal['total'],prev_c.get('total_leads',0),is_count=True)}\n\n"
        f"*By Source:*\n" + "\n".join(src_lines or ["_none_"]) +
        f"\n\n*By Stage:*\n" + "\n".join(stg_lines or ["_none_"]) +
        f"\n\n*Top Agents:*\n" + "\n".join(agt_lines or ["_none_"])
    )

    # Recommendations
    if recs:
        rec_lines = [f"{r[0]} *{r[2]}*\n   _{r[3]}_" for r in recs]
        rec_text  = "\n\n".join(rec_lines)
    else:
        rec_text = "✅ No critical issues this week. All channels performing within benchmark."

    blocks = [
        header(f"📊 Weekly Correlation & Optimization — {label}"),
        divider(),
        section(f"*🔭 Funnel Summary*\n{funnel_text}"),
        divider(),
        section(f"*📡 Spend → WhatsApp Leads (by Channel)*\n{ch_tbl}"),
        divider(),
        section(f"*📲 Gallabox — Lead Breakdown (7 Days)*\n{gal_text}"),
        divider(),
        section(f"*🎯 This Week's Priority Actions*\n{rec_text}"),
        divider(),
        ctx(mode_tag()),
    ]
    post_slack(blocks, f"MKV Weekly Correlation & Actions — {label}")

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def run():
    label = week_label()
    print("=" * 64)
    print("  MKV WEEKLY OPTIMIZER v2.0")
    print(f"  {datetime.now(dubai_tz).strftime('%Y-%m-%d %H:%M')} Dubai")
    print(f"  Week: {label}")
    print(f"  Mode: {'🧪 TEST' if TEST_MODE else '🚀 LIVE'}  |  Channel: {SLACK_CHANNEL}")
    print("=" * 64)

    prev = load_store()

    # ── Google Ads ────────────────────────────────────────────────────────────
    print("\n📊 Google Ads (7-day)...")
    gc     = get_google_client()
    google = fetch_google_summary(gc)
    camps  = fetch_google_campaigns(gc)
    terms  = fetch_google_search_terms(gc)

    # ── Meta ──────────────────────────────────────────────────────────────────
    print("\n📥 Meta Ads (7-day via META_TOKEN)...")
    token      = META_TOKEN.strip() if META_TOKEN else None
    meta_mkv   = {}; meta_rto = {}; mkv_camps = []; placements = []
    token_days = None
    if token:
        token_days = check_token_expiry(token)
        print(f"  Token expires in: {token_days} day(s)" if token_days is not None else "  Token expiry: unknown")
        meta_mkv   = fetch_meta_account(MKV_ACCOUNT, token, "MKV Luxury")
        meta_rto   = fetch_meta_account(RTO_ACCOUNT, token, "Lease to Own")
        mkv_camps  = fetch_meta_campaigns(MKV_ACCOUNT, token)
        placements = fetch_meta_placement(MKV_ACCOUNT, token)
    else:
        print("  ⚠️  META_TOKEN not set — Meta data skipped")

    # ── Gallabox ──────────────────────────────────────────────────────────────
    print("\n📲 Gallabox (7-day)...")
    gal = fetch_gallabox_weekly()

    # ── Correlate ─────────────────────────────────────────────────────────────
    print("\n🔗 Correlating channels → leads...")
    corr = correlate(google, meta_mkv, meta_rto, gal)
    print(f"  Google CPL: AED {corr['g_cpl']} | Meta CPL: AED {corr['m_cpl']} | Winner: {corr['winner']}")

    # ── Recommendations ───────────────────────────────────────────────────────
    recs = recommendations(google, meta_mkv, meta_rto, gal, corr)

    # ── Save for next week's WoW ──────────────────────────────────────────────
    save_store({
        "week"  : week_range()[0],
        "google": google,
        "meta"  : {"mkv_luxury": meta_mkv, "lease_to_own": meta_rto},
        "corr"  : corr,
    })

    # ── 3 Separate Slack Posts ────────────────────────────────────────────────
    print("\n📤 Post 1 — Google Ads Weekly...")
    post_google(google, camps, terms, gal, corr, prev, label)

    print("📤 Post 2 — Meta Ads Weekly...")
    post_meta(meta_mkv, meta_rto, mkv_camps, placements, gal, corr, prev, label, token_days)

    print("📤 Post 3 — Correlation & Actions...")
    post_correlation(google, meta_mkv, meta_rto, gal, corr, recs, prev, label)

    print(f"\n✅  Done — {label}\n")

# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run()
