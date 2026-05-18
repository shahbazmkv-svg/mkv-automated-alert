"""
MKV Daily Ads Snapshot
======================
Reads Google Ads CSV reports from Gmail via IMAP,
parses data, and posts unified Daily Snapshot to Slack.

Runs on GitHub Actions — no local setup needed.
Triggered daily at 9:30 AM Dubai time via cron-job.org.

Gmail subjects expected:
  - MKV Google Ads - Campaign
  - MKV Google Ads - Conversions
  - MKV Google Ads - Search Terms
  - MKV Google Ads - Auction
  - MKV Google Ads - Landing Pages
  - MKV Meta Ads - Daily (contact@mkvluxury.com)

Secrets required in GitHub:
  SLACK_BOT_TOKEN
  GMAIL_ADDRESS       (shahbazmkv@gmail.com)
  GMAIL_APP_PASSWORD  (Gmail App Password - 16 chars)
  META_GMAIL_ADDRESS  (contact@mkvluxury.com)
  META_GMAIL_APP_PASSWORD
"""

import imaplib
import email
import io
import gzip
import os
import json
import pandas as pd
from datetime import datetime, timedelta
from email import message_from_bytes
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ── CONFIG ────────────────────────────────────────────────────────────────────

SLACK_TOKEN        = os.environ["SLACK_BOT_TOKEN"]
GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS", "shahbazmkv@gmail.com")
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
META_GMAIL_ADDRESS      = os.environ.get("META_GMAIL_ADDRESS", "contact@mkvluxury.com")
META_GMAIL_APP_PASSWORD = os.environ.get("META_GMAIL_APP_PASSWORD", "")

TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"

SLACK_CHANNEL_TEST = "C0B0TGBDCDU"   # mkv-test-automation
SLACK_CHANNEL_PROD = "C0AASQKLY59"   # mkv-marketing-team
SLACK_CHANNEL      = SLACK_CHANNEL_TEST if TEST_MODE else SLACK_CHANNEL_PROD

REPORT_SUBJECTS = {
    "campaign":    "MKV Google Ads - Campaign",
    "conversions": "MKV Google Ads - Conversions",
    "search_terms":"MKV Google Ads - Search Terms",
    "auction":     "MKV Google Ads - Auction",
    "landing":     "MKV Google Ads - Landing Pages",
}
META_SUBJECT = "MKV Meta Ads - Daily"

# ── IMAP: FETCH CSV FROM GMAIL ────────────────────────────────────────────────

def fetch_csv_imap(gmail_address, app_password, subject_line):
    """Connect to Gmail via IMAP, find today's email, return DataFrame."""
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(gmail_address, app_password)
        mail.select("inbox")

        # Search for email with subject from today
        today = datetime.now().strftime("%d-%b-%Y")
        search = f'(SUBJECT "{subject_line}" SINCE {today})'
        _, msg_ids = mail.search(None, search)

        if not msg_ids[0]:
            # Try yesterday as fallback
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%d-%b-%Y")
            search = f'(SUBJECT "{subject_line}" SINCE {yesterday})'
            _, msg_ids = mail.search(None, search)

        if not msg_ids[0]:
            print(f"  ⚠️  No email found: {subject_line}")
            mail.logout()
            return None

        # Get latest message
        latest_id = msg_ids[0].split()[-1]
        _, msg_data = mail.fetch(latest_id, "(RFC822)")
        mail.logout()

        raw_email = msg_data[0][1]
        email_msg = message_from_bytes(raw_email)

        for part in email_msg.walk():
            filename = part.get_filename()
            if filename and (filename.endswith(".csv") or
                           filename.endswith(".csv.gz") or
                           filename.endswith(".xlsx")):
                payload = part.get_payload(decode=True)
                if filename.endswith(".gz"):
                    payload = gzip.decompress(payload)
                try:
                    if filename.endswith(".xlsx"):
                        df = pd.read_excel(io.BytesIO(payload))
                    else:
                        # Try different encodings
                        for enc in ["utf-8", "utf-8-sig", "latin-1"]:
                            try:
                                text = payload.decode(enc)
                                break
                            except:
                                continue
                        # Skip Google Ads header rows
                        lines = text.split("\n")
                        skip = 0
                        for i, line in enumerate(lines):
                            if any(col in line for col in
                                   ["Campaign","Impressions","Reach","Ad set"]):
                                skip = i
                                break
                        df = pd.read_csv(io.StringIO("\n".join(lines[skip:])))

                    # Drop footer/empty rows
                    df = df.dropna(how="all")
                    df = df[~df.iloc[:,0].astype(str).str.contains(
                        "Total|Report|©|^$", na=False)]
                    print(f"  ✅  Loaded: {subject_line} ({len(df)} rows)")
                    return df
                except Exception as e:
                    print(f"  ❌  Parse error {subject_line}: {e}")
                    return None

        print(f"  ⚠️  No CSV attachment in: {subject_line}")
        return None

    except Exception as e:
        print(f"  ❌  IMAP error for {subject_line}: {e}")
        return None

# ── PARSERS ───────────────────────────────────────────────────────────────────

def normalize(df):
    """Normalize column names."""
    df.columns = [c.strip().lower().replace(" ", "_").replace("/","_")
                  for c in df.columns]
    return df

def to_num(series):
    return pd.to_numeric(
        series.astype(str).str.replace(",","").str.replace("AED","")
              .str.replace("د.إ","").str.strip(),
        errors="coerce"
    ).fillna(0)

def parse_campaign(df):
    if df is None or df.empty: return {}
    try:
        df = normalize(df)
        imp_col  = next((c for c in df.columns if "impress" in c), None)
        clk_col  = next((c for c in df.columns if "click" in c and "through" not in c), None)
        cst_col  = next((c for c in df.columns if "cost" in c and "conv" not in c and "per" not in c), None)
        cnv_col  = next((c for c in df.columns if "conv" in c and "cost" not in c and "rate" not in c), None)

        impr = to_num(df[imp_col]).sum() if imp_col else 0
        clks = to_num(df[clk_col]).sum() if clk_col else 0
        cost = to_num(df[cst_col]).sum() if cst_col else 0
        conv = to_num(df[cnv_col]).sum() if cnv_col else 0

        return {
            "impressions":        int(impr),
            "clicks":             int(clks),
            "cost":               round(cost, 2),
            "conversions":        int(conv),
            "ctr":                round(clks/impr*100, 2) if impr > 0 else 0,
            "cost_per_conv":      round(cost/conv, 2) if conv > 0 else 0,
            "campaigns":          len(df),
        }
    except Exception as e:
        print(f"  ❌  Campaign parse: {e}")
        return {}

def parse_conversions(df):
    if df is None or df.empty: return {}
    try:
        df = normalize(df)
        action_col = next((c for c in df.columns if "action" in c or "type" in c), None)
        value_col  = next((c for c in df.columns if "value" in c or "conv" in c
                           and "action" not in c), None)
        rows = []
        if action_col:
            for _, row in df.iterrows():
                val = f": {row[value_col]}" if value_col else ""
                rows.append(f"• {row[action_col]}{val}")
        return {"breakdown": rows[:5]}
    except Exception as e:
        print(f"  ❌  Conversions parse: {e}")
        return {}

def parse_search_terms(df, top_n=5):
    if df is None or df.empty: return {}
    try:
        df = normalize(df)
        term_col = next((c for c in df.columns if "search" in c or "query" in c or "term" in c), None)
        clk_col  = next((c for c in df.columns if "click" in c and "through" not in c), None)
        if term_col and clk_col:
            df[clk_col] = to_num(df[clk_col])
            top = df.nlargest(top_n, clk_col)
            return {"terms": [f"• {r[term_col]} ({int(r[clk_col])} clicks)"
                               for _, r in top.iterrows()]}
        return {}
    except Exception as e:
        print(f"  ❌  Search terms parse: {e}")
        return {}

def parse_auction(df, top_n=5):
    if df is None or df.empty: return {}
    try:
        df = normalize(df)
        domain_col  = next((c for c in df.columns if "domain" in c or "display" in c
                            or "competitor" in c or "name" in c), None)
        overlap_col = next((c for c in df.columns if "overlap" in c or "share" in c), None)
        rows = []
        if domain_col:
            for _, row in df.head(top_n).iterrows():
                overlap = f" — {row[overlap_col]}" if overlap_col else ""
                rows.append(f"• {row[domain_col]}{overlap}")
        return {"competitors": rows}
    except Exception as e:
        print(f"  ❌  Auction parse: {e}")
        return {}

def parse_landing(df, top_n=3):
    if df is None or df.empty: return {}
    try:
        df = normalize(df)
        url_col = next((c for c in df.columns if "url" in c or "page" in c
                        or "landing" in c), None)
        clk_col = next((c for c in df.columns if "click" in c and "through" not in c), None)
        ctr_col = next((c for c in df.columns if "ctr" in c), None)
        if url_col and clk_col:
            df[clk_col] = to_num(df[clk_col])
            top = df.nlargest(top_n, clk_col)
            pages = []
            for _, r in top.iterrows():
                ctr = f" | CTR: {r[ctr_col]}" if ctr_col else ""
                pages.append(f"• {r[url_col]} — {int(r[clk_col])} clicks{ctr}")
            return {"pages": pages}
        return {}
    except Exception as e:
        print(f"  ❌  Landing parse: {e}")
        return {}

def parse_meta(df):
    if df is None or df.empty: return {}
    try:
        df = normalize(df)
        imp_col  = next((c for c in df.columns if "impress" in c), None)
        reach_col= next((c for c in df.columns if "reach" in c), None)
        clk_col  = next((c for c in df.columns if "click" in c and "through" not in c), None)
        cst_col  = next((c for c in df.columns if "spent" in c or "spend" in c or "amount" in c), None)
        res_col  = next((c for c in df.columns if "result" in c and "rate" not in c
                         and "cost" not in c), None)
        cpr_col  = next((c for c in df.columns if "cost_per" in c or "cost per" in c), None)

        return {
            "impressions": int(to_num(df[imp_col]).sum())   if imp_col  else 0,
            "reach":       int(to_num(df[reach_col]).sum()) if reach_col else 0,
            "clicks":      int(to_num(df[clk_col]).sum())   if clk_col  else 0,
            "spent":       round(to_num(df[cst_col]).sum(), 2) if cst_col else 0,
            "results":     int(to_num(df[res_col]).sum())   if res_col  else 0,
            "cost_per_result": round(to_num(df[cpr_col]).mean(), 2) if cpr_col else 0,
        }
    except Exception as e:
        print(f"  ❌  Meta parse: {e}")
        return {}

# ── SLACK MESSAGE ─────────────────────────────────────────────────────────────

def build_message(g_camp, g_conv, g_search, g_auction, g_landing, meta):
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%d %b %Y")

    # Performance score
    score = 0
    if g_camp.get("ctr", 0) >= 5:               score += 25
    elif g_camp.get("ctr", 0) >= 3:             score += 15
    if g_camp.get("conversions", 0) > 0:        score += 30
    if g_camp.get("cost_per_conv", 999) < 50:   score += 25
    if g_camp.get("clicks", 0) > 100:           score += 20
    grade = ("🟢 Strong"   if score >= 70 else
             "🟡 Moderate" if score >= 40 else
             "🔴 Needs Attention")

    # Google Ads section
    g_text = (
        f"*Impressions:* {g_camp.get('impressions',0):,}   "
        f"*Clicks:* {g_camp.get('clicks',0):,}   "
        f"*CTR:* {g_camp.get('ctr',0)}%\n"
        f"*Spend:* AED {g_camp.get('cost',0):,}   "
        f"*Conversions:* {g_camp.get('conversions',0)}   "
        f"*Cost/Conv:* AED {g_camp.get('cost_per_conv',0)}\n"
        f"*Campaigns:* {g_camp.get('campaigns',0)}"
    ) if g_camp else "_No data received yet_"

    # Conversions
    conv_text = ("\n".join(g_conv.get("breakdown", [])) or "_No conversion data_")

    # Search terms
    search_text = ("\n".join(g_search.get("terms", [])) or "_No search term data_")

    # Competitors
    comp_text = ("\n".join(g_auction.get("competitors", [])) or "_No competitor data_")

    # Landing pages
    land_text = ("\n".join(g_landing.get("pages", [])) or "_No landing page data_")

    # Meta section
    if meta:
        meta_text = (
            f"*Impressions:* {meta.get('impressions',0):,}   "
            f"*Reach:* {meta.get('reach',0):,}   "
            f"*Clicks:* {meta.get('clicks',0):,}\n"
            f"*Spent:* AED {meta.get('spent',0):,}   "
            f"*Results:* {meta.get('results',0)}   "
            f"*Cost/Result:* AED {meta.get('cost_per_result',0)}"
        )
    else:
        meta_text = "_No Meta data today_"

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text",
                  "text": f"📊 MKV Daily Ads Snapshot — {yesterday}"}},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": f"*🔵 Google Ads Performance*\n{g_text}"}},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": f"*🎯 Conversions Breakdown*\n{conv_text}"}},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": f"*🔍 Top Search Terms*\n{search_text}"}},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": f"*⚔️ Competitor Auction Insights*\n{comp_text}"}},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": f"*📄 Top Landing Pages*\n{land_text}"}},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": f"*🟦 Meta Ads Performance*\n{meta_text}"}},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": f"*🏆 Performance Score: {score}/100 — {grade}*"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
                        "text": f"_MKV Car Rental | Auto-generated | "
                                f"Google Ads ✅  Meta Ads ✅  | "
                                f"{'🧪 TEST MODE' if TEST_MODE else '🚀 LIVE'}_"}]}
    ]
    return blocks

# ── SLACK POST ────────────────────────────────────────────────────────────────

def post_slack(blocks):
    client = WebClient(token=SLACK_TOKEN)
    try:
        client.chat_postMessage(
            channel=SLACK_CHANNEL,
            blocks=blocks,
            text="MKV Daily Ads Snapshot"
        )
        print(f"  ✅  Posted to Slack: {SLACK_CHANNEL}")
    except SlackApiError as e:
        print(f"  ❌  Slack error: {e.response['error']}")

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 56)
    print("  MKV Daily Ads Snapshot")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} Dubai Time")
    print(f"  Mode: {'🧪 TEST' if TEST_MODE else '🚀 LIVE'}")
    print("=" * 56)

    # Google Ads reports
    print("\n📥 Fetching Google Ads reports from Gmail...")
    df_camp   = fetch_csv_imap(GMAIL_ADDRESS, GMAIL_APP_PASSWORD, REPORT_SUBJECTS["campaign"])
    df_conv   = fetch_csv_imap(GMAIL_ADDRESS, GMAIL_APP_PASSWORD, REPORT_SUBJECTS["conversions"])
    df_search = fetch_csv_imap(GMAIL_ADDRESS, GMAIL_APP_PASSWORD, REPORT_SUBJECTS["search_terms"])
    df_auction= fetch_csv_imap(GMAIL_ADDRESS, GMAIL_APP_PASSWORD, REPORT_SUBJECTS["auction"])
    df_landing= fetch_csv_imap(GMAIL_ADDRESS, GMAIL_APP_PASSWORD, REPORT_SUBJECTS["landing"])

    # Meta Ads report
    print("\n📥 Fetching Meta Ads report...")
    df_meta = None
    if META_GMAIL_APP_PASSWORD:
        df_meta = fetch_csv_imap(META_GMAIL_ADDRESS, META_GMAIL_APP_PASSWORD, META_SUBJECT)

    # Parse
    print("\n🔄 Parsing data...")
    g_camp    = parse_campaign(df_camp)
    g_conv    = parse_conversions(df_conv)
    g_search  = parse_search_terms(df_search)
    g_auction = parse_auction(df_auction)
    g_landing = parse_landing(df_landing)
    meta      = parse_meta(df_meta)

    print(f"\n  Google Ads — Spend: AED {g_camp.get('cost',0)} | "
          f"Clicks: {g_camp.get('clicks',0)} | "
          f"Conv: {g_camp.get('conversions',0)}")
    if meta:
        print(f"  Meta Ads   — Spent: AED {meta.get('spent',0)} | "
              f"Results: {meta.get('results',0)}")

    # Post to Slack
    print("\n📤 Posting to Slack...")
    blocks = build_message(g_camp, g_conv, g_search, g_auction, g_landing, meta)
    post_slack(blocks)

    print("\n✅  Done!\n")

if __name__ == "__main__":
    main()
