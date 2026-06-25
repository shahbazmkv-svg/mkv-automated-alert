"""
MKV Daily Ads Snapshot — Google Ads API + Meta Ads API → Slack
=============================================================
Version : 4.0  (25 Jun 2026)
- Google Ads via API (unchanged)
- Meta Ads via API (replaces Gmail CSV parsing)
  - Both accounts: MKV Luxury + Lease to Own
  - Placement breakdown + MTD summary
  - Uses permanent System User token (never expires)
- Posts unified daily snapshot to Slack via Block Kit

GitHub Secrets required:
  SLACK_BOT_TOKEN
  GOOGLE_ADS_DEVELOPER_TOKEN
  GOOGLE_ADS_CLIENT_ID
  GOOGLE_ADS_CLIENT_SECRET
  GOOGLE_ADS_REFRESH_TOKEN
  GOOGLE_ADS_CUSTOMER_ID        (3847584613 — no dashes)
  GOOGLE_ADS_LOGIN_CUSTOMER_ID  (3112515531)
  META_TOKEN                    (System User permanent token)

Run: python mkv_ads_api_report.py
     TEST_MODE=true python mkv_ads_api_report.py
"""

import os
import json
import time
import urllib.request
from datetime import datetime, timedelta
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ── CONFIG ────────────────────────────────────────────────────────────────────

SLACK_TOKEN         = os.environ.get("SLACK_BOT_TOKEN", "")
DEVELOPER_TOKEN     = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN", "")
CLIENT_ID           = os.environ.get("GOOGLE_ADS_CLIENT_ID", "")
CLIENT_SECRET       = os.environ.get("GOOGLE_ADS_CLIENT_SECRET", "")
REFRESH_TOKEN       = os.environ.get("GOOGLE_ADS_REFRESH_TOKEN", "")
CUSTOMER_ID         = os.environ.get("GOOGLE_ADS_CUSTOMER_ID", "")
LOGIN_CUSTOMER_ID   = os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "")

GMAIL_ADDRESS       = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD  = os.environ.get("GMAIL_APP_PASSWORD", "")

META_TOKEN          = os.environ.get("META_TOKEN", "")
MKV_ACCOUNT         = "699611181993619"
RTO_ACCOUNT         = "900731551390821"

TEST_MODE           = os.environ.get("TEST_MODE", "false").lower() == "true"
SLACK_CHANNEL       = "C0B0TGBDCDU" if TEST_MODE else "C0AASQKLY59"

DELTA_FILE          = "mkv_yesterday_metrics.json"
REPORT_DATE         = (datetime.now() - timedelta(days=1)).strftime("%d %b %Y")
DATE_RANGE          = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

# ── GOOGLE ADS CLIENT ─────────────────────────────────────────────────────────

def get_google_ads_client():
    """Initialize Google Ads API client."""
    config = {
        "developer_token"  : DEVELOPER_TOKEN,
        "client_id"        : CLIENT_ID,
        "client_secret"    : CLIENT_SECRET,
        "refresh_token"    : REFRESH_TOKEN,
        "login_customer_id": LOGIN_CUSTOMER_ID,
        "use_proto_plus"   : True,
    }
    return GoogleAdsClient.load_from_dict(config)

# ── GOOGLE ADS API QUERIES ────────────────────────────────────────────────────

def fetch_campaign_performance(client):
    """Fetch campaign performance metrics for yesterday."""
    print("  → Fetching campaign performance...")
    ga_service = client.get_service("GoogleAdsService")

    query = f"""
        SELECT
            campaign.name,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.ctr
        FROM campaign
        WHERE segments.date = '{DATE_RANGE}'
          AND campaign.status = 'ENABLED'
        ORDER BY metrics.cost_micros DESC
        LIMIT 10
    """

    try:
        response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
        campaigns = []
        total = {"impressions": 0, "clicks": 0, "cost": 0, "conversions": 0}

        for row in response:
            cost = row.metrics.cost_micros / 1_000_000
            impr = row.metrics.impressions
            clks = row.metrics.clicks
            conv = row.metrics.conversions
            ctr  = round(row.metrics.ctr * 100, 2)

            total["impressions"]  += impr
            total["clicks"]       += clks
            total["cost"]         += cost
            total["conversions"]  += conv

            campaigns.append({
                "name"    : row.campaign.name[:45],
                "spend"   : round(cost, 2),
                "clicks"  : int(clks),
                "ctr"     : ctr,
                "conv"    : round(conv, 1),
            })

        total_impr = total["impressions"]
        total_clks = total["clicks"]
        total_cost = round(total["cost"], 2)
        total_conv = round(total["conversions"], 1)

        print(f"    ✅ Campaign: AED {total_cost} | {total_clks} clicks | {total_conv} conv")
        return {
            "impressions"   : int(total_impr),
            "clicks"        : int(total_clks),
            "cost"          : total_cost,
            "conversions"   : total_conv,
            "ctr"           : round(total_clks / total_impr * 100, 2) if total_impr > 0 else 0,
            "cost_per_conv" : round(total_cost / total_conv, 2) if total_conv > 0 else 0,
            "campaigns"     : campaigns[:3],
        }
    except GoogleAdsException as ex:
        print(f"    ❌ Campaign fetch failed: {ex.error.code().name}")
        return {}


def fetch_conversions(client):
    """Fetch conversion breakdown."""
    print("  → Fetching conversions...")
    ga_service = client.get_service("GoogleAdsService")

    query = f"""
        SELECT
            conversion_action.name,
            metrics.conversions,
            metrics.cost_per_conversion
        FROM conversion_action
        WHERE segments.date = '{DATE_RANGE}'
          AND metrics.conversions > 0
        ORDER BY metrics.conversions DESC
        LIMIT 5
    """

    try:
        response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
        rows = []
        for row in response:
            cpc = round(row.metrics.cost_per_conversion / 1_000_000, 2)
            rows.append(
                f"• {row.conversion_action.name[:35]}  "
                f"— {round(row.metrics.conversions, 1)} conv"
                f"{f' | AED {cpc}/conv' if cpc > 0 else ''}"
            )
        print(f"    ✅ Conversions: {len(rows)} types")
        return {"breakdown": rows or ["• No conversions recorded today"]}
    except GoogleAdsException as ex:
        print(f"    ❌ Conversions fetch failed: {ex.error.code().name}")
        return {"breakdown": ["• Could not fetch conversion data"]}


def fetch_search_terms(client):
    """Fetch top search terms by clicks."""
    print("  → Fetching search terms...")
    ga_service = client.get_service("GoogleAdsService")

    query = f"""
        SELECT
            search_term_view.search_term,
            metrics.clicks,
            metrics.impressions
        FROM search_term_view
        WHERE segments.date = '{DATE_RANGE}'
          AND metrics.clicks > 0
        ORDER BY metrics.clicks DESC
        LIMIT 5
    """

    try:
        response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
        terms = []
        for row in response:
            terms.append(
                f"• {row.search_term_view.search_term[:50]}"
                f"  ({int(row.metrics.clicks)} clicks)"
            )
        print(f"    ✅ Search terms: {len(terms)} found")
        return {"terms": terms or ["• No search term data today"]}
    except GoogleAdsException as ex:
        print(f"    ❌ Search terms fetch failed: {ex.error.code().name}")
        return {"terms": ["• Could not fetch search term data"]}


def fetch_auction_insights(client):
    """Fetch auction insights / competitor data."""
    print("  → Fetching auction insights...")
    ga_service = client.get_service("GoogleAdsService")

    query = f"""
        SELECT
            auction_insight.domain,
            metrics.auction_insight_search_impression_share,
            metrics.auction_insight_search_overlap_rate
        FROM auction_insight
        WHERE segments.date = '{DATE_RANGE}'
        ORDER BY metrics.auction_insight_search_impression_share DESC
        LIMIT 5
    """

    try:
        response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
        competitors = []
        for row in response:
            is_pct   = round(row.metrics.auction_insight_search_impression_share * 100, 1)
            ovlp_pct = round(row.metrics.auction_insight_search_overlap_rate * 100, 1)
            competitors.append(
                f"• {row.auction_insight.domain[:35]}"
                f"  — IS: {is_pct}% | Overlap: {ovlp_pct}%"
            )
        print(f"    ✅ Auction insights: {len(competitors)} competitors")
        return {"competitors": competitors or ["• No competitor data today"]}
    except GoogleAdsException as ex:
        print(f"    ❌ Auction insights fetch failed: {ex.error.code().name}")
        return {"competitors": ["• Could not fetch competitor data"]}


def fetch_landing_pages(client):
    """Fetch top landing pages by clicks."""
    print("  → Fetching landing pages...")
    ga_service = client.get_service("GoogleAdsService")

    query = f"""
        SELECT
            landing_page_view.unexpanded_final_url,
            metrics.clicks,
            metrics.conversions,
            metrics.ctr
        FROM landing_page_view
        WHERE segments.date = '{DATE_RANGE}'
          AND metrics.clicks > 0
        ORDER BY metrics.clicks DESC
        LIMIT 5
    """

    try:
        response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
        pages = []
        for row in response:
            url  = row.landing_page_view.unexpanded_final_url.replace("https://www.mkvluxury.com", "")
            ctr  = round(row.metrics.ctr * 100, 2)
            conv = round(row.metrics.conversions, 1)
            pages.append(
                f"• {url[:50]}  —  "
                f"{int(row.metrics.clicks)} clicks"
                f" | CTR {ctr}%"
                f"{f' | {conv} conv' if conv > 0 else ''}"
            )
        print(f"    ✅ Landing pages: {len(pages)} found")
        return {"pages": pages or ["• No landing page data today"]}
    except GoogleAdsException as ex:
        print(f"    ❌ Landing pages fetch failed: {ex.error.code().name}")
        return {"pages": ["• Could not fetch landing page data"]}

# ── META ADS — API ───────────────────────────────────────────────────────────

def meta_api(url):
    """Call Meta Graph API."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read().decode())


def fetch_meta_insights(account_id, token, name):
    """Fetch yesterday's Meta Ads insights for one account."""
    try:
        d = meta_api(
            f"https://graph.facebook.com/v20.0/act_{account_id}/insights"
            f"?fields=spend,impressions,clicks,reach,actions"
            f"&date_preset=yesterday&level=account&access_token={token}"
        )
        if not d.get("data"):
            print(f"    ⚠️  {name}: No data yesterday (campaigns paused?)")
            return {}
        row   = d["data"][0]
        spent = round(float(row.get("spend", 0)), 2)
        impr  = int(row.get("impressions", 0))
        clks  = int(row.get("clicks", 0))
        reach = int(row.get("reach", 0))
        results = max(
            (int(a.get("value", 0)) for a in row.get("actions", [])
             if a["action_type"] == "onsite_conversion.total_messaging_connection"),
            default=0
        )
        ctr = round(clks / impr * 100, 2) if impr > 0 else 0
        cpr = round(spent / results, 2) if results > 0 else 0
        print(f"    ✅ {name}: AED {spent} | {results} results | CTR {ctr}%")
        return {"spent": spent, "impressions": impr, "reach": reach,
                "clicks": clks, "results": results, "ctr": ctr,
                "cost_per_result": cpr}
    except Exception as e:
        print(f"    ❌ {name}: {e}")
        return {}


def fetch_meta_mtd(account_id, token):
    """Fetch month-to-date Meta Ads data."""
    try:
        mtd_start = datetime.now().replace(day=1).strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        d = meta_api(
            f"https://graph.facebook.com/v20.0/act_{account_id}/insights"
            f"?fields=spend,actions"
            f"&time_range=%7B%22since%22%3A%22{mtd_start}%22%2C%22until%22%3A%22{yesterday}%22%7D"
            f"&level=account&access_token={token}"
        )
        if not d.get("data"):
            return {}
        row   = d["data"][0]
        spent = round(float(row.get("spend", 0)), 2)
        results = max(
            (int(a.get("value", 0)) for a in row.get("actions", [])
             if a["action_type"] == "onsite_conversion.total_messaging_connection"),
            default=0
        )
        return {"spent": spent, "results": results}
    except:
        return {}


def fetch_meta_placement(account_id, token):
    """Fetch placement breakdown for yesterday."""
    try:
        d = meta_api(
            f"https://graph.facebook.com/v20.0/act_{account_id}/insights"
            f"?fields=spend,impressions,clicks,reach"
            f"&date_preset=yesterday&breakdowns=publisher_platform"
            f"&level=account&access_token={token}"
        )
        rows = []
        for row in sorted(d.get("data", []), key=lambda x: float(x.get("spend", 0)), reverse=True)[:4]:
            platform = row.get("publisher_platform", "").replace("_", " ").title()
            spent = round(float(row.get("spend", 0)), 2)
            clks  = int(row.get("clicks", 0))
            impr  = int(row.get("impressions", 0))
            reach = int(row.get("reach", 0))
            if spent > 0:
                ctr = round(clks / impr * 100, 2) if impr > 0 else 0
                rows.append(f"• {platform}  —  AED {spent:,.2f} | {clks} clicks | Reach {reach:,} | CTR {ctr}%")
        return rows
    except:
        return []
        return {}

# ── DAY-OVER-DAY DELTA ────────────────────────────────────────────────────────

def load_yesterday():
    try:
        if os.path.exists(DELTA_FILE):
            with open(DELTA_FILE) as f:
                return json.load(f)
    except:
        pass
    return {}


def save_today(g_camp, meta):
    try:
        with open(DELTA_FILE, "w") as f:
            json.dump({
                "date"     : REPORT_DATE,
                "g_cost"   : g_camp.get("cost", 0),
                "g_clicks" : g_camp.get("clicks", 0),
                "g_conv"   : g_camp.get("conversions", 0),
                "m_spent"  : meta.get("mkv", {}).get("spent", 0) + meta.get("rto", {}).get("spent", 0),
                "m_results": meta.get("mkv", {}).get("results", 0) + meta.get("rto", {}).get("results", 0),
            }, f)
    except Exception as e:
        print(f"  ⚠️   Could not save delta: {e}")


def delta(today, yesterday, prefix="AED "):
    if yesterday == 0:
        return ""
    diff = today - yesterday
    arrow = "↑" if diff > 0 else "↓" if diff < 0 else "→"
    return f"  ({'+' if diff >= 0 else ''}{prefix}{abs(diff):,.0f} {arrow})"

# ── PERFORMANCE SCORE ─────────────────────────────────────────────────────────

def score_report(g_camp, meta):
    score = 0
    notes = []
    ctr  = g_camp.get("ctr", 0)
    conv = g_camp.get("conversions", 0)
    cost = g_camp.get("cost", 0)
    cpc  = g_camp.get("cost_per_conv", 0)

    if ctr >= 3:
        score += 25; notes.append("✅ CTR above benchmark (3%+)")
    elif ctr >= 1.5:
        score += 15; notes.append("🟡 CTR average — test new headlines")
    elif ctr > 0:
        score += 5;  notes.append("🔴 Low CTR — ad copy review needed")

    if conv >= 5:
        score += 30; notes.append("✅ Strong conversions")
    elif conv >= 1:
        score += 15; notes.append("🟡 Some conversions — optimise landing pages")
    else:
        notes.append("🔴 Zero conversions — check tracking & landing pages")

    if 0 < cpc < 100:
        score += 20; notes.append("✅ Cost/conv efficient")
    elif 0 < cpc < 200:
        score += 10; notes.append("🟡 Cost/conv moderate")
    elif cpc >= 200:
        notes.append("🔴 High cost per conversion")

    if cost > 0:
        score += 10

    if meta.get("mkv", {}).get("spent", 0) > 0 or meta.get("rto", {}).get("spent", 0) > 0:
        score += 10; notes.append("✅ Meta Ads active")
        mkv_ctr = meta.get("mkv", {}).get("ctr", 0)
        rto_ctr = meta.get("rto", {}).get("ctr", 0)
        if max(mkv_ctr, rto_ctr) >= 1.5:
            score += 5; notes.append("✅ Meta CTR strong")

    grade = (
        "🟢 Excellent"         if score >= 80 else
        "🟡 Good"              if score >= 60 else
        "🟠 Needs Improvement"  if score >= 40 else
        "🔴 Needs Attention"
    )
    return min(score, 100), grade, notes

# ── SLACK BLOCK KIT ───────────────────────────────────────────────────────────

def build_blocks(g_camp, g_conv, g_search, g_auction, g_landing, meta, yesterday):
    score, grade, notes = score_report(g_camp, meta)

    d_cost  = delta(g_camp.get("cost", 0),   yesterday.get("g_cost", 0))
    d_clk   = delta(g_camp.get("clicks", 0), yesterday.get("g_clicks", 0), prefix="")
    d_conv  = delta(g_camp.get("conversions", 0), yesterday.get("g_conv", 0), prefix="")
    d_mcost = delta(
        meta.get("mkv", {}).get("spent", 0) + meta.get("rto", {}).get("spent", 0),
        yesterday.get("m_spent", 0))

    # Google Ads section
    if g_camp:
        g_text = (
            f"*Spend:* AED {g_camp.get('cost',0):,.2f}{d_cost}    "
            f"*Clicks:* {g_camp.get('clicks',0):,}{d_clk}\n"
            f"*Impressions:* {g_camp.get('impressions',0):,}    "
            f"*CTR:* {g_camp.get('ctr',0)}%\n"
            f"*Conversions:* {g_camp.get('conversions',0)}{d_conv}    "
            f"*Cost/Conv:* AED {g_camp.get('cost_per_conv',0):,.2f}"
        )
        if g_camp.get("campaigns"):
            lines = ["\n*Top Campaigns:*"]
            for c in g_camp["campaigns"]:
                lines.append(f"  • {c['name']}  —  AED {c['spend']:,.0f} | {c['clicks']:,} clicks | {c['ctr']}% CTR")
            g_text += "\n" + "\n".join(lines)
    else:
        g_text = "_No Google Ads data today_"

    conv_text   = "\n".join(g_conv.get("breakdown", [])) or "_No conversion data_"
    search_text = "\n".join(g_search.get("terms", []))    or "_No search term data_"
    comp_text   = "\n".join(g_auction.get("competitors",[]))or "_No competitor data_"
    land_text   = "\n".join(g_landing.get("pages", []))   or "_No landing page data_"

    # Meta section — both accounts
    def fmt_meta_acct(data, mtd_data, acct_name):
        if not data.get("spent", 0):
            return f"_No {acct_name} Meta data today_"
        text = (
            f"*Spent:* AED {data['spent']:,.2f}    "
            f"*Results:* {data['results']}    "
            f"*Cost/Result:* AED {data['cost_per_result']:,.2f}\n"
            f"*Impressions:* {data['impressions']:,}    "
            f"*Reach:* {data['reach']:,}    "
            f"*CTR:* {data['ctr']}%"
        )
        if mtd_data:
            text += f"\n*MTD:* AED {mtd_data.get('spent',0):,.0f} | {mtd_data.get('results',0)} results"
        return text

    mkv_meta_text = fmt_meta_acct(
        meta.get("mkv", {}), meta.get("mkv_mtd", {}), "MKV Luxury")
    rto_meta_text = fmt_meta_acct(
        meta.get("rto", {}), meta.get("rto_mtd", {}), "Lease to Own")

    place_text = "\n".join(meta.get("placements", [])) if meta.get("placements") else "• Placement data unavailable"

    total_mtd = meta.get("mkv_mtd", {}).get("spent", 0) + meta.get("rto_mtd", {}).get("spent", 0)
    if total_mtd > 0:
        mtd_text = (
            f"*Total Meta MTD:* AED {total_mtd:,.0f}\n"
            f"• MKV Luxury: AED {meta.get('mkv_mtd',{}).get('spent',0):,.0f} | "
            f"{meta.get('mkv_mtd',{}).get('results',0)} results\n"
            f"• Lease to Own: AED {meta.get('rto_mtd',{}).get('spent',0):,.0f} | "
            f"{meta.get('rto_mtd',{}).get('results',0)} results"
        )
    else:
        mtd_text = "_No MTD data_"

    meta_text = f"*🟦 MKV Luxury*\n{mkv_meta_text}"
    rto_text  = f"*🟪 Lease to Own*\n{rto_meta_text}"

    score_text = f"*Score: {score}/100 — {grade}*\n" + "\n".join(notes)
    mode_tag   = "🧪 TEST MODE" if TEST_MODE else "🚀 LIVE"

    return [
        {"type": "header",
         "text": {"type": "plain_text", "text": f"📊 MKV Daily Ads Snapshot — {REPORT_DATE}", "emoji": True}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*🔵 Google Ads Performance*\n{g_text}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*🎯 Conversions Breakdown*\n{conv_text}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*🔍 Top Search Terms*\n{search_text}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*⚔️ Competitor Auction Insights*\n{comp_text}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*📄 Top Landing Pages*\n{land_text}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": meta_text}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": rto_text}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*📺 Placement Breakdown*\n{place_text}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*📅 MTD Summary*\n{mtd_text}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*🏆 Performance Score*\n{score_text}"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
                       "text": f"_MKV Luxury Car Rental  •  Google Ads + Meta API v4.0  •  {mode_tag}_"}]},
    ]


def post_slack(blocks, fallback="MKV Daily Ads Snapshot"):
    client = WebClient(token=SLACK_TOKEN)
    try:
        client.chat_postMessage(channel=SLACK_CHANNEL, blocks=blocks, text=fallback)
        print(f"  ✅  Posted to Slack: {SLACK_CHANNEL}")
    except SlackApiError as e:
        print(f"  ❌  Slack error: {e.response['error']}")

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  MKV Daily Ads Snapshot v4.0 — Google Ads + Meta API")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Report date: {REPORT_DATE}")
    print(f"  Mode: {'🧪 TEST' if TEST_MODE else '🚀 LIVE'}  |  Channel: {SLACK_CHANNEL}")
    print("=" * 60)

    # ── Google Ads API
    print("\n📊 Fetching Google Ads data via API...")
    try:
        client  = get_google_ads_client()
        g_camp    = fetch_campaign_performance(client)
        g_conv    = fetch_conversions(client)
        g_search  = fetch_search_terms(client)
        g_auction = fetch_auction_insights(client)
        g_landing = fetch_landing_pages(client)
    except Exception as e:
        print(f"  ❌  Google Ads API error: {e}")
        g_camp = g_conv = g_search = g_auction = g_landing = {}

    # ── Meta Ads via API
    print("\n📥 Fetching Meta Ads via API...")
    meta = {}
    if META_TOKEN:
        mkv_data = fetch_meta_insights(MKV_ACCOUNT, META_TOKEN, "MKV Luxury")
        rto_data = fetch_meta_insights(RTO_ACCOUNT, META_TOKEN, "Lease to Own")
        mkv_mtd  = fetch_meta_mtd(MKV_ACCOUNT, META_TOKEN)
        rto_mtd  = fetch_meta_mtd(RTO_ACCOUNT, META_TOKEN)
        placements = fetch_meta_placement(MKV_ACCOUNT, META_TOKEN)
        meta = {
            "mkv": mkv_data, "rto": rto_data,
            "mkv_mtd": mkv_mtd, "rto_mtd": rto_mtd,
            "placements": placements,
        }
    else:
        print("  ⚠️  META_TOKEN not set — skipping Meta Ads")

    # ── Delta
    yesterday = load_yesterday()
    save_today(g_camp, meta)

    # ── Summary
    print(f"\n  Google Ads → AED {g_camp.get('cost',0):.2f} | "
          f"{g_camp.get('clicks',0)} clicks | "
          f"{g_camp.get('conversions',0)} conv")
    print(f"  Meta MKV   → AED {meta.get('mkv',{}).get('spent',0):.2f} | "
          f"{meta.get('mkv',{}).get('results',0)} results")
    print(f"  Meta RTO   → AED {meta.get('rto',{}).get('spent',0):.2f} | "
          f"{meta.get('rto',{}).get('results',0)} results")

    # ── Post to Slack
    print("\n📤 Posting to Slack...")
    blocks = build_blocks(g_camp, g_conv, g_search, g_auction, g_landing, meta, yesterday)
    post_slack(blocks)
    print("\n✅  Done!\n")


if __name__ == "__main__":
    main()
