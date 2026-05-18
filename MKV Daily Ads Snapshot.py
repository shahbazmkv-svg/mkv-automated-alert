"""
MKV Car Rental — Daily Ads Snapshot
=====================================
Connects to Gmail, downloads Google Ads CSV reports,
parses the data, and posts a unified Daily Snapshot to Slack.

Reports expected in Gmail (subject lines):
  - MKV Google Ads - Campaign
  - MKV Google Ads - Conversions
  - MKV Google Ads - Search Terms
  - MKV Google Ads - Auction
  - MKV Google Ads - Landing Pages

Later: MKV Meta Ads - Daily (plug-in ready)

Requirements:
  pip install google-auth google-auth-oauthlib google-auth-httplib2
              google-api-python-client slack-sdk pandas

Setup:
  1. Place credentials.json (Gmail OAuth) in this folder
  2. Place slack_token.txt with your Slack Bot Token in this folder
  3. Run once manually to authorize Gmail
  4. Schedule via Windows Task Scheduler to run daily at 9:30 AM
"""

import os
import io
import gzip
import base64
import json
import re
import pandas as pd
from datetime import datetime, timedelta
from email import message_from_bytes

# Gmail API
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Slack SDK
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ── CONFIG ────────────────────────────────────────────────────────────────────

GMAIL_SCOPES      = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE  = "credentials.json"
TOKEN_FILE        = "token.json"
SLACK_TOKEN_FILE  = "slack_token.txt"

# Change to SLACK_CHANNEL_PROD when ready for live
SLACK_CHANNEL_TEST = "C0B0TGBDCDU"   # mkv-test-automation
SLACK_CHANNEL_PROD = "C0AASQKLY59"   # mkv-marketing-team
SLACK_CHANNEL      = SLACK_CHANNEL_TEST

# Gmail subject lines (must match exactly what was scheduled)
REPORT_SUBJECTS = {
    "campaign":    "MKV Google Ads - Campaign",
    "conversions": "MKV Google Ads - Conversions",
    "search_terms":"MKV Google Ads - Search Terms",
    "auction":     "MKV Google Ads - Auction",
    "landing":     "MKV Google Ads - Landing Pages",
    # "meta":       "MKV Meta Ads - Daily",  # ← Uncomment when Meta is ready
}

# ── GMAIL AUTH ────────────────────────────────────────────────────────────────

def get_gmail_service():
    """Authenticate and return Gmail API service."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)

# ── GMAIL: FETCH CSV ATTACHMENT ───────────────────────────────────────────────

def fetch_csv_from_gmail(service, subject_line):
    """
    Search Gmail for today's email with the given subject,
    download the CSV attachment, and return a DataFrame.
    """
    today = datetime.now().strftime("%Y/%m/%d")
    query = f'subject:"{subject_line}" after:{today}'

    results = service.users().messages().list(userId="me", q=query).execute()
    messages = results.get("messages", [])

    if not messages:
        print(f"  ⚠️  No email found for: {subject_line}")
        return None

    # Get the most recent message
    msg = service.users().messages().get(
        userId="me", id=messages[0]["id"], format="raw"
    ).execute()

    raw = base64.urlsafe_b64decode(msg["raw"].encode("ASCII"))
    email_msg = message_from_bytes(raw)

    for part in email_msg.walk():
        filename = part.get_filename()
        if filename and (filename.endswith(".csv") or filename.endswith(".csv.gz")):
            payload = part.get_payload(decode=True)
            if filename.endswith(".gz"):
                payload = gzip.decompress(payload)
            try:
                df = pd.read_csv(io.StringIO(payload.decode("utf-8")), skiprows=2)
                # Drop empty rows at end (Google Ads adds footer rows)
                df = df.dropna(how="all")
                df = df[~df.iloc[:, 0].astype(str).str.contains("Total|Report|©", na=False)]
                print(f"  ✅  Loaded: {subject_line} ({len(df)} rows)")
                return df
            except Exception as e:
                print(f"  ❌  Parse error for {subject_line}: {e}")
                return None

    print(f"  ⚠️  No CSV attachment in: {subject_line}")
    return None

# ── PARSERS ───────────────────────────────────────────────────────────────────

def parse_campaign(df):
    if df is None or df.empty:
        return {}
    try:
        # Normalize column names
        df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
        num_cols = ["impressions", "clicks", "cost", "conversions"]
        for col in num_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(
                    df[col].astype(str).str.replace(",", "").str.replace("AED", "").str.strip(),
                    errors="coerce"
                ).fillna(0)

        return {
            "total_impressions": int(df["impressions"].sum()) if "impressions" in df.columns else 0,
            "total_clicks":      int(df["clicks"].sum())      if "clicks"      in df.columns else 0,
            "total_cost_aed":    round(df["cost"].sum(), 2)   if "cost"        in df.columns else 0,
            "total_conversions": int(df["conversions"].sum()) if "conversions" in df.columns else 0,
            "ctr": round((df["clicks"].sum() / df["impressions"].sum() * 100), 2)
                   if "impressions" in df.columns and df["impressions"].sum() > 0 else 0,
            "cost_per_conversion": round(df["cost"].sum() / df["conversions"].sum(), 2)
                   if "conversions" in df.columns and df["conversions"].sum() > 0 else 0,
            "campaigns": len(df),
        }
    except Exception as e:
        print(f"  ❌  Campaign parse error: {e}")
        return {}


def parse_conversions(df):
    if df is None or df.empty:
        return {}
    try:
        df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
        conversion_col = next((c for c in df.columns if "conversion" in c and "action" not in c), None)
        action_col     = next((c for c in df.columns if "action" in c), None)
        rows = []
        if action_col and conversion_col:
            for _, row in df.iterrows():
                rows.append(f"• {row[action_col]}: {row[conversion_col]}")
        return {"conversion_breakdown": rows}
    except Exception as e:
        print(f"  ❌  Conversions parse error: {e}")
        return {}


def parse_search_terms(df, top_n=5):
    if df is None or df.empty:
        return {}
    try:
        df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
        term_col   = next((c for c in df.columns if "search_term" in c or "query" in c), None)
        clicks_col = next((c for c in df.columns if "click" in c), None)
        if term_col and clicks_col:
            df[clicks_col] = pd.to_numeric(
                df[clicks_col].astype(str).str.replace(",", ""), errors="coerce"
            ).fillna(0)
            top = df.nlargest(top_n, clicks_col)[[term_col, clicks_col]]
            terms = [f"• {row[term_col]} ({int(row[clicks_col])} clicks)" for _, row in top.iterrows()]
            return {"top_search_terms": terms}
        return {}
    except Exception as e:
        print(f"  ❌  Search terms parse error: {e}")
        return {}


def parse_auction(df, top_n=5):
    if df is None or df.empty:
        return {}
    try:
        df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
        domain_col  = next((c for c in df.columns if "domain" in c or "competitor" in c or "display" in c), None)
        overlap_col = next((c for c in df.columns if "overlap" in c or "impression_share" in c), None)
        if domain_col:
            competitors = []
            for _, row in df.head(top_n).iterrows():
                overlap = f" — overlap: {row[overlap_col]}" if overlap_col else ""
                competitors.append(f"• {row[domain_col]}{overlap}")
            return {"top_competitors": competitors}
        return {}
    except Exception as e:
        print(f"  ❌  Auction parse error: {e}")
        return {}


def parse_landing_pages(df, top_n=3):
    if df is None or df.empty:
        return {}
    try:
        df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
        url_col    = next((c for c in df.columns if "landing" in c or "url" in c or "page" in c), None)
        clicks_col = next((c for c in df.columns if "click" in c), None)
        ctr_col    = next((c for c in df.columns if "ctr" in c), None)
        if url_col and clicks_col:
            df[clicks_col] = pd.to_numeric(
                df[clicks_col].astype(str).str.replace(",", ""), errors="coerce"
            ).fillna(0)
            top  = df.nlargest(top_n, clicks_col)
            pages = []
            for _, row in top.iterrows():
                ctr  = f" | CTR: {row[ctr_col]}" if ctr_col else ""
                pages.append(f"• {row[url_col]} — {int(row[clicks_col])} clicks{ctr}")
            return {"top_landing_pages": pages}
        return {}
    except Exception as e:
        print(f"  ❌  Landing pages parse error: {e}")
        return {}

# ── SLACK MESSAGE BUILDER ─────────────────────────────────────────────────────

def build_slack_message(data):
    """Build the unified Daily Snapshot Slack message."""
    today     = datetime.now().strftime("%A, %d %B %Y")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%d %b %Y")

    c  = data.get("campaign",    {})
    cv = data.get("conversions", {})
    st = data.get("search_terms",{})
    au = data.get("auction",     {})
    lp = data.get("landing",     {})

    # ── Performance score (simple weighted calc) ──
    score = 0
    if c.get("ctr", 0) >= 5:            score += 25
    elif c.get("ctr", 0) >= 3:          score += 15
    if c.get("total_conversions", 0) > 0: score += 30
    if c.get("cost_per_conversion", 0) < 50: score += 25
    if c.get("total_clicks", 0) > 100:  score += 20
    grade = "🟢 Strong" if score >= 70 else "🟡 Moderate" if score >= 40 else "🔴 Needs Attention"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📊 MKV Daily Ads Snapshot — {yesterday}"}
        },
        {"type": "divider"},

        # ── Google Ads Summary ──
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*🔵 Google Ads Performance*\n"
                    f"*Impressions:* {c.get('total_impressions', 'N/A'):,}   "
                    f"*Clicks:* {c.get('total_clicks', 'N/A'):,}   "
                    f"*CTR:* {c.get('ctr', 'N/A')}%\n"
                    f"*Spend:* AED {c.get('total_cost_aed', 'N/A'):,}   "
                    f"*Conversions:* {c.get('total_conversions', 'N/A')}   "
                    f"*Cost/Conv:* AED {c.get('cost_per_conversion', 'N/A')}\n"
                    f"*Active Campaigns:* {c.get('campaigns', 'N/A')}"
                )
            }
        },
        {"type": "divider"},

        # ── Conversions Breakdown ──
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*🎯 Conversions Breakdown*\n" +
                    ("\n".join(cv.get("conversion_breakdown", [])) or "_No conversion data_")
                )
            }
        },
        {"type": "divider"},

        # ── Top Search Terms ──
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*🔍 Top Search Terms*\n" +
                    ("\n".join(st.get("top_search_terms", [])) or "_No search term data_")
                )
            }
        },
        {"type": "divider"},

        # ── Competitor Insights ──
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*⚔️ Competitor Auction Insights*\n" +
                    ("\n".join(au.get("top_competitors", [])) or "_No competitor data_")
                )
            }
        },
        {"type": "divider"},

        # ── Landing Pages ──
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*📄 Top Landing Pages*\n" +
                    ("\n".join(lp.get("top_landing_pages", [])) or "_No landing page data_")
                )
            }
        },
        {"type": "divider"},

        # ── Performance Score ──
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*🏆 Overall Performance Score: {score}/100 — {grade}*"
            }
        },

        # ── Footer ──
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"_MKV Car Rental | Auto-generated snapshot | {today} | Google Ads ✅  Meta Ads 🔜_"
                }
            ]
        }
    ]

    return blocks

# ── SLACK SENDER ──────────────────────────────────────────────────────────────

def post_to_slack(blocks, channel):
    """Post the Daily Snapshot to Slack."""
    with open(SLACK_TOKEN_FILE) as f:
        token = f.read().strip()

    client = WebClient(token=token)
    try:
        response = client.chat_postMessage(
            channel=channel,
            blocks=blocks,
            text="MKV Daily Ads Snapshot"
        )
        print(f"\n  ✅  Posted to Slack channel: {channel}")
        return True
    except SlackApiError as e:
        print(f"\n  ❌  Slack error: {e.response['error']}")
        return False

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  MKV Car Rental — Daily Ads Snapshot")
    print(f"  Running: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 55)

    # 1. Connect to Gmail
    print("\n📧 Connecting to Gmail...")
    service = get_gmail_service()
    print("  ✅  Gmail connected")

    # 2. Fetch & parse all reports
    print("\n📥 Fetching Google Ads reports...")
    data = {}
    df_campaign  = fetch_csv_from_gmail(service, REPORT_SUBJECTS["campaign"])
    df_conv      = fetch_csv_from_gmail(service, REPORT_SUBJECTS["conversions"])
    df_search    = fetch_csv_from_gmail(service, REPORT_SUBJECTS["search_terms"])
    df_auction   = fetch_csv_from_gmail(service, REPORT_SUBJECTS["auction"])
    df_landing   = fetch_csv_from_gmail(service, REPORT_SUBJECTS["landing"])

    print("\n🔄 Parsing data...")
    data["campaign"]     = parse_campaign(df_campaign)
    data["conversions"]  = parse_conversions(df_conv)
    data["search_terms"] = parse_search_terms(df_search)
    data["auction"]      = parse_auction(df_auction)
    data["landing"]      = parse_landing_pages(df_landing)

    # 3. Build Slack message
    print("\n📝 Building snapshot...")
    blocks = build_slack_message(data)

    # 4. Post to Slack
    print("\n📤 Posting to Slack...")
    post_to_slack(blocks, SLACK_CHANNEL)

    print("\n✅  Done!\n")

if __name__ == "__main__":
    main()
