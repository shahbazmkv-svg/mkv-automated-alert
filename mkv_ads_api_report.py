"""
MKV Daily Ads Snapshot — Google Ads API + Meta Ads API → Slack
=============================================================
Version : 6.1  (26 Nov 2026)
v6.1: plain status per campaign and overall — ✅ Performing / 🟡 Average / 🔴 Needs attention
v6.0:
  - Google report rewritten in plain English: one-line summary with real Gallabox
    WhatsApp chats and cost per real chat, per-campaign CPC alerts (Rental > AED 45,
    RTO > AED 15), MTD tracking capture rate and budget pace, search terms classified
    (relevant / already blocked / decide / new junk — core terms never suggested as
    negatives), "what to do today", learning-period banner, explained health score
  - Meta: one-line summary per account + which ad caused each warning
  - TEST_MODE read from env (TEST_MODE=true → #mkvtest-new)
v5.7  (25 Jun 2026)
Fixes:
  - URL previews suppressed in Slack (unfurl disabled)
  - Conversions query fixed (use campaign resource)
  - Auction insights fixed (use campaign resource)
  - Rent-to-Own separate section added
  - Meta via API (permanent System User token, never expires)
  - Meta report only posts when data exists
  - Gmail fallback removed (no longer needed)
"""

import os
import io
import re
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

TEST_MODE           = os.environ.get("TEST_MODE", "false").lower() == "true"
SLACK_CHANNEL       = "C0B0TGBDCDU" if TEST_MODE else "C0AASQKLY59"

DELTA_FILE          = "mkv_yesterday_metrics.json"
REPORT_DATE         = (datetime.now() - timedelta(days=1)).strftime("%d %b %Y")
DATE_RANGE          = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

# Meta Ads API
META_ACCESS_TOKEN   = os.environ.get("META_ACCESS_TOKEN", os.environ.get("META_TOKEN", ""))
print(f"  Meta token configured: {'YES' if META_ACCESS_TOKEN else 'NO'}")
META_AD_ACCOUNT_ID  = os.environ.get("META_AD_ACCOUNT_ID", "699611181993619")
META_RTO_ACCOUNT_ID = os.environ.get("META_RTO_ACCOUNT_ID", "900731551390821")

# RentToOwn domain to split out separately
RTO_DOMAIN = "renttoowncars.ae"

# ── GOOGLE ADS CLIENT ─────────────────────────────────────────────────────────

def get_google_ads_client():
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


# Geo target constant IDs → readable names
GEO_NAMES = {
    "2784": "United Arab Emirates", "9006157": "United Arab Emirates",
    "1011245": "Dubai", "20636": "Dubai", "9047765": "Dubai",
    "1011246": "Abu Dhabi", "20637": "Abu Dhabi",
    "1011247": "Sharjah", "20638": "Sharjah",
    "1011248": "Ajman", "1011249": "Ras Al Khaimah",
    "1011250": "Fujairah", "1011251": "Umm Al Quwain",
    "1007741": "Saudi Arabia", "2682": "United Kingdom",
    "2840": "United States", "2356": "India",
    "2276": "Germany", "2250": "France",
    "2036": "Australia", "2124": "Canada",
    "2410": "South Korea", "2702": "Singapore",
    "2408": "Japan", "2156": "China",
}


def get_geo_name(resource_name, geo_svc):
    """Resolve geoTargetConstants/XXXX to a human name."""
    geo_id = resource_name.split("/")[-1].split("~")[0].strip()
    if geo_id in GEO_NAMES:
        return GEO_NAMES[geo_id]
    try:
        gtc  = geo_svc.get_geo_target_constant(resource_name=f"geoTargetConstants/{geo_id}")
        name = gtc.canonical_name or gtc.name or ""
        if "," in name:
            return name.split(",")[0].strip()
        return name if name else f"Region {geo_id}"
    except:
        return f"Region {geo_id}"


def fetch_geographic(client):
    """Fetch top countries and cities by performance."""
    print("  → Fetching geographic performance...")
    ga_service = client.get_service("GoogleAdsService")
    geo_svc    = client.get_service("GeoTargetConstantService")

    def resolve(resource_name):
        geo_id = resource_name.split("/")[-1].split("~")[0]
        if geo_id in GEO_NAMES:
            return GEO_NAMES[geo_id]
        try:
            gtc  = geo_svc.get_geo_target_constant(resource_name=f"geoTargetConstants/{geo_id}")
            name = gtc.canonical_name or gtc.name or ""
            return name.split(",")[0].strip() if "," in name else name
        except:
            return None

    query = f"""
        SELECT
            geographic_view.resource_name,
            geographic_view.location_type,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.impressions
        FROM geographic_view
        WHERE segments.date = '{DATE_RANGE}'
          AND metrics.clicks > 0
        ORDER BY metrics.clicks DESC
        LIMIT 50
    """
    try:
        response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
        country_agg = {}
        city_agg    = {}

        for row in response:
            resource = row.geographic_view.resource_name
            loc_type = str(row.geographic_view.location_type)
            clks = int(row.metrics.clicks)
            cost = round(row.metrics.cost_micros / 1_000_000, 2)
            conv = round(row.metrics.conversions, 1)
            impr = int(row.metrics.impressions)

            name = resolve(resource)
            if not name:
                continue

            is_country = (
                name in ["United Arab Emirates", "Saudi Arabia", "India",
                         "United Kingdom", "United States", "Germany",
                         "France", "Australia", "China", "Japan", "Canada"] or
                "1" not in loc_type
            )

            target = country_agg if is_country else city_agg
            if name not in target:
                target[name] = {"clicks":0,"cost":0,"conv":0,"impr":0}
            target[name]["clicks"] += clks
            target[name]["cost"]   += cost
            target[name]["conv"]   += conv
            target[name]["impr"]   += impr

        def fmt(agg, limit=5):
            rows = []
            for name, v in sorted(agg.items(), key=lambda x: x[1]["clicks"], reverse=True)[:limit]:
                ctr = round(v["clicks"]/v["impr"]*100,1) if v["impr"] > 0 else 0
                conv_str = f" | {v['conv']} conv" if v["conv"] > 0 else ""
                rows.append(f"• {name}  —  {v['clicks']} clicks | AED {v['cost']:,.0f} | CTR {ctr}%{conv_str}")
            return rows

        countries = fmt(country_agg)
        cities    = fmt(city_agg)

        if not cities:
            query2 = f"""
                SELECT
                    user_location_view.country_criterion_id,
                    metrics.clicks,
                    metrics.cost_micros,
                    metrics.conversions,
                    metrics.impressions
                FROM user_location_view
                WHERE segments.date = '{DATE_RANGE}'
                  AND metrics.clicks > 0
                ORDER BY metrics.clicks DESC
                LIMIT 20
            """
            try:
                resp2 = ga_service.search(customer_id=CUSTOMER_ID, query=query2)
                city_agg2 = {}
                for row in resp2:
                    geo_id = str(row.user_location_view.country_criterion_id)
                    name   = GEO_NAMES.get(geo_id) or resolve(f"geoTargetConstants/{geo_id}")
                    if not name:
                        continue
                    clks = int(row.metrics.clicks)
                    cost = round(row.metrics.cost_micros / 1_000_000, 2)
                    conv = round(row.metrics.conversions, 1)
                    impr = int(row.metrics.impressions)
                    if name not in city_agg2:
                        city_agg2[name] = {"clicks":0,"cost":0,"conv":0,"impr":0}
                    city_agg2[name]["clicks"] += clks
                    city_agg2[name]["cost"]   += cost
                    city_agg2[name]["conv"]   += conv
                    city_agg2[name]["impr"]   += impr
                cities = fmt(city_agg2)
            except:
                pass

        print(f"    ✅ Geo: {len(countries)} countries | {len(cities)} cities")
        return {
            "countries": countries or ["• No country data"],
            "cities"   : cities    or ["• No city data — UAE city breakdown requires location targeting setup"]
        }
    except GoogleAdsException as ex:
        print(f"    ❌ Geographic failed: {ex.error.code().name}")
        return {"countries": ["• Geographic data unavailable"], "cities": []}


def fetch_mtd_google(client):
    """Fetch Month-to-Date Google Ads metrics."""
    print("  → Fetching MTD Google Ads...")
    ga_service = client.get_service("GoogleAdsService")
    mtd_start = datetime.now().replace(day=1).strftime("%Y-%m-%d")
    query = f"""
        SELECT
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.ctr
        FROM customer
        WHERE segments.date BETWEEN '{mtd_start}' AND '{DATE_RANGE}'
    """
    try:
        response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
        impr = clks = cost = conv = 0
        for row in response:
            impr += row.metrics.impressions
            clks += row.metrics.clicks
            cost += row.metrics.cost_micros / 1_000_000
            conv += row.metrics.conversions
        print(f"    ✅ MTD Google: AED {round(cost,2)} | {int(clks)} clicks | {round(conv,1)} conv")
        return {
            "impressions" : int(impr),
            "clicks"      : int(clks),
            "cost"        : round(cost, 2),
            "conversions" : round(conv, 1),
            "ctr"         : round(clks / impr * 100, 2) if impr > 0 else 0,
            "cost_per_conv": round(cost / conv, 2) if conv > 0 else 0,
        }
    except GoogleAdsException as ex:
        print(f"    ❌ MTD Google failed: {ex.error.code().name}")
        return {}


def fetch_campaign_performance(client):
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
            total["impressions"] += impr
            total["clicks"]      += clks
            total["cost"]        += cost
            total["conversions"] += conv
            campaigns.append({
                "name"  : row.campaign.name[:45],
                "spend" : round(cost, 2),
                "clicks": int(clks),
                "ctr"   : round(row.metrics.ctr * 100, 2),
                "conv"  : round(conv, 1),
            })
        ti = total["impressions"]
        tc = total["clicks"]
        ts = round(total["cost"], 2)
        tv = round(total["conversions"], 1)
        print(f"    ✅ Campaign: AED {ts} | {tc} clicks | {tv} conv")
        return {
            "impressions"  : int(ti),
            "clicks"       : int(tc),
            "cost"         : ts,
            "conversions"  : tv,
            "ctr"          : round(tc / ti * 100, 2) if ti > 0 else 0,
            "cost_per_conv": round(ts / tv, 2) if tv > 0 else 0,
            "campaigns"    : campaigns[:3],
        }
    except GoogleAdsException as ex:
        print(f"    ❌ Campaign fetch failed: {ex.error.code().name}")
        return {}


def fetch_conversions(client):
    print("  → Fetching conversions...")
    ga_service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT
            campaign.name,
            metrics.conversions,
            metrics.cost_per_conversion
        FROM campaign
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
            conv = round(row.metrics.conversions, 1)
            rows.append(
                f"• {row.campaign.name[:35]}  — {conv} conv"
                f"{f'  |  AED {cpc}/conv' if cpc > 0 else ''}"
            )
        print(f"    ✅ Conversions: {len(rows)} campaigns")
        return {"breakdown": rows or ["• No conversions recorded today"]}
    except GoogleAdsException as ex:
        print(f"    ❌ Conversions fetch failed: {ex.error.code().name}")
        return {"breakdown": ["• Could not fetch conversion data"]}


def fetch_search_terms(client):
    print("  → Fetching search terms...")
    ga_service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT
            search_term_view.search_term,
            campaign.name,
            metrics.clicks,
            metrics.impressions,
            metrics.conversions,
            metrics.cost_micros
        FROM search_term_view
        WHERE segments.date = '{DATE_RANGE}'
          AND metrics.clicks > 0
        ORDER BY metrics.clicks DESC
        LIMIT 10
    """
    try:
        response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
        terms = []
        raw   = []
        for row in response:
            term  = row.search_term_view.search_term
            camp  = row.campaign.name[:30]
            clks  = int(row.metrics.clicks)
            conv  = round(row.metrics.conversions, 1)
            cost  = round(row.metrics.cost_micros / 1_000_000, 2)
            conv_str = f" | {conv} conv" if conv > 0 else ""
            terms.append(f"• `{term[:45]}`  ({clks} clicks{conv_str})\n  _Campaign: {camp}_")
            raw.append({"term": term, "campaign": camp, "clicks": clks, "conv": conv, "cost": cost})
        print(f"    ✅ Search terms: {len(terms)} found")
        return {"terms": terms or ["• No search term data today"], "raw": raw}
    except GoogleAdsException as ex:
        print(f"    ❌ Search terms fetch failed: {ex.error.code().name}")
        return {"terms": ["• Could not fetch search term data"], "raw": []}


def fetch_keyword_recommendations(client, search_terms_data):
    """Analyse search terms to suggest add/remove from specific campaigns."""
    raw = search_terms_data.get("raw", [])
    if not raw or client is None:
        return {"add": [], "remove": []}

    irrelevant_patterns = [
        "rpm", "kia soul", "hyundai staria", "yaris", "corolla",
        "cheap", "budget", "economy", "used car", "second hand",
        "buy car", "purchase car", "car for sale", "one click"
    ]
    high_intent = ["rental", "rent", "hire", "luxury", "supercar",
                   "lamborghini", "ferrari", "rolls", "bentley", "dubai",
                   "patrol", "audi", "limousine", "porsche", "without deposit"]

    add_keywords = []
    remove_keywords = []

    for r in raw:
        term      = r["term"]
        camp      = r["campaign"]
        term_lower = term.lower()

        is_irrelevant = any(p in term_lower for p in irrelevant_patterns)
        is_high_intent = any(kw in term_lower for kw in high_intent)

        if is_irrelevant:
            remove_keywords.append(f"• `{term[:40]}` → add as negative in *{camp}*")
        elif is_high_intent and r["conv"] == 0:
            remove_keywords.append(f"• `{term[:40]}` → 0 conv, review in *{camp}*")
        elif is_high_intent:
            add_keywords.append(f"• `{term[:40]}` → add [exact match] to *{camp}*")

    return {
        "add"   : add_keywords[:5] or ["• No high-intent terms to add today"],
        "remove": remove_keywords[:5] or ["• No irrelevant terms detected today"]
    }


def fetch_auction_insights(client):
    """Fetch impression share & competitive metrics per campaign."""
    print("  → Fetching impression share & competitive metrics...")
    ga_service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT
            campaign.name,
            metrics.search_impression_share,
            metrics.search_rank_lost_impression_share,
            metrics.search_budget_lost_impression_share,
            metrics.search_top_impression_share
        FROM campaign
        WHERE segments.date = '{DATE_RANGE}'
          AND campaign.status = 'ENABLED'
          AND metrics.impressions > 0
        ORDER BY metrics.search_impression_share DESC
        LIMIT 5
    """
    try:
        response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
        rows = []
        for row in response:
            name     = row.campaign.name[:35]
            is_pct   = round(row.metrics.search_impression_share * 100, 1)
            lost_rank = round(row.metrics.search_rank_lost_impression_share * 100, 1)
            lost_bud  = round(row.metrics.search_budget_lost_impression_share * 100, 1)
            top_is    = round(row.metrics.search_top_impression_share * 100, 1)
            rows.append(
                f"• {name}\n"
                f"  IS: {is_pct}% | Top IS: {top_is}% | Lost (rank): {lost_rank}% | Lost (budget): {lost_bud}%"
            )
        print(f"    ✅ Impression share: {len(rows)} campaigns")
        return {"competitors": rows or ["• No impression share data today"]}
    except GoogleAdsException as ex:
        print(f"    ❌ Impression share failed: {ex.error.code().name}")
        return {"competitors": ["• Data unavailable — check Google Ads UI for Auction Insights"]}


def fetch_mtd_google_split(client):
    """Fetch MTD Google Ads split by Rental vs RTO campaigns."""
    print("  → Fetching MTD Google split (Rental vs RTO)...")
    ga_service = client.get_service("GoogleAdsService")
    mtd_start = datetime.now().replace(day=1).strftime("%Y-%m-%d")
    query = f"""
        SELECT
            campaign.name,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.impressions
        FROM campaign
        WHERE segments.date BETWEEN '{mtd_start}' AND '{DATE_RANGE}'
          AND campaign.status = 'ENABLED'
    """
    try:
        response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
        rental = {"clicks": 0, "cost": 0, "conversions": 0, "impressions": 0}
        rto    = {"clicks": 0, "cost": 0, "conversions": 0, "impressions": 0}
        for row in response:
            name = row.campaign.name.lower()
            clks = int(row.metrics.clicks)
            cost = round(row.metrics.cost_micros / 1_000_000, 2)
            conv = round(row.metrics.conversions, 1)
            impr = int(row.metrics.impressions)
            if "rent to own" in name or "rto" in name or "lease" in name:
                rto["clicks"] += clks; rto["cost"] += cost
                rto["conversions"] += conv; rto["impressions"] += impr
            else:
                rental["clicks"] += clks; rental["cost"] += cost
                rental["conversions"] += conv; rental["impressions"] += impr
        for d in [rental, rto]:
            d["cost"]          = round(d["cost"], 2)
            d["conversions"]   = round(d["conversions"], 1)
            d["ctr"]           = round(d["clicks"] / d["impressions"] * 100, 2) if d["impressions"] > 0 else 0
            d["cost_per_conv"] = round(d["cost"] / d["conversions"], 2) if d["conversions"] > 0 else 0
        print(f"    ✅ Google MTD Split — Rental: AED {rental['cost']} | RTO: AED {rto['cost']}")
        return {"rental": rental, "rto": rto}
    except GoogleAdsException as ex:
        print(f"    ❌ Google MTD split failed: {ex.error.code().name}")
        return {}


def fetch_competitor_keywords(client):
    """Identify competitor-branded search terms in your traffic."""
    print("  → Fetching competitor keyword opportunities...")
    ga_service = client.get_service("GoogleAdsService")

    query = f"""
        SELECT
            search_term_view.search_term,
            metrics.clicks,
            metrics.impressions,
            metrics.conversions,
            metrics.cost_micros
        FROM search_term_view
        WHERE segments.date BETWEEN '{(datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")}' AND '{DATE_RANGE}'
          AND metrics.impressions > 0
        ORDER BY metrics.clicks DESC
        LIMIT 100
    """

    competitor_names = [
        "rotana", "trinity", "luxury supercar", "vip motors", "shift",
        "hertz", "sixt", "europcar", "avis", "budget", "diamond",
        "rpm", "cars25", "carnaby", "prestige", "octane"
    ]

    try:
        response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
        competitor_terms = []
        opportunity_terms = []

        for row in response:
            term  = row.search_term_view.search_term.lower()
            clks  = int(row.metrics.clicks)
            impr  = int(row.metrics.impressions)
            conv  = round(row.metrics.conversions, 1)
            cost  = round(row.metrics.cost_micros / 1_000_000, 2)

            for comp in competitor_names:
                if comp in term:
                    competitor_terms.append(
                        f"• `{row.search_term_view.search_term[:45]}`  "
                        f"— {clks} clicks | {conv} conv | AED {cost:.0f}"
                    )
                    break

            high_intent_words = ["rent", "hire", "rental", "luxury", "supercar",
                                 "lamborghini", "ferrari", "rolls", "bentley",
                                 "porsche", "mclaren", "dubai"]
            if (any(w in term for w in high_intent_words) and
                    impr >= 3 and conv == 0 and clks > 0):
                opportunity_terms.append(
                    f"• `{row.search_term_view.search_term[:45]}`  "
                    f"— {impr} impr | {clks} clicks | 0 conv → add to campaigns"
                )

        print(f"    ✅ Competitor terms: {len(competitor_terms)} | Opportunities: {len(opportunity_terms)}")
        return {
            "competitor_terms": competitor_terms[:5] or ["• No competitor searches detected"],
            "opportunity_terms": opportunity_terms[:5] or ["• No missed opportunities detected"],
        }
    except GoogleAdsException as ex:
        print(f"    ❌ Competitor keywords failed: {ex.error.code().name}")
        return {"competitor_terms": [], "opportunity_terms": []}


def fetch_landing_pages(client):
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
        LIMIT 10
    """
    try:
        response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
        mkv_pages = []
        rto_pages = []
        for row in response:
            url  = row.landing_page_view.unexpanded_final_url
            clks = int(row.metrics.clicks)
            conv = round(row.metrics.conversions, 1)
            ctr  = round(row.metrics.ctr * 100, 2)
            display_url = url.replace("https://www.mkvluxury.com", "").replace("https://mkvluxury.com", "") or "/"
            line = f"• `{display_url[:50]}`  —  {clks} clicks | CTR {ctr}%{f' | {conv} conv' if conv > 0 else ''}"
            if RTO_DOMAIN in url:
                rto_pages.append(line)
            else:
                mkv_pages.append(line)
        print(f"    ✅ Landing pages: {len(mkv_pages)} MKV + {len(rto_pages)} RTO")
        return {"pages": mkv_pages[:5], "rto_pages": rto_pages[:5]}
    except GoogleAdsException as ex:
        print(f"    ❌ Landing pages fetch failed: {ex.error.code().name}")
        return {"pages": ["• Could not fetch landing page data"], "rto_pages": []}

# ── META ADS API ──────────────────────────────────────────────────────────────

def fetch_meta_campaigns(account_id, access_token, account_name=""):
    """Fetch Meta campaign-level breakdown."""
    if not access_token:
        return []
    try:
        since = DATE_RANGE
        until = DATE_RANGE
        fields = f"name,status,insights.time_range(since={since},until={until}){{spend,impressions,clicks}}"
        url = (
            f"https://graph.facebook.com/v19.0/act_{account_id}/campaigns"
            f"?fields={fields}"
            f"&access_token={access_token}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        campaigns = []
        for c in data.get("data", []):
            insights = c.get("insights", {}).get("data", [])
            if not insights:
                continue
            ins = insights[0]
            spend = round(float(ins.get("spend", 0)), 2)
            if spend == 0:
                continue
            clks  = int(ins.get("clicks", 0))
            impr  = int(ins.get("impressions", 0))
            campaigns.append({
                "name"  : c.get("name", "")[:40],
                "spend" : spend,
                "clicks": clks,
                "ctr"   : round(clks / impr * 100, 2) if impr > 0 else 0,
            })
        campaigns.sort(key=lambda x: x["spend"], reverse=True)
        return campaigns[:4]
    except Exception as e:
        print(f"  ⚠️  Meta campaign breakdown error: {e}")
        return []


def fetch_meta_placement(account_id):
    """Fetch Meta Ads by platform (Facebook, Instagram etc)."""
    token = META_ACCESS_TOKEN
    if not token:
        return {}
    print(f"  → Fetching Meta placement breakdown...")
    try:
        url = (
            f"https://graph.facebook.com/v20.0/act_{account_id}/insights"
            f"?fields=spend,impressions,clicks,reach"
            f"&date_preset=yesterday"
            f"&breakdowns=publisher_platform"
            f"&level=account"
            f"&access_token={token}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        rows = []
        for row in sorted(data.get("data", []), key=lambda x: float(x.get("spend", 0)), reverse=True)[:6]:
            platform = row.get("publisher_platform", "unknown").replace("_", " ").title()
            spent = round(float(row.get("spend", 0)), 2)
            clks  = int(row.get("clicks", 0))
            impr  = int(row.get("impressions", 0))
            reach = int(row.get("reach", 0))
            if spent > 0:
                ctr = round(clks / impr * 100, 2) if impr > 0 else 0
                rows.append(f"• {platform}  —  AED {spent:,.2f} | {clks} clicks | Reach {reach:,} | CTR {ctr}%")
        print(f"    ✅ Meta placements: {len(rows)}")
        return {"placements": rows or ["• No placement data today"]}
    except Exception as e:
        print(f"    ❌ Meta placement failed: {e}")
        return {"placements": ["• Placement data unavailable"]}


def fetch_meta_age_gender(account_id):
    """Fetch Meta Ads by age and gender."""
    token = META_ACCESS_TOKEN
    if not token:
        return {}
    print(f"  → Fetching Meta age/gender breakdown...")
    try:
        url = (
            f"https://graph.facebook.com/v20.0/act_{account_id}/insights"
            f"?fields=spend,impressions,clicks,actions"
            f"&date_preset=yesterday"
            f"&breakdowns=age%2Cgender"
            f"&level=account"
            f"&access_token={token}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        segments = []
        for row in data.get("data", []):
            age    = row.get("age", "")
            gender = row.get("gender", "").title()
            spent  = round(float(row.get("spend", 0)), 2)
            clks   = int(row.get("clicks", 0))
            impr   = int(row.get("impressions", 0))
            results = 0
            for a in row.get("actions", []):
                if a["action_type"] == "onsite_conversion.total_messaging_connection":
                    results = max(results, int(a.get("value", 0)))
            if spent > 0:
                cpr = round(spent / results, 2) if results > 0 else 0
                segments.append({"label": f"{gender} {age}", "spent": spent,
                                  "clicks": clks, "results": results, "cpr": cpr, "impressions": impr})
        segments.sort(key=lambda x: (x["results"], x["clicks"]), reverse=True)
        rows = []
        for s in segments[:6]:
            ctr = round(s["clicks"] / s["impressions"] * 100, 2) if s["impressions"] > 0 else 0
            res_str = f" | {s['results']} results @ AED {s['cpr']:,.0f}" if s["results"] > 0 else ""
            rows.append(f"• {s['label']:<15}  AED {s['spent']:,.2f} | {s['clicks']} clicks | CTR {ctr}%{res_str}")
        print(f"    ✅ Meta age/gender: {len(rows)} segments")
        return {"segments": rows or ["• No demographic data today"]}
    except Exception as e:
        print(f"    ❌ Meta age/gender failed: {e}")
        return {"segments": ["• Age/gender data unavailable"]}


def fetch_meta_mtd(account_id, account_name=""):
    """Fetch Meta Ads Month-to-Date metrics."""
    token = META_ACCESS_TOKEN
    if not token:
        return {}
    mtd_start = datetime.now().replace(day=1).strftime("%Y-%m-%d")
    try:
        url = (
            f"https://graph.facebook.com/v20.0/act_{account_id}/insights"
            f"?fields=spend,impressions,clicks,reach,actions"
            f"&time_range=%7B%22since%22%3A%22{mtd_start}%22%2C%22until%22%3A%22{DATE_RANGE}%22%7D"
            f"&level=account"
            f"&access_token={token}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        if not data.get("data"):
            return {}
        row = data["data"][0]
        spent = round(float(row.get("spend", 0)), 2)
        impr  = int(row.get("impressions", 0))
        clks  = int(row.get("clicks", 0))
        actions = row.get("actions", [])
        messaging = 0
        for a in actions:
            if a["action_type"] == "onsite_conversion.total_messaging_connection":
                messaging = max(messaging, int(a.get("value", 0)))
        return {"spent": spent, "impressions": impr, "clicks": clks, "results": messaging}
    except Exception as e:
        print(f"  ⚠️  Meta MTD error: {e}")
        return {}


def fetch_meta_api(account_id, account_name="MKV Luxury"):
    """Fetch Meta Ads data via Marketing API."""
    token = META_ACCESS_TOKEN
    if not token:
        print(f"  ⚠️  No Meta access token configured")
        return {}
    print(f"  → Fetching Meta Ads for {account_name}...")
    try:
        url = (
            f"https://graph.facebook.com/v20.0/act_{account_id}/insights"
            f"?fields=spend,impressions,clicks,reach,actions"
            f"&date_preset=yesterday"
            f"&level=account"
            f"&access_token={token}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        if not data.get("data"):
            print(f"  ⚠️  No Meta data for {account_name}")
            return {}

        row = data["data"][0]
        spent = round(float(row.get("spend", 0)), 2)
        impr  = int(row.get("impressions", 0))
        clks  = int(row.get("clicks", 0))
        reach = int(row.get("reach", 0))
        ctr   = round(clks / impr * 100, 2) if impr > 0 else 0

        actions = row.get("actions", [])
        messaging = 0
        for a in actions:
            if a["action_type"] in ("onsite_conversion.total_messaging_connection",
                                     "onsite_conversion.messaging_first_reply"):
                messaging = max(messaging, int(a.get("value", 0)))

        cpr = round(spent / max(messaging, 1), 2) if messaging > 0 else 0

        campaigns = fetch_meta_campaigns(account_id, META_ACCESS_TOKEN, account_name)
        print(f"    ✅ {account_name}: AED {spent} | {impr:,} impr | {clks} clicks | {messaging} msgs")
        return {
            "spent"          : spent,
            "impressions"    : impr,
            "reach"          : reach,
            "clicks"         : clks,
            "results"        : messaging,
            "cost_per_result": cpr,
            "ctr"            : ctr,
            "campaigns"      : campaigns,
        }
    except Exception as e:
        print(f"  ❌ Meta API error for {account_name}: {e}")
        return {}


def fetch_device_split(client):
    """Fetch performance by device: Mobile, Desktop, Tablet."""
    print("  → Fetching device split...")
    ga_service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT
            segments.device,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.impressions
        FROM campaign
        WHERE segments.date = '{DATE_RANGE}'
          AND campaign.status = 'ENABLED'
          AND metrics.clicks > 0
    """
    try:
        response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
        devices = {}
        for row in response:
            dev  = str(row.segments.device).replace("Device.", "").title()
            clks = int(row.metrics.clicks)
            cost = round(row.metrics.cost_micros / 1_000_000, 2)
            conv = round(row.metrics.conversions, 1)
            impr = int(row.metrics.impressions)
            if dev not in devices:
                devices[dev] = {"clicks":0,"cost":0,"conv":0,"impr":0}
            devices[dev]["clicks"] += clks
            devices[dev]["cost"]   += cost
            devices[dev]["conv"]   += conv
            devices[dev]["impr"]   += impr

        rows = []
        for dev, v in sorted(devices.items(), key=lambda x: x[1]["clicks"], reverse=True):
            if dev in ("Unspecified", "Unknown", "Other"):
                continue
            ctr  = round(v["clicks"]/v["impr"]*100,1) if v["impr"] > 0 else 0
            cpc  = round(v["cost"]/v["conv"],2) if v["conv"] > 0 else 0
            conv_str = f" | {v['conv']} conv | AED {cpc}/conv" if v["conv"] > 0 else ""
            rows.append(f"• {dev}  —  {v['clicks']} clicks | AED {v['cost']:,.0f} | CTR {ctr}%{conv_str}")

        if rows:
            mobile_clicks = devices.get("Mobile", {}).get("clicks", 0)
            total_clicks  = sum(v["clicks"] for v in devices.values())
            mobile_pct    = round(mobile_clicks / total_clicks * 100) if total_clicks > 0 else 0
            if mobile_pct > 70:
                rows.append(f"\n⚡ {mobile_pct}% mobile traffic — ensure mobile landing page is optimised")

        print(f"    ✅ Devices: {len(rows)} found")
        return {"rows": rows or ["• No device data today"]}
    except Exception as ex:
        print(f"    ❌ Device split failed: {ex}")
        return {"rows": ["• Device data unavailable"]}

# ── DAY-OVER-DAY DELTA ────────────────────────────────────────────────────────

def load_yesterday():
    try:
        if os.path.exists(DELTA_FILE):
            with open(DELTA_FILE) as f:
                return json.load(f)
    except:
        pass
    return {}


def save_today(g_camp, meta, meta_rto=None):
    try:
        with open(DELTA_FILE, "w") as f:
            json.dump({
                "date"        : REPORT_DATE,
                "g_cost"      : g_camp.get("cost", 0),
                "g_clicks"    : g_camp.get("clicks", 0),
                "g_conv"      : g_camp.get("conversions", 0),
                "m_spent"     : meta.get("spent", 0),
                "m_results"   : meta.get("results", 0),
                "m_rto_spent" : meta_rto.get("spent", 0) if meta_rto else 0,
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

    if meta.get("spent", 0) > 0:
        score += 10; notes.append("✅ Meta Ads active")
        if meta.get("ctr", 0) >= 1.5:
            score += 5; notes.append("✅ Meta CTR strong")

    grade = (
        "🟢 Excellent"         if score >= 80 else
        "🟡 Good"              if score >= 60 else
        "🟠 Needs Improvement"  if score >= 40 else
        "🔴 Needs Attention"
    )
    return min(score, 100), grade, notes

# ══════════════════════════════════════════════════════════════════════════════
# v6.0 — PLAIN-ENGLISH GOOGLE REPORT (Gallabox real chats, CPC alerts,
#        classified search terms, "what to do today", explained health score)
# ══════════════════════════════════════════════════════════════════════════════

# Watch levels — avg cost per click above these is flagged ⚠️
CPC_WATCH = {"rental": 45.0, "rto": 15.0}

# Account changes that put Smart Bidding into a learning period.
# Add a line here whenever changes are made in Google Ads (date, short label).
RECENT_CHANGES = [
    ("2026-11-26", "bidding, WhatsApp tracking, schedules, ad assets"),
]
LEARNING_DAYS = 7

GALLABOX_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mtd_store.json")

# Searches that must NEVER be suggested as negatives (core business terms)
CORE_TERMS = ["rent", "rental", "hire", "lease", "subscription", "rent to own", "lease to own",
              "luxury car", "supercar", "sports car", "exotic car"]
OUR_BRANDS = ["mkv", "rent to own cars", "renttoowncars"]
# Fleet brands/models — never auto-suggested as junk; sent to "Decide" instead
FLEET_BRANDS = ["lamborghini", "urus", "huracan", "ferrari", "rolls", "cullinan", "ghost", "mercedes", "g63",
                "g class", "g-class", "brabus", "maybach", "porsche", "range rover", "bentley", "mclaren",
                "bmw", "audi", "cadillac", "escalade", "nissan patrol", "patrol", "lexus", "corvette", "mustang"]

# Junk categories → suggest blocking (only if not already blocked)
JUNK_CATEGORIES = {
    "competitor": ["ezhire", "maraya", "shift leasing", "u drive", "udrive", "selfdrive", "cars24", "dubizzle",
                   "invygo", "yalla ride", "one click drive", "oneclickdrive", "quick lease", "autostrad",
                   "speedy drive", "drive rent", "hertz", "budget rent", "thrifty", "europcar", "sixt",
                   "avis", "dollar rent", "ekar", "uber", "careem"],
    "bank / loan": ["loan", "bank", "mashreq", "emirates nbd", "enbd", "adcb", "fab ", "dib ", "rakbank",
                    "finance company", "credit card", "installment calculator", "emi"],
    "chauffeur / limo": ["chauffeur", "limo", "driver service", "with driver", "airport transfer", "taxi", "zofeur"],
    "jobs": ["job", "vacancy", "vacancies", "career", "hiring", "salary", "driver job"],
    "buying a car": ["for sale", "used car", "second hand", "buy car", "price in uae", "showroom", "auction"],
    "contact / customer care": ["contact number", "phone number", "customer care", "customer service",
                                "helpline", "toll free", "complaint", "whatsapp number", "login", "app download"],
    "outside Dubai": ["sharjah", "ajman", "ras al khaimah", "ras alkhaimah", "rasalkhaimah", " rak ",
                      "fujairah", "al ain", "umm al quwain", "khor fakkan", "oman", "muscat", "saudi",
                      "riyadh", "jeddah", "doha", "qatar"],
    "budget models": ["yaris", "corolla", "sunny", "attrage", "picanto", "kia soul", "hyundai staria",
                      "cheap", "economy", "monthly 1000", "aed 99"],
}
# Places MKV may or may not serve → ask instead of blocking
# Ads target Dubai only (Shahbaz 26 Nov 2026) → other emirates are junk for ads (block).
# Exception: Abu Dhabi stays targeted on Rent to Own (best cost per lead) but is
# excluded on Rental → Abu Dhabi searches are junk on Rental, relevant on Rent to Own.
DECIDE_PLACES = []
RENTAL_ONLY_JUNK_PLACES = ["abu dhabi", "abudhabi"]
JUNK_SPEND_14D = 50.0   # a non-category term is suggested only at ≥ AED 50 / 14 days with 0 conversions


def _camp_kind(name):
    n = name.lower()
    return "rto" if ("rent to own" in n or "lease" in n or " rto" in n) else "rental"


def fetch_campaigns_v6(client):
    """Yesterday per campaign (all enabled campaigns) + daily budgets."""
    ga = client.get_service("GoogleAdsService")
    q = f"""
        SELECT campaign.name, campaign_budget.amount_micros,
               metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions
        FROM campaign
        WHERE segments.date = '{DATE_RANGE}' AND campaign.status = 'ENABLED'
    """
    out = []
    try:
        for r in ga.search(customer_id=CUSTOMER_ID, query=q):
            cost = r.metrics.cost_micros / 1_000_000
            clicks = int(r.metrics.clicks)
            out.append({
                "name": r.campaign.name, "kind": _camp_kind(r.campaign.name),
                "cost": round(cost, 2), "clicks": clicks, "impr": int(r.metrics.impressions),
                "conv": round(r.metrics.conversions, 1),
                "cpc": round(cost / clicks, 2) if clicks else 0,
                "budget": round(r.campaign_budget.amount_micros / 1_000_000, 2),
            })
    except GoogleAdsException as ex:
        print(f"    ❌ v6 campaigns failed: {ex.error.code().name}")
    return out


def fetch_budgets_v6(client):
    """Daily budgets of all enabled campaigns (also those with no spend yesterday)."""
    ga = client.get_service("GoogleAdsService")
    q = "SELECT campaign.name, campaign_budget.amount_micros FROM campaign WHERE campaign.status = 'ENABLED'"
    try:
        return {r.campaign.name: r.campaign_budget.amount_micros / 1_000_000 for r in ga.search(customer_id=CUSTOMER_ID, query=q)}
    except GoogleAdsException as ex:
        print(f"    ❌ v6 budgets failed: {ex.error.code().name}")
        return {}


def fetch_negatives_v6(client):
    """All active negative keywords: shared lists + campaign-level negatives."""
    ga = client.get_service("GoogleAdsService")
    negs = set()
    queries = [
        """SELECT shared_criterion.keyword.text FROM shared_criterion
           WHERE shared_set.type = 'NEGATIVE_KEYWORDS' AND shared_set.status = 'ENABLED'""",
        """SELECT campaign_criterion.keyword.text FROM campaign_criterion
           WHERE campaign_criterion.negative = TRUE AND campaign_criterion.type = 'KEYWORD'
             AND campaign.status = 'ENABLED'""",
    ]
    for q in queries:
        try:
            for r in ga.search(customer_id=CUSTOMER_ID, query=q):
                kw = r.shared_criterion.keyword.text if "shared_criterion" in q else r.campaign_criterion.keyword.text
                if kw:
                    negs.add(kw.lower().strip())
        except GoogleAdsException as ex:
            print(f"    ⚠️  negatives query failed: {ex.error.code().name}")
    print(f"    ✅ Negative keywords loaded: {len(negs)}")
    return negs


def fetch_search_terms_v6(client):
    """Yesterday's paid search terms + 14-day cost/conversions per term."""
    ga = client.get_service("GoogleAdsService")
    start14 = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
    def run(where):
        q = f"""
            SELECT search_term_view.search_term, campaign.name,
                   metrics.clicks, metrics.cost_micros, metrics.conversions
            FROM search_term_view WHERE {where} AND metrics.cost_micros > 0
        """
        agg = {}
        try:
            for r in ga.search(customer_id=CUSTOMER_ID, query=q):
                t = r.search_term_view.search_term.lower().strip()
                a = agg.setdefault(t, {"term": t, "campaign": r.campaign.name, "clicks": 0, "cost": 0.0, "conv": 0.0})
                a["clicks"] += int(r.metrics.clicks)
                a["cost"] += r.metrics.cost_micros / 1_000_000
                a["conv"] += r.metrics.conversions
        except GoogleAdsException as ex:
            print(f"    ⚠️  search terms query failed: {ex.error.code().name}")
        return agg
    y = run(f"segments.date = '{DATE_RANGE}'")
    d14 = run(f"segments.date BETWEEN '{start14}' AND '{DATE_RANGE}'")
    for t, v in y.items():
        v["cost14"] = round(d14.get(t, {}).get("cost", v["cost"]), 2)
        v["conv14"] = round(d14.get(t, {}).get("conv", v["conv"]), 1)
        v["cost"] = round(v["cost"], 2)
    print(f"    ✅ Search terms yesterday: {len(y)}")
    return sorted(y.values(), key=lambda x: x["cost"], reverse=True)


def _is_blocked(term, negs):
    t = f" {term} "
    for n in negs:
        n2 = n.strip("[]\"+ ").lower()
        if n2 and (term == n2 or f" {n2} " in t):
            return True
    return False


def classify_terms(terms, negs):
    """→ dict(relevant, blocked, decide, junk) of term rows (with 'why')."""
    out = {"relevant": [], "blocked": [], "decide": [], "junk": []}
    for r in terms:
        t = r["term"]
        if _is_blocked(t, negs):
            out["blocked"].append(r); continue
        cat = next((c for c, words in JUNK_CATEGORIES.items() if any(w in f" {t} " for w in words)), None)
        if cat:
            r["why"] = cat; out["junk"].append(r); continue
        if _camp_kind(r["campaign"]) == "rental" and any(p in t for p in RENTAL_ONLY_JUNK_PLACES):
            r["why"] = "Abu Dhabi — excluded on Rental"; out["junk"].append(r); continue
        place = next((p.strip() for p in DECIDE_PLACES if p in f" {t} "), None)
        if place:
            r["why"] = f"do we deliver to {place.upper() if len(place) <= 3 else place.title()}?"
            out["decide"].append(r); continue
        core = any(w in t for w in CORE_TERMS + OUR_BRANDS)
        if core:
            out["relevant"].append(r); continue                       # core terms are never suggested as negatives
        if r["cost14"] >= JUNK_SPEND_14D and r["conv14"] == 0:
            if any(b in t for b in FLEET_BRANDS):
                r["why"] = f"fleet model but AED {r['cost14']:,.0f} in 14 days with 0 leads — keep or block?"
                out["decide"].append(r)
            else:
                r["why"] = f"AED {r['cost14']:,.0f} in 14 days, 0 leads"; out["junk"].append(r)
        elif r["conv"] > 0 or r["conv14"] > 0:
            out["relevant"].append(r)
        else:
            r["why"] = "unclear intent — keep or block?"; out["decide"].append(r)
    return out


def load_gallabox_counts():
    """Real WhatsApp chats from the Gallabox store (written daily by mkv_gallabox_snapshot.py).
    Google = Main no. (+971 56 279 4545) + any 'UID' website chat; RTO = Rent to Own no."""
    res = {"y_google": 0, "y_rto": 0, "m_google": 0, "m_rto": 0, "ok": False, "updated": ""}
    try:
        with open(GALLABOX_STORE, encoding="utf-8") as f:
            store = json.load(f)
    except Exception as e:
        print(f"  ⚠️  Gallabox store not readable: {e}")
        return res
    month = DATE_RANGE[:7]
    for v in store.get("convs", {}).values():
        d = v.get("date", "")
        if not d.startswith(month) or d > DATE_RANGE:
            continue
        if v.get("origin") == "website" or v.get("channel") == "Main":
            k = "google"
        elif v.get("channel") == "Rent to Own":
            k = "rto"
        else:
            continue
        res["m_" + k] += 1
        if d == DATE_RANGE:
            res["y_" + k] += 1
    res["ok"] = True
    res["updated"] = store.get("updated", "")
    print(f"    ✅ Gallabox: yesterday Google {res['y_google']} / RTO {res['y_rto']} | MTD {res['m_google']} / {res['m_rto']}")
    return res


def learning_banner():
    """(active, text) — active for LEARNING_DAYS after the latest logged change."""
    if not RECENT_CHANGES:
        return False, ""
    last_date, label = max(RECENT_CHANGES)
    d0 = datetime.strptime(last_date, "%Y-%m-%d")
    rep = datetime.strptime(DATE_RANGE, "%Y-%m-%d")
    judge = d0 + timedelta(days=LEARNING_DAYS)
    if d0 <= rep < judge:
        return True, (f"ℹ️ Changes in effect: {label} ({d0.strftime('%d %b')}). "
                      f"Numbers will swing until ~{judge.strftime('%d %b')} — don't react to one day.")
    return False, ""


def health_v6(camps, gb, mtd_cost, mtd_conv, terms_cls, pace_gap, learning):
    """Start at 100 and subtract explained penalties."""
    pen = []
    g_chats = gb["m_google"] + gb["m_rto"]
    capture = (mtd_conv / g_chats * 100) if (gb["ok"] and g_chats) else None
    if capture is not None:
        if capture < 40:   pen.append(("🔴", "tracking", 30))
        elif capture < 60: pen.append(("🟠", "tracking", 15))
    for c in camps:
        if c["clicks"] and c["cpc"] > CPC_WATCH[c["kind"]]:
            pen.append(("🟠", f"{'Rental' if c['kind']=='rental' else 'RTO'} click cost", 20 if c["kind"] == "rental" else 10))
    y_cost = sum(c["cost"] for c in camps) or 0
    junk_cost = sum(r["cost"] for r in terms_cls["junk"])
    if y_cost and junk_cost / y_cost > 0.10:
        pen.append(("🟠", "junk searches", 15))
    if gb["ok"] and g_chats:
        cpl = mtd_cost / g_chats
        if cpl > 120:  pen.append(("🔴", "cost per real chat", 20))
        elif cpl > 80: pen.append(("🟠", "cost per real chat", 10))
    if pace_gap is not None and abs(pace_gap) > 15:
        pen.append(("🟠", "budget pace", 10))
    score = max(0, 100 - sum(p[2] for p in pen))
    dot = "🟢" if score >= 75 else "🟡" if score >= 55 else "🔴"
    detail = " · ".join(f"{e} {n} (−{p})" for e, n, p in pen) or "no penalties"
    return score, dot, detail, capture



# ── Plain status: ✅ PERFORMING / 🟡 AVERAGE / 🔴 NEEDS ATTENTION ──────────────
# Judged on cost per REAL WhatsApp chat (Gallabox) — the business target — plus click cost.
#   (good up to, average up to) — above the second number = needs attention
COST_PER_CHAT = {"rental": (40.0, 70.0), "rto": (150.0, 250.0)}
META_COST_PER_CHAT = {"mkv": (30.0, 50.0), "rto": (60.0, 100.0)}
STATUS = {0: "✅ PERFORMING", 1: "🟡 AVERAGE", 2: "🔴 NEEDS ATTENTION"}


def judge(kind, cost, chats, cpc=0.0, clicks=0, bands=None):
    """→ (level 0/1/2 or None if not running, short reason)."""
    if cost <= 0:
        return None, "not running"
    good, ok = (bands or COST_PER_CHAT)[kind]
    reasons, level = [], 0
    if chats == 0:
        if cost >= good:
            return 2, f"AED {cost:,.0f} spent, 0 chats"
        return 1, f"AED {cost:,.0f} spent, no chat yet"
    per = cost / chats
    if per > ok:
        level = 2; reasons.append(f"AED {per:,.0f}/chat (target ≤ {good:,.0f})")
    elif per > good:
        level = 1; reasons.append(f"AED {per:,.0f}/chat (target ≤ {good:,.0f})")
    else:
        reasons.append(f"AED {per:,.0f}/chat")
    if clicks and kind in CPC_WATCH and cpc > CPC_WATCH[kind]:
        level = max(level, 1); reasons.append(f"clicks expensive AED {cpc:,.0f} (watch {CPC_WATCH[kind]:,.0f})")
    return level, " · ".join(reasons)

def build_google_report_v6(camps, budgets, mtd, mtd_split, terms, negs, gb):
    mode_tag = "🧪 TEST" if TEST_MODE else "🚀 LIVE"
    day_label = datetime.strptime(DATE_RANGE, "%Y-%m-%d").strftime("%a %d %b %Y")
    learning, banner = learning_banner()
    cls = classify_terms(terms, negs)

    y_cost = sum(c["cost"] for c in camps)
    y_g_chats = gb["y_google"] + gb["y_rto"]
    rental = [c for c in camps if c["kind"] == "rental"]
    rto = [c for c in camps if c["kind"] == "rto"]

    # ── 1. One line
    if gb["ok"]:
        per = f"~AED {y_cost / y_g_chats:,.0f} per real chat" if y_g_chats else "no real chats yet"
        line = (f"📌 *In one line:* AED {y_cost:,.0f} spent → *{y_g_chats} WhatsApp chats* in Gallabox "
                f"({gb['y_google']} Rental no. · {gb['y_rto']} RTO no.) → {per}.")
    else:
        line = f"📌 *In one line:* AED {y_cost:,.0f} spent · Gallabox data unavailable today."
    alerts = [f"{'Rental' if c['kind']=='rental' else 'Rent to Own'} clicks were expensive (AED {c['cpc']:,.0f} each) ⚠️"
              for c in camps if c["clicks"] and c["cpc"] > CPC_WATCH[c["kind"]]]
    if alerts:
        line += " " + " ".join(alerts)

    # ── 2. Yesterday by campaign
    rows, levels, why = [], [], []
    for c in sorted(camps, key=lambda x: x["kind"]):
        icon, label = ("🚗", "Rental") if c["kind"] == "rental" else ("🔑", "Rent to Own")
        watch = CPC_WATCH[c["kind"]]
        flag = (f"⚠️ (watch level AED {watch:,.0f})" if c["cpc"] > watch else "✅") if c["clicks"] else ""
        gal = gb["y_google"] if c["kind"] == "rental" else gb["y_rto"]
        lvl, reason = judge(c["kind"], c["cost"], gal, c["cpc"], c["clicks"]) if gb["ok"] else (None, "")
        status = f"  →  *{STATUS[lvl]}*" if lvl is not None else ""
        if lvl is not None:
            levels.append(lvl)
            if lvl > 0:
                why.append(f"{label}: {reason}")
        rows.append(f"{icon} *{label}*{status}\n      AED {c['cost']:,.0f} | {c['clicks']} clicks | "
                    f"AED {c['cpc']:,.1f}/click {flag} | Google leads {c['conv']:g} | Gallabox chats {gal if gb['ok'] else '–'}")
    if not rto:
        rows.append("🔑 *Rent to Own*  no spend yesterday" + (" (runs Mon–Thu)" if datetime.strptime(DATE_RANGE, "%Y-%m-%d").weekday() >= 4 else ""))
    camp_text = "\n".join(rows) or "_No campaign data_"

    # ── 3. Month to date
    mtd_cost = mtd.get("cost", 0); mtd_conv = mtd.get("conversions", 0)
    rep = datetime.strptime(DATE_RANGE, "%Y-%m-%d")
    import calendar
    days_in_month = calendar.monthrange(rep.year, rep.month)[1]
    month_gone = rep.day / days_in_month * 100
    monthly_budget = sum(budgets.values()) * 30.4 if budgets else 0
    pace_gap = None
    m_chats = gb["m_google"] + gb["m_rto"]
    mtd_lines = [f"Spend AED {mtd_cost:,.0f} · Google-tracked leads {mtd_conv:g}"
                 + (f" · Gallabox chats {m_chats} (Google no. {gb['m_google']} + RTO {gb['m_rto']})" if gb["ok"] else "")]
    score, dot, detail, capture = health_v6(camps, gb, mtd_cost, mtd_conv, cls, None, learning)
    if gb["ok"] and m_chats:
        cap_icon = "✅" if capture >= 60 else ("⚠️" if capture >= 40 else "❌")
        cap_note = " (tracking fix 26 Nov — expect 60%+)" if capture < 60 and learning else ""
        mtd_lines.append(f"Real cost per chat ≈ AED {mtd_cost / m_chats:,.0f} · Tracking capture {capture:.0f}% {cap_icon}{cap_note}")
    if monthly_budget:
        spent_pct = mtd_cost / monthly_budget * 100
        pace_gap = spent_pct - month_gone
        p_icon = "✅" if abs(pace_gap) <= 15 else ("⚠️ overspending" if pace_gap > 0 else "⚠️ underspending")
        mtd_lines.append(f"Budget pace: {spent_pct:.0f}% of month budget spent with {month_gone:.0f}% of month gone {p_icon}")
    mtd_levels = []
    if mtd_split:
        r, t = mtd_split.get("rental", {}), mtd_split.get("rto", {})
        for icon, label, kind, d, chats in (("🚗", "Rental", "rental", r, gb["m_google"]),
                                            ("🔑", "Rent to Own", "rto", t, gb["m_rto"])):
            lvl, reason = judge(kind, d.get("cost", 0), chats) if gb["ok"] else (None, "")
            if lvl is not None:
                mtd_levels.append(lvl)
            st = f" → *{STATUS[lvl]}* — {reason}" if lvl is not None else ""
            mtd_lines.append(f"{icon} {label}: AED {d.get('cost',0):,.0f} · {chats if gb['ok'] else '–'} chats{st}")
    score, dot, detail, capture = health_v6(camps, gb, mtd_cost, mtd_conv, cls, pace_gap, learning)
    mtd_text = "\n".join(mtd_lines)

    # ── 4. Searches that cost money
    def names(lst, n=6, extra=False):
        out = []
        for r in lst[:n]:
            s = r["term"][:40]
            if extra:
                s += f" ({'Rental' if _camp_kind(r['campaign'])=='rental' else 'RTO'}, AED {r['cost']:,.0f})"
            out.append(s)
        more = f" +{len(lst)-n} more" if len(lst) > n else ""
        return " · ".join(out) + more
    st = []
    st.append("✅ *Relevant:* " + (names(cls["relevant"], extra=True) + " — keep" if cls["relevant"] else "none"))
    st.append("⛔ *Already blocked:* " + (names(cls["blocked"]) + " — no action needed" if cls["blocked"] else "none"))
    st.append("❓ *Decide:* " + ("; ".join(f"{r['term'][:40]} — {r['why']}" for r in cls["decide"][:4]) if cls["decide"] else "none"))
    st.append("➕ *New junk to block:* " + ("; ".join(f"{r['term'][:40]} (AED {r['cost']:,.0f} yesterday · {r['why']})" for r in cls["junk"][:5]) if cls["junk"] else "none"))
    terms_text = "\n".join(st)

    # ── 5. What to do today
    todo = []
    if learning:
        todo.append("Nothing on bids/budgets — learning period (judge from "
                    + (datetime.strptime(max(RECENT_CHANGES)[0], "%Y-%m-%d") + timedelta(days=LEARNING_DAYS)).strftime("%d %b") + ")")
    for c in camps:
        if c["clicks"] and c["cpc"] > CPC_WATCH[c["kind"]] and not learning:
            todo.append(f"{'Rental' if c['kind']=='rental' else 'RTO'} CPC AED {c['cpc']:,.0f} > AED {CPC_WATCH[c['kind']]:,.0f} — if 3 days running, add a max-CPC cap")
    if cls["junk"]:
        todo.append("Block: " + ", ".join(r["term"][:30] for r in cls["junk"][:5]) + " (add to MKV - Account negatives)")
    for r in cls["decide"][:3]:
        todo.append(f"Answer: {r['term'][:35]} — {r['why']}")
    if gb["ok"] and y_cost > 100 and y_g_chats == 0:
        todo.append("0 Gallabox chats on AED %d spend — check ads are serving and WhatsApp links work" % y_cost)
    if not todo:
        todo.append("Nothing — all on track ✅")
    todo_text = "\n".join(f"{i}. {t}" for i, t in enumerate(todo, 1))

    if levels:
        overall = max(levels)
        verdict = f"*How ads did yesterday: {STATUS[overall]}*"
        verdict += ("\n" + "\n".join(f"• {w}" for w in why)) if why else "\n• All running campaigns within target"
    else:
        verdict = "*How ads did yesterday: ⚪ NO DATA* — no spend or Gallabox data unavailable"
    if mtd_levels:
        verdict += f"\n_Month so far: {STATUS[max(mtd_levels)]}_"
    verdict += ("\n_Scale: ✅ Performing = at/below target cost per real chat · 🟡 Average = slightly above target "
                "or clicks expensive · 🔴 Needs attention = far above target or spend with 0 chats_")
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🔵 Google Ads — {day_label}", "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": verdict}},
        {"type": "section", "text": {"type": "mrkdwn", "text": line}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*━━ Yesterday by campaign ━━*\n{camp_text}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*━━ Month to date (1–{rep.day} {rep.strftime('%b')}) ━━*\n{mtd_text}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*━━ Searches that cost money yesterday ━━*\n{terms_text}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*━━ What to do today ━━*\n{todo_text}"}},
    ]
    if banner:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": banner}]})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                   "text": f"🏁 *Health {score}/100* {dot} — {detail}   |   v6.1 • {mode_tag}"}]})
    return blocks


# ── META: one-line summary + which ad caused the warnings ─────────────────────

def fetch_meta_ads_v6(account_id):
    """Yesterday at ad level: name, spend, CTR, messaging results."""
    token = META_ACCESS_TOKEN
    if not token or not account_id:
        return []
    try:
        url = (f"https://graph.facebook.com/v20.0/act_{account_id}/insights"
               f"?fields=ad_name,campaign_name,spend,impressions,clicks,actions"
               f"&date_preset=yesterday&level=ad&limit=100&access_token={token}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        ads = []
        for r in data.get("data", []):
            spend = float(r.get("spend", 0))
            if spend <= 0:
                continue
            impr, clks = int(r.get("impressions", 0)), int(r.get("clicks", 0))
            res = 0
            for a in r.get("actions", []) or []:
                if a["action_type"] in ("onsite_conversion.total_messaging_connection",
                                        "onsite_conversion.messaging_first_reply"):
                    res = max(res, int(a.get("value", 0)))
            ads.append({"name": r.get("ad_name", "")[:45], "spend": round(spend, 2), "res": res,
                        "ctr": round(clks / impr * 100, 2) if impr else 0,
                        "cpr": round(spend / res, 2) if res else 0})
        return sorted(ads, key=lambda x: x["spend"], reverse=True)
    except Exception as e:
        print(f"  ⚠️  Meta ad-level error: {e}")
        return []


def meta_summary_v6(label, acc, ads, cpr_watch):
    """One line + the ads behind each warning."""
    if not acc or acc.get("spent", 0) <= 0:
        return f"*{label}:* no spend yesterday"
    kind = "rto" if "own" in label.lower() else "mkv"
    lvl, _ = judge(kind, acc["spent"], acc.get("results", 0), bands=META_COST_PER_CHAT)
    status = f" → {STATUS[lvl]}" if lvl is not None else ""
    s = (f"*{label}{status}*\n  AED {acc['spent']:,.0f} → *{acc['results']} chats*"
         + (f" → AED {acc['cost_per_result']:,.0f} per chat" if acc["results"] else " → no chats ⚠️")
         + f" · CTR {acc['ctr']}%")
    warn = []
    for a in ads:
        if a["spend"] >= 30 and a["res"] == 0:
            warn.append(f"• `{a['name']}` — AED {a['spend']:,.0f}, 0 chats ❌ pause or refresh")
        elif a["res"] and a["cpr"] > cpr_watch:
            warn.append(f"• `{a['name']}` — AED {a['cpr']:,.0f}/chat (watch AED {cpr_watch:,.0f}) ⚠️")
        elif a["spend"] >= 30 and a["ctr"] < 1:
            warn.append(f"• `{a['name']}` — CTR {a['ctr']}% ⚠️ creative tired")
    best = max((a for a in ads if a["res"]), key=lambda a: (a["res"], -a["cpr"]), default=None)
    if best:
        s += f"\n  🏆 Best ad: `{best['name']}` — {best['res']} chats at AED {best['cpr']:,.0f}"
    if warn:
        s += "\n  Warnings come from:\n" + "\n".join(warn[:4])
    return s


def build_meta_summary_block(meta, meta_rto, ads_mkv, ads_rto):
    text = (meta_summary_v6("MKV Luxury", meta, ads_mkv, 50) + "\n\n"
            + meta_summary_v6("Lease to Own", meta_rto, ads_rto, 100))
    return [{"type": "section", "text": {"type": "mrkdwn", "text": f"*📌 Meta in one look*\n{text}"}},
            {"type": "divider"}]


# ── SLACK BLOCK KIT ───────────────────────────────────────────────────────────

def build_google_report(g_camp, g_mtd, g_mtd_split, g_conv, g_search, g_auction, g_landing, g_geo, g_device, yesterday, kw_recs, comp_kw):
    mode_tag = "🧪 TEST" if TEST_MODE else "🚀 LIVE"
    d_cost = delta(g_camp.get("cost", 0), yesterday.get("g_cost", 0))
    d_conv = delta(g_camp.get("conversions", 0), yesterday.get("g_conv", 0), prefix="")

    # ── 1. Daily Overview
    daily = (
        f"💰 *Spend:* AED {g_camp.get('cost',0):,.2f}{d_cost}\n"
        f"👆 *Clicks:* {g_camp.get('clicks',0):,}   👁 *Impressions:* {g_camp.get('impressions',0):,}   📊 *CTR:* {g_camp.get('ctr',0)}%\n"
        f"🎯 *Conversions:* {g_camp.get('conversions',0)}{d_conv}   💵 *Cost/Conv:* AED {g_camp.get('cost_per_conv',0):,.2f}"
    )
    if g_mtd:
        daily += f"\n📅 *MTD:* AED {g_mtd.get('cost',0):,.0f} spent  |  {g_mtd.get('conversions',0)} conv  |  AED {g_mtd.get('cost_per_conv',0):,.2f}/conv"

    # ── 2. Campaign Breakdown
    conv_map = {}
    for cv in g_conv.get("breakdown", []):
        if "—" in cv:
            parts = cv.split("—")
            cname = parts[0].replace("•","").strip()
            conv_map[cname] = parts[1].strip()

    camp_lines = []
    for c in g_camp.get("campaigns", []):
        conv_info = conv_map.get(c["name"], "")
        conv_str  = f"  |  {conv_info}" if conv_info else ""
        camp_lines.append(
            f"*{c['name']}*\n"
            f"  AED {c['spend']:,.0f}  |  {c['clicks']:,} clicks  |  {c['ctr']}% CTR{conv_str}"
        )
    camp_text = "\n\n".join(camp_lines) or "_No campaign data_"

    # ── 3. MTD Split
    mtd_split_text = "_No MTD split_"
    if g_mtd_split:
        r = g_mtd_split.get("rental", {})
        t = g_mtd_split.get("rto", {})
        mtd_split_text = (
            f"🚗 *Rental:*  AED {r.get('cost',0):,.0f}  |  {r.get('conversions',0)} conv  |  AED {r.get('cost_per_conv',0):,.2f}/conv\n"
            f"🔄 *Rent-to-Own:*  AED {t.get('cost',0):,.0f}  |  {t.get('conversions',0)} conv  |  AED {t.get('cost_per_conv',0):,.2f}/conv"
        )

    # ── 4. Search Terms
    raw_terms = g_search.get("raw", [])
    search_lines = []
    for r in sorted(raw_terms, key=lambda x: x["clicks"], reverse=True)[:8]:
        conv_str = f" | {r['conv']} conv" if r["conv"] > 0 else ""
        search_lines.append(
            f"• `{r['term'][:40]}` — {r['clicks']} clicks{conv_str} | _{r['campaign'][:25]}_"
        )
    search_text = "\n".join(search_lines) or "_No search term data_"

    # ── 5. Keyword Actions
    add_kw = kw_recs.get("add", [])
    neg_kw = kw_recs.get("remove", [])
    kw_text = ""
    if add_kw:
        kw_text += "*➕ Add as exact match:*\n" + "\n".join(add_kw[:4])
    if neg_kw:
        kw_text += ("\n\n" if kw_text else "") + "*➖ Review / add as negative:*\n" + "\n".join(neg_kw[:4])
    if not kw_text:
        kw_text = "_No keyword actions today_"

    # ── 6. Device split
    device_text = "\n".join(g_device.get("rows", [])) or "_No device data_"

    # ── Score
    score = 0
    if g_camp.get("ctr", 0) >= 3:           score += 25
    elif g_camp.get("ctr", 0) >= 1.5:       score += 15
    if g_camp.get("conversions", 0) >= 5:    score += 30
    elif g_camp.get("conversions", 0) >= 1:  score += 15
    if 0 < g_camp.get("cost_per_conv",0) < 100: score += 20
    if g_camp.get("cost", 0) > 0:           score += 10
    grade = "🟢 Excellent" if score >= 70 else "🟡 Good" if score >= 50 else "🔴 Needs Attention"

    return [
        {"type": "header",
         "text": {"type": "plain_text", "text": f"🔵 Google Ads — {REPORT_DATE}", "emoji": True}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": daily}},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*🎯 Campaign Breakdown*\n\n{camp_text}"}},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*📈 MTD Split*\n{mtd_split_text}"}},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*🔍 Search Terms*\n{search_text}"}},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*🔑 Keyword Actions*\n{kw_text}"}},
        {"type": "divider"},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
                       "text": f"*🏆 Score: {score}/100 — {grade}*   |   MKV Google Ads v5.7 • {mode_tag}"}]},
    ]


def build_meta_report(meta, meta_mtd, meta_rto, meta_rto_mtd, meta_placement, meta_age_gender, yesterday):
    """Clean Meta Ads daily report — campaign focused."""
    mode_tag = "🧪 TEST" if TEST_MODE else "🚀 LIVE"
    d_mcost  = delta(meta.get("spent", 0), yesterday.get("m_spent", 0))
    d_rto    = delta(meta_rto.get("spent", 0) if meta_rto else 0, yesterday.get("m_rto_spent", 0))

    # ── MKV Luxury
    if meta.get("spent", 0) > 0:
        mkv_text = (
            f"*Spent:* AED {meta['spent']:,.2f}{d_mcost}    "
            f"*Results:* {meta['results']}    "
            f"*Cost/Result:* AED {meta['cost_per_result']:,.2f}\n"
            f"*Impressions:* {meta['impressions']:,}    "
            f"*Reach:* {meta['reach']:,}    "
            f"*CTR:* {meta['ctr']}%"
        )
        if meta_mtd:
            mkv_text += f"\n*MTD:* AED {meta_mtd.get('spent',0):,.0f} | {meta_mtd.get('results',0)} results"
        if meta.get("campaigns"):
            mkv_text += "\n\n*Campaigns:*"
            for c in meta["campaigns"]:
                mkv_text += f"\n• {c['name']}  —  AED {c['spend']:,.0f} | {c['clicks']} clicks | {c['ctr']}% CTR"
        actions = []
        if meta.get("ctr", 0) < 1:
            actions.append("⚡ CTR below 1% — refresh creative/audience")
        if meta.get("cost_per_result", 0) > 50:
            actions.append("⚡ High cost/result — test new ad formats")
        if meta.get("results", 0) == 0:
            actions.append("⚡ Zero results — check campaign delivery")
        if meta.get("impressions", 0) > 0 and meta.get("results", 0) > 0:
            cpr = meta.get("cost_per_result", 0)
            if cpr > 30:
                actions.append("⚡ Cost/result AED {:.0f} — test new audience or creative".format(cpr))
            if meta.get("ctr", 0) < 1.5:
                actions.append("⚡ CTR {:.2f}% — refresh ad creative".format(meta.get("ctr", 0)))
            else:
                actions.append("✅ CTR {:.2f}% — creative performing well".format(meta.get("ctr", 0)))
        if actions:
            mkv_text += "\n\n*🔴 Actions:*\n" + "\n".join(actions[:3])
    else:
        mkv_text = "_No MKV Luxury Meta data today_"

    # ── Lease to Own
    if meta_rto and meta_rto.get("spent", 0) > 0:
        rto_text = (
            f"*Spent:* AED {meta_rto['spent']:,.2f}{d_rto}    "
            f"*Results:* {meta_rto['results']}    "
            f"*Cost/Result:* AED {meta_rto['cost_per_result']:,.2f}\n"
            f"*Impressions:* {meta_rto['impressions']:,}    "
            f"*Reach:* {meta_rto['reach']:,}    "
            f"*CTR:* {meta_rto['ctr']}%"
        )
        if meta_rto_mtd:
            rto_text += f"\n*MTD:* AED {meta_rto_mtd.get('spent',0):,.0f} | {meta_rto_mtd.get('results',0)} results"
        if meta_rto.get("campaigns"):
            rto_text += "\n\n*Campaigns:*"
            for c in meta_rto["campaigns"]:
                rto_text += f"\n• {c['name']}  —  AED {c['spend']:,.0f} | {c['clicks']} clicks | {c['ctr']}% CTR"
        rto_actions = []
        if meta_rto.get("ctr", 0) < 1:
            rto_actions.append("⚡ CTR below 1% — refresh RTO creative")
        if meta_rto.get("cost_per_result", 0) > 100:
            rto_actions.append("⚡ High cost/result — review targeting")
        if meta_rto.get("results", 0) > 0:
            cpr = meta_rto.get("cost_per_result", 0)
            if cpr < 20:
                rto_actions.append("✅ Cost/result AED {:.0f} — efficient, consider scaling budget".format(cpr))
            elif cpr < 50:
                rto_actions.append("🟡 Cost/result AED {:.0f} — monitor closely".format(cpr))
            if meta_rto.get("ctr", 0) > 2:
                rto_actions.append("✅ CTR {:.2f}% — strong creative performance".format(meta_rto.get("ctr", 0)))
        if rto_actions:
            rto_text += "\n\n*🔴 Actions:*\n" + "\n".join(rto_actions[:3])
    else:
        rto_text = "_No Lease to Own Meta data today_"

    # ── Placement
    placement_text = "\n".join(meta_placement.get("placements", [])) or "_No placement data_"
    placement_actions = []
    for p in meta_placement.get("placements", []):
        if "reels" in p.lower():
            placement_actions.append("⚡ Reels active — use vertical video creative")
        if "instagram" in p.lower():
            placement_actions.append("⚡ Instagram driving traffic — check story quality")
    if placement_actions:
        placement_text += "\n\n*Actions:*\n" + "\n".join(list(dict.fromkeys(placement_actions))[:2])

    # ── Age/Gender
    age_text = "\n".join(meta_age_gender.get("segments", [])) or "_No demographic data_"
    if meta_age_gender.get("segments"):
        age_text += "\n\n*Actions:*\n⚡ Focus budget on top-converting segments above"

    # ── MTD Combined
    total_mtd = (
        meta_mtd.get("spent", 0) +
        (meta_rto_mtd.get("spent", 0) if meta_rto_mtd else 0)
    )
    mtd_text = (
        f"*Total Meta MTD:* AED {total_mtd:,.0f}\n"
        f"• MKV Luxury: AED {meta_mtd.get('spent',0):,.0f} | {meta_mtd.get('results',0)} results\n"
        f"• Lease to Own: AED {meta_rto_mtd.get('spent',0) if meta_rto_mtd else 0:,.0f} | {meta_rto_mtd.get('results',0) if meta_rto_mtd else 0} results"
    ) if total_mtd > 0 else "_No MTD data_"

    return [
        {"type": "header", "text": {"type": "plain_text", "text": f"🟦 Meta Ads Report — {REPORT_DATE}", "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*MKV Luxury*\n{mkv_text}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*🟪 Lease to Own*\n{rto_text}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*📺 Placement Breakdown*\n{placement_text}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*📅 MTD Summary*\n{mtd_text}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"_MKV Meta Ads • v6.1 • {mode_tag}_"}]},
    ]


def post_slack(blocks, fallback="MKV Ads Report"):
    client = WebClient(token=SLACK_TOKEN)
    try:
        client.chat_postMessage(channel=SLACK_CHANNEL, blocks=blocks, text=fallback, unfurl_links=False, unfurl_media=False)
        print(f"  ✅  Posted to Slack: {SLACK_CHANNEL}")
    except SlackApiError as e:
        print(f"  ❌  Slack error: {e.response['error']}")


def main():
    print("=" * 60)
    print("  MKV Daily Ads Snapshot v6.1")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Report date: {REPORT_DATE}")
    print(f"  Mode: {'🧪 TEST' if TEST_MODE else '🚀 LIVE'}  |  Channel: {SLACK_CHANNEL}")
    print("=" * 60)

    client = None
    g_camp = g_conv = g_search = g_auction = {}
    g_mtd = g_mtd_split = {}
    g_landing = {"pages": [], "rto_pages": []}
    g_geo = {"countries": [], "cities": []}
    g_device = {"rows": []}

    print("\n📊 Fetching Google Ads data...")
    try:
        client    = get_google_ads_client()
        g_camp    = fetch_campaign_performance(client)
        g_mtd     = fetch_mtd_google(client)
        g_mtd_split = fetch_mtd_google_split(client)
        g_conv    = fetch_conversions(client)
        g_search  = fetch_search_terms(client)
        g_auction = fetch_auction_insights(client)
        g_landing = fetch_landing_pages(client)
        g_geo     = fetch_geographic(client)
        g_device  = fetch_device_split(client)
    except Exception as e:
        print(f"  ❌  Google Ads API error: {e}")

    print("\n📥 Fetching Meta Ads data...")
    meta         = fetch_meta_api(META_AD_ACCOUNT_ID, "MKV Luxury")
    meta_rto     = fetch_meta_api(META_RTO_ACCOUNT_ID, "MKV Lease to Own") if META_RTO_ACCOUNT_ID else {}
    meta_mtd     = fetch_meta_mtd(META_AD_ACCOUNT_ID)
    meta_rto_mtd = fetch_meta_mtd(META_RTO_ACCOUNT_ID) if META_RTO_ACCOUNT_ID else {}
    meta_placement  = fetch_meta_placement(META_AD_ACCOUNT_ID) if META_AD_ACCOUNT_ID else {}
    meta_age_gender = fetch_meta_age_gender(META_AD_ACCOUNT_ID) if META_AD_ACCOUNT_ID else {}

    yesterday = load_yesterday()
    save_today(g_camp, meta, meta_rto)

    kw_recs  = fetch_keyword_recommendations(client, g_search) if client and g_search else {"add": [], "remove": []}
    comp_kw  = fetch_competitor_keywords(client) if client and g_search else {"competitor_terms": [], "opportunity_terms": []}

    print("\n📤 Posting Google Ads report to Slack...")
    try:
        camps_v6   = fetch_campaigns_v6(client) if client else []
        budgets_v6 = fetch_budgets_v6(client) if client else {}
        terms_v6   = fetch_search_terms_v6(client) if client else []
        negs_v6    = fetch_negatives_v6(client) if client else set()
        gb_v6      = load_gallabox_counts()
        google_blocks = build_google_report_v6(camps_v6, budgets_v6, g_mtd, g_mtd_split, terms_v6, negs_v6, gb_v6)
    except Exception as e:
        print(f"  ⚠️  v6 report failed ({e}) — falling back to v5.7 layout")
        google_blocks = build_google_report(g_camp, g_mtd, g_mtd_split, g_conv, g_search, g_auction, g_landing, g_geo, g_device, yesterday, kw_recs, comp_kw)
    post_slack(google_blocks, "MKV Google Ads Report")
    print(f"\n  Google -> AED {g_camp.get('cost',0):.2f} | {g_camp.get('clicks',0)} clicks | {g_camp.get('conversions',0)} conv")

    if meta.get("spent", 0) > 0 or (meta_rto and meta_rto.get("spent", 0) > 0):
        print("\n📤 Posting Meta Ads report to Slack...")
        meta_blocks = build_meta_report(meta, meta_mtd, meta_rto, meta_rto_mtd, meta_placement, meta_age_gender, yesterday)
        try:
            summary = build_meta_summary_block(meta, meta_rto, fetch_meta_ads_v6(META_AD_ACCOUNT_ID),
                                               fetch_meta_ads_v6(META_RTO_ACCOUNT_ID))
            meta_blocks = meta_blocks[:1] + summary + meta_blocks[1:]
        except Exception as e:
            print(f"  ⚠️  Meta summary failed: {e}")
        post_slack(meta_blocks, "MKV Meta Ads Report")
        print(f"  Meta   -> AED {meta.get('spent',0):.2f} | {meta.get('results',0)} results")
    else:
        print("\n⚠️  No Meta data — skipping Meta report post")

    print("\n✅  Done!\n")


if __name__ == "__main__":
    main()
