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
META_ACCESS_TOKEN   = os.environ.get("META_ACCESS_TOKEN", "")
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


# Known geo target IDs for UAE and common locations
GEO_NAMES = {
    "100001": "United Arab Emirates",
    "1011245": "Dubai", "9041":    "Dubai",
    "1011246": "Abu Dhabi", "9045": "Abu Dhabi",
    "1011247": "Sharjah", "9046":  "Sharjah",
    "1011248": "Ajman",
    "1011249": "Ras Al Khaimah",
    "1011250": "Fujairah",
    "1011251": "Umm Al Quwain",
    "2784":    "United Arab Emirates",
    "20636":   "Dubai",
    "20637":   "Abu Dhabi",
    "20638":   "Sharjah",
    "1007741": "Saudi Arabia",
    "2682":    "United Kingdom",
    "2840":    "United States",
    "2356":    "India",
    "2276":    "Germany",
    "2250":    "France",
    "2643":    "United Kingdom",
    "2408":    "Japan",
    "2156":    "China",
    "2643":    "United Kingdom",
}


def resolve_geo_name(resource_name):
    """Resolve geo target constant ID to human-readable name."""
    geo_id = resource_name.split("/")[-1] if "/" in resource_name else resource_name
    return GEO_NAMES.get(geo_id, f"Location {geo_id}")


def fetch_geographic(client):
    """Fetch top countries AND cities by performance."""
    print("  → Fetching geographic data (country + city)...")
    ga_service = client.get_service("GoogleAdsService")
    geo_svc    = client.get_service("GeoTargetConstantService")

    def get_geo_name(resource_name):
        """Resolve geo target resource name to readable name."""
        geo_id = resource_name.split("/")[-1] if "/" in resource_name else resource_name
        # Try known IDs first
        if geo_id in GEO_NAMES:
            return GEO_NAMES[geo_id]
        # Try API
        try:
            gtc = geo_svc.get_geo_target_constant(resource_name=f"geoTargetConstants/{geo_id}")
            return gtc.canonical_name or gtc.name or f"Location {geo_id}"
        except:
            return GEO_NAMES.get(geo_id, f"Location {geo_id}")

    results = {"countries": [], "cities": []}

    # 1. Country level
    try:
        query_country = f"""
            SELECT
                geographic_view.resource_name,
                geographic_view.location_type,
                metrics.clicks,
                metrics.cost_micros,
                metrics.conversions,
                metrics.impressions
            FROM geographic_view
            WHERE segments.date = '{DATE_RANGE}'
              AND geographic_view.location_type = 'LOCATION_OF_PRESENCE'
              AND metrics.clicks > 0
            ORDER BY metrics.clicks DESC
            LIMIT 8
        """
        resp = ga_service.search(customer_id=CUSTOMER_ID, query=query_country)
        country_data = {}
        for row in resp:
            name  = get_geo_name(row.geographic_view.resource_name)
            clks  = int(row.metrics.clicks)
            cost  = round(row.metrics.cost_micros / 1_000_000, 2)
            conv  = round(row.metrics.conversions, 1)
            impr  = int(row.metrics.impressions)
            ctr   = round(clks / impr * 100, 1) if impr > 0 else 0
            # Aggregate by name (avoid duplicates)
            if name not in country_data:
                country_data[name] = {"clicks": 0, "cost": 0, "conv": 0, "impr": 0}
            country_data[name]["clicks"] += clks
            country_data[name]["cost"]   += cost
            country_data[name]["conv"]   += conv
            country_data[name]["impr"]   += impr

        for name, vals in sorted(country_data.items(), key=lambda x: x[1]["clicks"], reverse=True)[:6]:
            ctr_val  = round(vals["clicks"] / vals["impr"] * 100, 1) if vals["impr"] > 0 else 0
            conv_str = f" | {vals['conv']} conv" if vals["conv"] > 0 else ""
            results["countries"].append(
                f"• {name[:28]}  —  {vals['clicks']} clicks | AED {vals['cost']:,.0f} | CTR {ctr_val}%{conv_str}"
            )
        print(f"    ✅ Countries: {len(results['countries'])}")
    except Exception as e:
        print(f"    ⚠️  Country fetch failed: {e}")
        results["countries"] = ["• Country data unavailable"]

    # 2. City level — use user_location_view
    try:
        query_city = f"""
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
            LIMIT 8
        """
        resp2 = ga_service.search(customer_id=CUSTOMER_ID, query=query_city)
        city_data = {}
        for row in resp2:
            cid   = str(row.user_location_view.country_criterion_id)
            name  = GEO_NAMES.get(cid, None)
            if not name:
                try:
                    gtc  = geo_svc.get_geo_target_constant(resource_name=f"geoTargetConstants/{cid}")
                    name = gtc.canonical_name or gtc.name or f"Location {cid}"
                except:
                    name = f"Location {cid}"
            clks = int(row.metrics.clicks)
            cost = round(row.metrics.cost_micros / 1_000_000, 2)
            conv = round(row.metrics.conversions, 1)
            impr = int(row.metrics.impressions)
            if name not in city_data:
                city_data[name] = {"clicks": 0, "cost": 0, "conv": 0, "impr": 0}
            city_data[name]["clicks"] += clks
            city_data[name]["cost"]   += cost
            city_data[name]["conv"]   += conv
            city_data[name]["impr"]   += impr

        for name, vals in sorted(city_data.items(), key=lambda x: x[1]["clicks"], reverse=True)[:6]:
            ctr_val  = round(vals["clicks"] / vals["impr"] * 100, 1) if vals["impr"] > 0 else 0
            conv_str = f" | {vals['conv']} conv" if vals["conv"] > 0 else ""
            results["cities"].append(
                f"• {name[:28]}  —  {vals['clicks']} clicks | AED {vals['cost']:,.0f} | CTR {ctr_val}%{conv_str}"
            )
        print(f"    ✅ Cities: {len(results['cities'])}")
    except Exception as e:
        print(f"    ⚠️  City fetch failed: {e}")
        results["cities"] = ["• City data unavailable"]

    return results



def fetch_meta_placement(account_id):
    """Fetch Meta Ads performance by placement (Feed, Reels, Stories etc)."""
    token = META_ACCESS_TOKEN
    if not token:
        return {}
    print(f"  → Fetching Meta placement breakdown...")
    try:
        url = (
            f"https://graph.facebook.com/v20.0/act_{account_id}/insights"
            f"?fields=spend,impressions,clicks,publisher_platform,platform_position"
            f"&date_preset=yesterday"
            f"&breakdowns=publisher_platform%2Cplatform_position"
            f"&level=account"
            f"&access_token={token}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        placements = {}
        for row in data.get("data", []):
            platform = row.get("publisher_platform", "unknown").title()
            position = row.get("platform_position", "").replace("_", " ").title()
            key = f"{platform} — {position}"
            spent = round(float(row.get("spend", 0)), 2)
            clks  = int(row.get("clicks", 0))
            impr  = int(row.get("impressions", 0))
            if spent > 0:
                if key not in placements:
                    placements[key] = {"spent": 0, "clicks": 0, "impressions": 0}
                placements[key]["spent"]  += spent
                placements[key]["clicks"] += clks
                placements[key]["impressions"] += impr
        rows = []
        for name, vals in sorted(placements.items(), key=lambda x: x[1]["spent"], reverse=True)[:6]:
            ctr = round(vals["clicks"] / vals["impressions"] * 100, 2) if vals["impressions"] > 0 else 0
            rows.append(f"• {name[:35]}  —  AED {vals['spent']:,.2f} | {vals['clicks']} clicks | CTR {ctr}%")
        print(f"    ✅ Meta placements: {len(rows)} found")
        return {"placements": rows or ["• No placement data today"]}
    except Exception as e:
        print(f"    ❌ Meta placement failed: {e}")
        return {"placements": ["• Placement data unavailable"]}


def fetch_meta_age_gender(account_id):
    """Fetch Meta Ads performance by age and gender."""
    token = META_ACCESS_TOKEN
    if not token:
        return {}
    print(f"  → Fetching Meta age/gender breakdown...")
    try:
        url = (
            f"https://graph.facebook.com/v20.0/act_{account_id}/insights"
            f"?fields=spend,impressions,clicks,actions"
            f"&date_preset=yesterday"
            f"&breakdowns=age,gender"
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
            # Get results from actions
            results = 0
            for a in row.get("actions", []):
                if a["action_type"] == "onsite_conversion.total_messaging_connection":
                    results = max(results, int(a.get("value", 0)))
            if spent > 0:
                cpr = round(spent / results, 2) if results > 0 else 0
                segments.append({
                    "label": f"{gender} {age}",
                    "spent": spent, "clicks": clks,
                    "results": results, "cpr": cpr, "impressions": impr
                })
        # Sort by results then clicks
        segments.sort(key=lambda x: (x["results"], x["clicks"]), reverse=True)
        rows = []
        for s in segments[:6]:
            ctr = round(s["clicks"] / s["impressions"] * 100, 2) if s["impressions"] > 0 else 0
            res_str = f" | {s['results']} results @ AED {s['cpr']:,.0f}" if s["results"] > 0 else ""
            rows.append(f"• {s['label']:<15}  AED {s['spent']:,.2f} | {s['clicks']} clicks | CTR {ctr}%{res_str}")
        print(f"    ✅ Meta age/gender: {len(rows)} segments")
        return {"segments": rows or ["• No age/gender data today"]}
    except Exception as e:
        print(f"    ❌ Meta age/gender failed: {e}")
        return {"segments": ["• Age/gender data unavailable"]}


def fetch_weekly_summary(client):
    """Fetch last 7 days summary for weekly context."""
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
        # Weekly totals
        total_clicks = sum(v["clicks"] for v in daily.values())
        total_cost   = round(sum(v["cost"] for v in daily.values()), 2)
        total_conv   = round(sum(v["conv"] for v in daily.values()), 1)
        avg_ctr      = round(sum(v["clicks"] for v in daily.values()) /
                             max(sum(v["impr"] for v in daily.values()), 1) * 100, 2)
        # Best and worst day
        best_day  = max(daily.items(), key=lambda x: x[1]["conv"], default=(None, {}))
        worst_day = min(daily.items(), key=lambda x: x[1]["conv"], default=(None, {}))
        rows = [
            f"*7-Day Totals:* AED {total_cost:,.0f} spent | {total_clicks:,} clicks | {total_conv} conv | {avg_ctr}% CTR",
            f"*Best day:*  {best_day[0]} — {best_day[1].get('conv',0)} conv | AED {best_day[1].get('cost',0):,.0f}",
            f"*Weakest day:* {worst_day[0]} — {worst_day[1].get('conv',0)} conv | AED {worst_day[1].get('cost',0):,.0f}",
        ]
        # Day-by-day
        rows.append("\n*Daily Breakdown:*")
        for date, vals in sorted(daily.items()):
            day_name = datetime.strptime(date, "%Y-%m-%d").strftime("%a %d %b")
            rows.append(f"  {day_name}  —  AED {vals['cost']:,.0f} | {vals['clicks']} clicks | {vals['conv']} conv")
        print(f"    ✅ Weekly summary: {len(daily)} days")
        return {"rows": rows}
    except GoogleAdsException as ex:
        print(f"    ❌ Weekly summary failed: {ex.error.code().name}")
        return {"rows": ["• Weekly data unavailable"]}

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
            metrics.clicks,
            metrics.impressions
        FROM search_term_view
        WHERE segments.date = '{DATE_RANGE}'
          AND metrics.clicks > 0
        ORDER BY metrics.clicks DESC
        LIMIT 8
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


def fetch_keyword_recommendations(client, search_terms_data):
    """Analyse search terms to suggest add/remove keywords."""
    terms = search_terms_data.get("terms", [])
    if not terms or client is None:
        return {"add": [], "remove": []}

    # Fetch negative keywords already in account
    ga_service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT
            campaign_criterion.keyword.text,
            campaign_criterion.negative
        FROM campaign_criterion
        WHERE campaign_criterion.negative = TRUE
          AND campaign_criterion.type = 'KEYWORD'
        LIMIT 50
    """
    existing_negatives = set()
    try:
        response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
        for row in response:
            existing_negatives.add(row.campaign_criterion.keyword.text.lower())
    except:
        pass

    # Known irrelevant terms for luxury car rental
    irrelevant_patterns = [
        "rpm", "kia soul", "hyundai staria", "yaris", "corolla",
        "cheap", "budget", "economy", "used car", "second hand",
        "buy car", "purchase car", "car for sale"
    ]

    add_keywords = []
    remove_keywords = []

    for term_line in terms:
        # Extract term from "• term (N clicks)" format
        term = term_line.replace("•", "").strip()
        if "(" in term:
            term = term[:term.rfind("(")].strip()
        term_lower = term.lower()

        # Flag for removal if matches irrelevant patterns
        for pattern in irrelevant_patterns:
            if pattern in term_lower and term_lower not in existing_negatives:
                remove_keywords.append(f"• `{term}` — add as negative keyword")
                break
        else:
            # Suggest adding as exact match if high-intent
            high_intent = ["rental", "rent", "hire", "luxury", "supercar",
                          "lamborghini", "ferrari", "rolls", "bentley", "dubai"]
            if any(kw in term_lower for kw in high_intent):
                add_keywords.append(f"• `{term}` — add as exact match")

    return {
        "add"   : add_keywords[:5] or ["• No new keywords to add today"],
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

def build_blocks(g_camp, g_mtd, g_conv, g_search, g_auction, g_landing, g_geo, g_weekly, meta, meta_mtd, meta_rto, meta_rto_mtd, meta_placement, meta_age_gender, yesterday, kw_recs=None, comp_kw=None):
    score, grade, notes = score_report(g_camp, meta)
    if kw_recs is None:
        kw_recs = {"add": [], "remove": []}
    if comp_kw is None:
        comp_kw = {"competitor_terms": [], "opportunity_terms": []}

    d_cost  = delta(g_camp.get("cost", 0),        yesterday.get("g_cost", 0))
    d_clk   = delta(g_camp.get("clicks", 0),      yesterday.get("g_clicks", 0), prefix="")
    d_conv  = delta(g_camp.get("conversions", 0), yesterday.get("g_conv", 0),   prefix="")
    d_mcost = delta(meta.get("spent", 0),         yesterday.get("m_spent", 0))

    # ── Google Ads
    if g_camp:
        # MTD row
        mtd_line = ""
        if g_mtd:
            mtd_line = (
                f"\n*MTD:* AED {g_mtd.get('cost',0):,.0f} spent | "
                f"{g_mtd.get('clicks',0):,} clicks | "
                f"{g_mtd.get('conversions',0)} conv | "
                f"AED {g_mtd.get('cost_per_conv',0):,.2f}/conv"
            )
        g_text = (
            f"*Spend:* AED {g_camp.get('cost',0):,.2f}{d_cost}    "
            f"*Clicks:* {g_camp.get('clicks',0):,}{d_clk}\n"
            f"*Impressions:* {g_camp.get('impressions',0):,}    "
            f"*CTR:* {g_camp.get('ctr',0)}%\n"
            f"*Conversions:* {g_camp.get('conversions',0)}{d_conv}    "
            f"*Cost/Conv:* AED {g_camp.get('cost_per_conv',0):,.2f}"
            f"{mtd_line}"
        )
        if g_camp.get("campaigns"):
            lines = ["\n*Top Campaigns:*"]
            for c in g_camp["campaigns"]:
                lines.append(f"  • {c['name']}  —  AED {c['spend']:,.0f} | {c['clicks']:,} clicks | {c['ctr']}% CTR")
            g_text += "\n" + "\n".join(lines)

        # Key actions for Google Ads
        g_actions = []
        if g_camp.get("ctr", 0) < 3:
            g_actions.append("⚡ CTR below 3% — review ad copy & headlines")
        if g_camp.get("cost_per_conv", 0) > 100:
            g_actions.append("⚡ High cost/conv — pause underperforming keywords")
        for c in g_camp.get("campaigns", []):
            if c["spend"] > 50 and c["ctr"] < 2:
                g_actions.append(f"⚡ {c['name'][:25]} — low CTR, review ad copy")
        if g_actions:
            g_text += "\n\n*🔴 Actions:*\n" + "\n".join(g_actions[:3])
    else:
        g_text = "_No Google Ads data today_"

    conv_text   = "\n".join(g_conv.get("breakdown", []))      or "_No conversion data_"
    search_text = "\n".join(g_search.get("terms", []))         or "_No search term data_"
    comp_lines = g_auction.get("competitors", [])
    # Add key actions for impression share
    is_actions = []
    for line in comp_lines:
        if "Lost (budget): 9" in line or "Lost (budget): 8" in line:
            camp = line.split("\n")[0].replace("•","").strip()
            is_actions.append(f"⚡ {camp[:25]} — increase budget to capture lost IS")
        if "Lost (rank): 7" in line or "Lost (rank): 8" in line or "Lost (rank): 9" in line:
            camp = line.split("\n")[0].replace("•","").strip()
            is_actions.append(f"⚡ {camp[:25]} — improve Quality Score to win rank")
    if is_actions:
        comp_lines = comp_lines + ["\n*🔴 Actions:*"] + is_actions[:3]
    comp_text = "\n".join(comp_lines) or "_No impression share data_"
    land_text   = "\n".join(g_landing.get("pages", []))        or "_No landing page data_"
    rto_text    = "\n".join(g_landing.get("rto_pages", []))    or "_No Rent-to-Own traffic today_"

    # ── Keyword recommendations
    add_text    = "\n".join(kw_recs.get("add", []))    or "_No new keywords to add_"
    remove_text = "\n".join(kw_recs.get("remove", [])) or "_No irrelevant terms detected_"
    kw_text     = f"*➕ Add as keywords:*\n{add_text}\n\n*➖ Add as negatives:*\n{remove_text}"

    # ── Meta MKV Luxury
    if meta.get("spent", 0) > 0:
        mtd_meta_line = ""
        if meta_mtd:
            mtd_meta_line = (
                f"\n*MTD:* AED {meta_mtd.get('spent',0):,.0f} spent | "
                f"{meta_mtd.get('clicks',0):,} clicks | "
                f"{meta_mtd.get('results',0)} results"
            )
        meta_text = (
            f"*Spent:* AED {meta['spent']:,.2f}{d_mcost}    "
            f"*Results:* {meta['results']}    "
            f"*Cost/Result:* AED {meta['cost_per_result']:,.2f}\n"
            f"*Impressions:* {meta['impressions']:,}    "
            f"*Reach:* {meta['reach']:,}    "
            f"*CTR:* {meta['ctr']}%"
            f"{mtd_meta_line}"
        )
        if meta.get("campaigns"):
            lines = ["\n*Campaigns:*"]
            for c in meta["campaigns"]:
                lines.append(f"  • {c['name']}  —  AED {c['spend']:,.0f} | {c['clicks']:,} clicks | {c['ctr']}% CTR")
            meta_text += "\n" + "\n".join(lines)
        # Key actions for Meta
        meta_actions = []
        if meta.get("ctr", 0) < 1:
            meta_actions.append("⚡ CTR below 1% — refresh creative/audience")
        if meta.get("cost_per_result", 0) > 50:
            meta_actions.append("⚡ High cost/result — test new ad formats")
        if meta.get("results", 0) == 0:
            meta_actions.append("⚡ Zero results today — check campaign delivery")
        if meta_actions:
            meta_text += "\n\n*🔴 Actions:*\n" + "\n".join(meta_actions[:2])
    else:
        meta_text = "_No Meta Ads data today_"

    # ── Meta RTO
    if meta_rto and meta_rto.get("spent", 0) > 0:
        d_rto = delta(meta_rto.get("spent", 0), yesterday.get("m_rto_spent", 0))
        mtd_rto_line = ""
        if meta_rto_mtd:
            mtd_rto_line = (
                f"\n*MTD:* AED {meta_rto_mtd.get('spent',0):,.0f} spent | "
                f"{meta_rto_mtd.get('clicks',0):,} clicks | "
                f"{meta_rto_mtd.get('results',0)} results"
            )
        meta_rto_text = (
            f"*Spent:* AED {meta_rto['spent']:,.2f}{d_rto}    "
            f"*Results:* {meta_rto['results']}    "
            f"*Cost/Result:* AED {meta_rto['cost_per_result']:,.2f}\n"
            f"*Impressions:* {meta_rto['impressions']:,}    "
            f"*Reach:* {meta_rto['reach']:,}    "
            f"*CTR:* {meta_rto['ctr']}%"
            f"{mtd_rto_line}"
        )
        # Key actions for RTO
        rto_actions = []
        if meta_rto.get("ctr", 0) < 1:
            rto_actions.append("⚡ CTR below 1% — refresh RTO creative")
        if meta_rto.get("cost_per_result", 0) > 100:
            rto_actions.append("⚡ High cost/result — review RTO targeting")
        if rto_actions:
            meta_rto_text += "\n\n*🔴 Actions:*\n" + "\n".join(rto_actions[:2])
    else:
        meta_rto_text = "_No Lease to Own Meta data today_"

    # ── Competitor keyword analysis
    comp_terms_text = "\n".join(comp_kw.get("competitor_terms", [])) or "_No competitor searches detected_"
    opp_terms_text  = "\n".join(comp_kw.get("opportunity_terms", [])) or "_No missed opportunities detected_"
    comp_kw_actions = []
    if comp_kw.get("competitor_terms"):
        comp_kw_actions.append("⚡ Create competitor comparison landing pages")
        comp_kw_actions.append("⚡ Add competitor names as exact match keywords")
    if comp_kw.get("opportunity_terms"):
        comp_kw_actions.append("⚡ Add high-intent 0-conv terms to exact match campaigns")
    comp_kw_action_text = "\n".join(comp_kw_actions) if comp_kw_actions else ""
    comp_kw_text = (
        f"*🎯 Competitor searches hitting your ads:*\n{comp_terms_text}\n\n"
        f"*💡 High-intent terms with 0 conversions:*\n{opp_terms_text}"
        + (f"\n\n*🔴 Actions:*\n{comp_kw_action_text}" if comp_kw_action_text else "")
    )

    # ── Geographic
    countries  = g_geo.get("countries", [])
    cities     = g_geo.get("cities", [])
    geo_text   = ""
    if countries:
        geo_text += "*🌐 By Country:*\n" + "\n".join(countries)
    if cities:
        geo_text += "\n\n*🏙️ By City/Region:*\n" + "\n".join(cities)
    if not geo_text:
        geo_text = "_No geographic data today_"
    # Geo actions
    geo_actions = []
    all_locs = countries + cities
    if any("saudi" in l.lower() for l in all_locs):
        geo_actions.append("⚡ Saudi Arabia traffic detected — create Arabic campaign targeting KSA")
    if any("india" in l.lower() for l in all_locs):
        geo_actions.append("⚡ India traffic detected — consider excluding or creating separate campaign")
    if any("abu dhabi" in l.lower() for l in all_locs):
        geo_actions.append("⚡ Abu Dhabi showing traffic — consider dedicated Abu Dhabi campaign")
    if any("uk" in l.lower() or "united kingdom" in l.lower() for l in all_locs):
        geo_actions.append("⚡ UK visitors detected — target UK tourists planning Dubai trips")
    if len(all_locs) > 4:
        geo_actions.append("⚡ Multiple markets active — review location bid adjustments")
    if geo_actions:
        geo_text += "\n\n*🔴 Actions:*\n" + "\n".join(geo_actions[:3])

    # ── Weekly Summary (Google + Meta combined)
    weekly_rows = g_weekly.get("rows", []).copy()
    # Add Meta weekly context
    if meta_mtd.get("spent", 0) > 0:
        weekly_rows.append(f"\n*Meta MKV MTD:* AED {meta_mtd.get('spent',0):,.0f} | {meta_mtd.get('results',0)} results")
    if meta_rto_mtd and meta_rto_mtd.get("spent", 0) > 0:
        weekly_rows.append(f"*Meta RTO MTD:* AED {meta_rto_mtd.get('spent',0):,.0f} | {meta_rto_mtd.get('results',0)} results")
    # Combined MTD spend
    total_mtd = (
        g_mtd.get("cost", 0) +
        meta_mtd.get("spent", 0) +
        (meta_rto_mtd.get("spent", 0) if meta_rto_mtd else 0)
    )
    if total_mtd > 0:
        weekly_rows.insert(0, f"*💰 Total MTD Spend (Google + Meta):* AED {total_mtd:,.0f}")
    weekly_text = "\n".join(weekly_rows) if weekly_rows else "_No weekly data_"

    # ── Meta Placement
    placement_text = "\n".join(meta_placement.get("placements", [])) or "_No placement data today_"
    placement_actions = []
    placements = meta_placement.get("placements", [])
    if any("reels" in p.lower() for p in placements):
        placement_actions.append("⚡ Reels active — ensure vertical video creative is optimised")
    if any("instagram" in p.lower() for p in placements):
        placement_actions.append("⚡ Instagram driving traffic — check story/reel creative quality")
    if placement_actions:
        placement_text += "\n\n*🔴 Actions:*\n" + "\n".join(placement_actions[:2])

    # ── Meta Age/Gender
    age_gender_text = "\n".join(meta_age_gender.get("segments", [])) or "_No demographic data today_"
    demo_actions = []
    segments = meta_age_gender.get("segments", [])
    if segments:
        demo_actions.append("⚡ Focus budget on top-converting age/gender segments")
        if any("female" in s.lower() for s in segments[:2]):
            demo_actions.append("⚡ Female segment converting — create female-targeted creatives")
    if demo_actions:
        age_gender_text += "\n\n*🔴 Actions:*\n" + "\n".join(demo_actions[:2])

    score_text = f"*Score: {score}/100 — {grade}*\n" + "\n".join(notes)
    mode_tag   = "🧪 TEST MODE" if TEST_MODE else "🚀 LIVE"

    return [
        {"type": "header",
         "text": {"type": "plain_text", "text": f"📊 MKV Daily Ads Snapshot — {REPORT_DATE}", "emoji": True}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*🔵 Google Ads — MKV Luxury*\n{g_text}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*🎯 Conversions by Campaign*\n{conv_text}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*🔍 Top Search Terms*\n{search_text}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*🔑 Keyword Recommendations*\n{kw_text}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*📊 Search Impression Share*\n{comp_text}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*📄 MKV Luxury — Top Landing Pages*\n{land_text}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*🚗 Rent-to-Own (renttoowncars.ae)*\n{rto_text}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*🌍 Geographic Performance*\n{geo_text}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*🟦 Meta Ads — MKV Luxury*\n{meta_text}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*🟪 Meta Ads — Lease to Own*\n{meta_rto_text}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*📺 Meta Placement Breakdown*\n{placement_text}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*👥 Meta Age & Gender*\n{age_gender_text}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*🕵️ Competitor & Opportunity Keywords*\n{comp_kw_text}"}},
        {"type": "divider"},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*📅 Weekly Summary (Last 7 Days)*\n{weekly_text}"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*🏆 Performance Score*\n{score_text}"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
                       "text": f"_MKV Luxury Car Rental  •  Google Ads API + Meta API v4.1  •  {mode_tag}_"}]},
    ]


def post_slack(blocks, fallback="MKV Daily Ads Snapshot"):
    client = WebClient(token=SLACK_TOKEN)
    try:
        client.chat_postMessage(channel=SLACK_CHANNEL, blocks=blocks, text=fallback, unfurl_links=False, unfurl_media=False)
        print(f"  ✅  Posted to Slack: {SLACK_CHANNEL}")
    except SlackApiError as e:
        print(f"  ❌  Slack error: {e.response['error']}")

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  MKV Daily Ads Snapshot v4.1 — Google Ads API")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Report date: {REPORT_DATE}")
    print(f"  Mode: {'🧪 TEST' if TEST_MODE else '🚀 LIVE'}  |  Channel: {SLACK_CHANNEL}")
    print("=" * 60)

    print("\n📊 Fetching Google Ads data via API...")
    client = None
    try:
        client    = get_google_ads_client()
        g_camp    = fetch_campaign_performance(client)
        g_mtd     = fetch_mtd_google(client)
        g_conv    = fetch_conversions(client)
        g_search  = fetch_search_terms(client)
        g_auction = fetch_auction_insights(client)
        g_landing = fetch_landing_pages(client)
        g_geo     = fetch_geographic(client)
        g_weekly  = fetch_weekly_summary(client)
    except Exception as e:
        print(f"  ❌  Google Ads API error: {e}")
        g_camp = g_conv = g_search = g_auction = {}
        g_mtd  = {}
        g_landing = {"pages": [], "rto_pages": []}
        g_geo  = {"locations": []}
        g_weekly = {"rows": []}

    print("\n📥 Fetching Meta Ads via API...")
    meta          = fetch_meta_api(META_AD_ACCOUNT_ID, "MKV Luxury")
    meta_rto      = fetch_meta_api(META_RTO_ACCOUNT_ID, "MKV Lease to Own") if META_RTO_ACCOUNT_ID else {}
    meta_mtd      = fetch_meta_mtd(META_AD_ACCOUNT_ID, "MKV Luxury")
    meta_rto_mtd  = fetch_meta_mtd(META_RTO_ACCOUNT_ID) if META_RTO_ACCOUNT_ID else {}
    meta_placement = fetch_meta_placement(META_AD_ACCOUNT_ID) if META_AD_ACCOUNT_ID else {}
    meta_age_gender = fetch_meta_age_gender(META_AD_ACCOUNT_ID) if META_AD_ACCOUNT_ID else {}
    if not meta:
        # Fallback to Gmail if API fails
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

    print(f"\n  Google Ads → AED {g_camp.get('cost',0):.2f} | {g_camp.get('clicks',0)} clicks | {g_camp.get('conversions',0)} conv")
    print(f"  Meta Ads   → AED {meta.get('spent',0):.2f} | {meta.get('results',0)} results")

    # Keyword recommendations & competitor analysis
    kw_recs = fetch_keyword_recommendations(client, g_search) if g_search else {"add": [], "remove": []}
    comp_kw  = fetch_competitor_keywords(client) if g_search else {"competitor_terms": [], "opportunity_terms": []}

    print("\n📤 Posting to Slack...")
    blocks = build_blocks(g_camp, g_mtd, g_conv, g_search, g_auction, g_landing, g_geo, g_weekly, meta, meta_mtd, meta_rto, meta_rto_mtd, meta_placement, meta_age_gender, yesterday, kw_recs, comp_kw)
    post_slack(blocks)
    print("\n✅  Done!\n")


if __name__ == "__main__":
    main()
