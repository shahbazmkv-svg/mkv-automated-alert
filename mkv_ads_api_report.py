"""
MKV Daily Ads Snapshot — Google Ads API + Meta Gmail → Slack
=============================================================
Version : 3.1  (19 May 2026)
Fixes:
  - URL previews suppressed in Slack (wrapped in < >)
  - Conversions query fixed (use campaign resource)
  - Auction insights fixed (use campaign resource)
  - Rent-to-Own separate section added
  - Meta CSV parse improved
"""

import os
import io
import re
import json
import time
import gzip
import imaplib
import urllib.request
import pandas as pd
from datetime import datetime, timedelta
from email import message_from_bytes
from email.header import decode_header
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

META_SUBJECT_KEYWORD = "Your Daily Facebook ads report"

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
    # Extract ID from resource name or raw string
    geo_id = resource_name.split("/")[-1].split("~")[0].strip()

    # Try known IDs first
    if geo_id in GEO_NAMES:
        return GEO_NAMES[geo_id]

    # Try Google API
    try:
        gtc  = geo_svc.get_geo_target_constant(resource_name=f"geoTargetConstants/{geo_id}")
        name = gtc.canonical_name or gtc.name or ""
        # canonical_name = "Dubai, Dubai, United Arab Emirates" → take first segment
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
            # "Dubai, Dubai, United Arab Emirates" → "Dubai"
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

            # Classify: countries are typically short names without commas
            # location_type 1 = LOCATION_OF_PRESENCE (physical location)
            # location_type 2 = AREA_OF_INTEREST
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

        # If city_agg empty, try to get UAE city breakdown separately
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
    # Use campaign resource which supports date segmentation
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
    """
    Auction Insights is not available via Google Ads API (UI-only feature).
    Instead, fetch impression share & search rank metrics per campaign.
    """
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


def fetch_weekly_summary(client):
    """Fetch last 7 days summary."""
    print("  → Fetching weekly summary...")
    ga_service = client.get_service("GoogleAdsService")
    week_start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    query = f"""
        SELECT
            segments.date,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.impressions
        FROM customer
        WHERE segments.date BETWEEN '{week_start}' AND '{DATE_RANGE}'
        ORDER BY segments.date ASC
    """
    try:
        response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
        daily = {}
        for row in response:
            d = row.segments.date
            daily[d] = {
                "clicks": int(row.metrics.clicks),
                "cost"  : round(row.metrics.cost_micros / 1_000_000, 2),
                "conv"  : round(row.metrics.conversions, 1),
                "impr"  : int(row.metrics.impressions),
            }
        total_clicks = sum(v["clicks"] for v in daily.values())
        total_cost   = round(sum(v["cost"] for v in daily.values()), 2)
        total_conv   = round(sum(v["conv"] for v in daily.values()), 1)
        avg_ctr      = round(total_clicks / max(sum(v["impr"] for v in daily.values()), 1) * 100, 2)
        best_day     = max(daily.items(), key=lambda x: x[1]["conv"], default=(None, {}))
        worst_day    = min(daily.items(), key=lambda x: x[1]["conv"], default=(None, {}))
        rows = [
            f"*7-Day Totals:* AED {total_cost:,.0f} | {total_clicks:,} clicks | {total_conv} conv | {avg_ctr}% CTR",
            f"*Best day:* {best_day[0]} — {best_day[1].get('conv',0)} conv | AED {best_day[1].get('cost',0):,.0f}",
            f"*Weakest day:* {worst_day[0]} — {worst_day[1].get('conv',0)} conv | AED {worst_day[1].get('cost',0):,.0f}",
            "\n*Daily Breakdown:*"
        ]
        for date, vals in sorted(daily.items()):
            day_name = datetime.strptime(date, "%Y-%m-%d").strftime("%a %d %b")
            rows.append(f"  {day_name}  —  AED {vals['cost']:,.0f} | {vals['clicks']} clicks | {vals['conv']} conv")
        print(f"    ✅ Weekly: {len(daily)} days")
        return {"rows": rows}
    except GoogleAdsException as ex:
        print(f"    ❌ Weekly failed: {ex.error.code().name}")
        return {"rows": []}


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


def fetch_competitor_keywords(client):
    """
    Identify competitor-branded search terms in your traffic
    and suggest keywords to capture competitor leads.
    """
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

    # Known competitors in Dubai luxury car rental
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

            # Check if competitor name appears in search term
            for comp in competitor_names:
                if comp in term:
                    competitor_terms.append(
                        f"• `{row.search_term_view.search_term[:45]}`  "
                        f"— {clks} clicks | {conv} conv | AED {cost:.0f}"
                    )
                    break

            # High-intent terms with good volume but no conversions = opportunity
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
            # Suppress URL previews by wrapping in < >
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

# ── META ADS — GMAIL ──────────────────────────────────────────────────────────

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


def fetch_meta_weekly(account_id):
    """Fetch Meta Ads last 7 days day-by-day breakdown."""
    token = META_ACCESS_TOKEN
    if not token:
        return {}
    week_start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    try:
        url = (
            f"https://graph.facebook.com/v20.0/act_{account_id}/insights"
            f"?fields=spend,impressions,clicks,actions,date_start"
            f"&time_range=%7B%22since%22%3A%22{week_start}%22%2C%22until%22%3A%22{DATE_RANGE}%22%7D"
            f"&time_increment=1"
            f"&level=account"
            f"&access_token={token}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        daily = {}
        for row in data.get("data", []):
            date  = row.get("date_start", "")
            spent = round(float(row.get("spend", 0)), 2)
            clks  = int(row.get("clicks", 0))
            impr  = int(row.get("impressions", 0))
            results = 0
            for a in row.get("actions", []):
                if a["action_type"] == "onsite_conversion.total_messaging_connection":
                    results = max(results, int(a.get("value", 0)))
            if date and spent > 0:
                daily[date] = {"spent": spent, "clicks": clks, "results": results, "impr": impr}
        return {"daily": daily}
    except Exception as e:
        print(f"  ⚠️  Meta weekly error: {e}")
        return {}


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
        # Use yesterday date explicitly for reliability
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
        
        # Parse actions
        actions = row.get("actions", [])
        results = 0
        messaging = 0
        for a in actions:
            if a["action_type"] in ("onsite_conversion.total_messaging_connection",
                                     "onsite_conversion.messaging_first_reply"):
                messaging = max(messaging, int(a.get("value", 0)))
            if a["action_type"] in ("link_click", "landing_page_view"):
                results += int(a.get("value", 0))
        
        # Cost per result
        cpr = round(spent / max(messaging, 1), 2) if messaging > 0 else 0
        
        # Fetch campaign breakdown
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


def imap_connect(retries=3):
    for attempt in range(1, retries + 1):
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            mail.select("inbox")
            print(f"  ✅  Gmail connected")
            return mail
        except Exception as e:
            print(f"  ⚠️   Gmail attempt {attempt} failed: {e}")
            if attempt < retries:
                time.sleep(5)
    return None


def fetch_meta_email(mail):
    for days_ago in [0, 1]:
        since = (datetime.now() - timedelta(days=days_ago)).strftime("%d-%b-%Y")
        query = f'(SUBJECT "{META_SUBJECT_KEYWORD}" SINCE {since})'
        _, msg_ids = mail.search(None, query)
        ids = msg_ids[0].split() if msg_ids[0] else []
        if ids:
            _, msg_data = mail.fetch(ids[-1], "(RFC822)")
            msg = message_from_bytes(msg_data[0][1])
            print(f"  ✅  Meta email found")
            return msg
    print(f"  ⚠️   Meta email not found")
    return None


def extract_csv(msg):
    """Extract CSV from email — handles attachments and download links."""
    # 1. Check for direct CSV attachment
    for part in msg.walk():
        fname = part.get_filename() or ""
        ctype = part.get_content_type()
        if fname.endswith(".csv") or ctype in ("text/csv", "application/csv", "application/octet-stream"):
            payload = part.get_payload(decode=True)
            if payload:
                for enc in ("utf-8-sig", "utf-8", "latin-1"):
                    try:
                        return payload.decode(enc)
                    except:
                        continue

    # 2. Look for CSV download link in HTML or plain text body (Meta Ads emails)
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype in ("text/html", "text/plain"):
            body = part.get_payload(decode=True)
            if not body:
                continue
            text = body.decode("utf-8", errors="ignore")
            # Meta CSV download link pattern
            patterns = [
                r'https?://[^\s"<>]+\.csv[^\s"<>]*',
                r'https?://lookaside\.facebook\.com/[^\s"<>]+',
                r'https?://[^\s"<>]*facebook[^\s"<>]*csv[^\s"<>]*',
                r'https?://[^\s"<>]*report[^\s"<>]*\.csv[^\s"<>]*',
            ]
            for pattern in patterns:
                match = re.search(pattern, text)
                if match:
                    url = match.group(0).strip().rstrip('>').rstrip('"')
                    print(f"    → Downloading Meta CSV from: {url[:80]}...")
                    try:
                        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req, timeout=30) as resp:
                            raw = resp.read()
                            for enc in ("utf-8-sig", "utf-8", "latin-1"):
                                try:
                                    return raw.decode(enc)
                                except:
                                    continue
                    except Exception as e:
                        print(f"    ⚠️  Download failed: {e}")
    return None


def norm_cols(df):
    df = df.copy()
    df.columns = [c.strip().lower().replace(" ", "_").replace("/", "_").replace("(", "").replace(")", "") for c in df.columns]
    return df


def to_num(series):
    return pd.to_numeric(
        series.astype(str).str.replace(",", "", regex=False)
              .str.replace("AED", "", regex=False)
              .str.replace("%", "", regex=False).str.strip(),
        errors="coerce"
    ).fillna(0)


def find_col(df, *keywords):
    for kw in keywords:
        for col in df.columns:
            if kw in col:
                return col
    return None


def parse_meta(csv_text):
    if not csv_text:
        return {}
    try:
        lines = csv_text.split("\n")
        start = 0
        for i, line in enumerate(lines):
            if any(kw in line for kw in ["Reach", "Impressions", "Results", "Amount", "Spent"]):
                start = i
                break
        df = pd.read_csv(io.StringIO("\n".join(lines[start:])))
        df = df.dropna(how="all").reset_index(drop=True)
        df = norm_cols(df)

        imp_col   = find_col(df, "impress")
        reach_col = find_col(df, "reach")
        clk_col   = find_col(df, "clicks")
        cst_col   = find_col(df, "spent", "spend", "amount")
        res_col   = find_col(df, "results")
        cpr_col   = find_col(df, "cost_per_result", "cost_per")

        spent = round(to_num(df[cst_col]).sum(), 2)   if cst_col   else 0
        impr  = int(to_num(df[imp_col]).sum())         if imp_col   else 0
        reach = int(to_num(df[reach_col]).sum())       if reach_col else 0
        clks  = int(to_num(df[clk_col]).sum())         if clk_col   else 0
        res   = int(to_num(df[res_col]).sum())          if res_col   else 0
        cpr   = round(to_num(df[cpr_col]).mean(), 2)   if cpr_col   else 0
        ctr   = round(clks / impr * 100, 2)            if impr > 0  else 0

        print(f"    ✅ Meta: AED {spent} | {res} results | CTR {ctr}%")
        return {"spent": spent, "impressions": impr, "reach": reach,
                "clicks": clks, "results": res, "cost_per_result": cpr, "ctr": ctr}
    except Exception as e:
        print(f"    ❌ Meta parse error: {e}")
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

# ── SLACK BLOCK KIT ───────────────────────────────────────────────────────────


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

        # Actions
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
    # Show highest clicks first, compact format
    raw_terms = g_search.get("raw", [])
    search_lines = []
    for r in sorted(raw_terms, key=lambda x: x["clicks"], reverse=True)[:8]:
        conv_str = f" | {r['conv']} conv" if r["conv"] > 0 else ""
        search_lines.append(
            f"• `{r['term'][:40]}` — {r['clicks']} clicks{conv_str} | _{r['campaign'][:25]}_"
        )
    search_text = "\n".join(search_lines) or "_No search term data_"

    # ── 5. Keyword Actions
    add_kw = g_kw_add = kw_recs.get("add", [])
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
                       "text": f"*🏆 Score: {score}/100 — {grade}*   |   MKV Google Ads v5.6 • {mode_tag}"}]},
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
        # Campaign breakdown
        if meta.get("campaigns"):
            mkv_text += "\n\n*Campaigns:*"
            for c in meta["campaigns"]:
                mkv_text += f"\n• {c['name']}  —  AED {c['spend']:,.0f} | {c['clicks']} clicks | {c['ctr']}% CTR"
        # Actions
        actions = []
        if meta.get("ctr", 0) < 1:
            actions.append("⚡ CTR below 1% — refresh creative/audience")
        if meta.get("cost_per_result", 0) > 50:
            actions.append("⚡ High cost/result — test new ad formats")
        if meta.get("results", 0) == 0:
            actions.append("⚡ Zero results — check campaign delivery")
        # Always show standard actions
        standard_actions = []
        if meta.get("impressions", 0) > 0 and meta.get("results", 0) > 0:
            cpr = meta.get("cost_per_result", 0)
            if cpr > 30:
                standard_actions.append("⚡ Cost/result AED {:.0f} — test new audience or creative".format(cpr))
            if meta.get("ctr", 0) < 1.5:
                standard_actions.append("⚡ CTR {:.2f}% — refresh ad creative".format(meta.get("ctr", 0)))
            else:
                standard_actions.append("✅ CTR {:.2f}% — creative performing well".format(meta.get("ctr", 0)))
        all_actions = actions + standard_actions
        if all_actions:
            mkv_text += "\n\n*🔴 Actions:*\n" + "\n".join(all_actions[:3])
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
        # Standard RTO actions
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
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"_MKV Meta Ads • v5.6 • {mode_tag}_"}]},
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
    print("  MKV Daily Ads Snapshot v5.6")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Report date: {REPORT_DATE}")
    print(f"  Mode: {'🧪 TEST' if TEST_MODE else '🚀 LIVE'}  |  Channel: {SLACK_CHANNEL}")
    print("=" * 60)

    client = None
    g_camp = g_conv = g_search = g_auction = {}
    g_mtd = g_mtd_split = {}
    g_landing = {"pages": [], "rto_pages": []}
    g_geo = {"countries": [], "cities": []}
    g_weekly = {"rows": []}

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

    if not meta:
        print("  → Falling back to Gmail...")
        mail = imap_connect()
        if mail:
            meta_msg = fetch_meta_email(mail)
            if meta_msg:
                csv_text = extract_csv(meta_msg)
                meta = parse_meta(csv_text) if csv_text else {}
            mail.logout()

    yesterday = load_yesterday()
    save_today(g_camp, meta, meta_rto)

    kw_recs  = fetch_keyword_recommendations(client, g_search) if client and g_search else {"add": [], "remove": []}
    comp_kw  = fetch_competitor_keywords(client) if client and g_search else {"competitor_terms": [], "opportunity_terms": []}

    print("\n📤 Posting Google Ads report to Slack...")
    google_blocks = build_google_report(g_camp, g_mtd, g_mtd_split, g_conv, g_search, g_auction, g_landing, g_geo, g_device, yesterday, kw_recs, comp_kw)
    post_slack(google_blocks, "MKV Google Ads Report")
    print(f"\n  Google -> AED {g_camp.get('cost',0):.2f} | {g_camp.get('clicks',0)} clicks | {g_camp.get('conversions',0)} conv")
# Meta report posted separately by mkv_meta_local.py
  
    
    print(f"  Meta   -> AED {meta.get('spent',0):.2f} | {meta.get('results',0)} results")
    print("\n✅  Done!\n")


if __name__ == "__main__":
    main()
