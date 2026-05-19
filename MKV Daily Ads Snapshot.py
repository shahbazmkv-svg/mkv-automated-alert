"""
MKV Daily Ads Snapshot — Gmail → Slack Automation
==================================================
Version : 2.0  (19 May 2026)
Changes : Correct email subjects, Meta forwarded email fix,
          Google Ads download-link parser, retry logic,
          day-over-day delta, Slack Block Kit, error alerts.

Gmail accounts
  Google Ads reports → shahbazmkv@gmail.com
  Meta Ads report    → forwarded to shahbazmkv@gmail.com
                       (original from contact@mkvluxury.com)

Slack channels
  Test  : mkv-test-automation   C0B0TGBDCDU
  Live  : mkv-marketing-team    C0AASQKLY59

GitHub Secrets required
  SLACK_BOT_TOKEN
  GMAIL_ADDRESS         (shahbazmkv@gmail.com)
  GMAIL_APP_PASSWORD    (16-char App Password)

Run: python mkv_daily_report_v2.py
     TEST_MODE=true python mkv_daily_report_v2.py
"""

import imaplib
import email
import io
import gzip
import os
import re
import json
import time
import urllib.request
import urllib.parse
import pandas as pd
from datetime import datetime, timedelta
from email import message_from_bytes
from email.header import decode_header
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ── CONFIG ────────────────────────────────────────────────────────────────────

SLACK_TOKEN        = os.environ.get("SLACK_BOT_TOKEN", "YOUR_SLACK_BOT_TOKEN")
GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS", "shahbazmkv@gmail.com")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "YOUR_APP_PASSWORD")

TEST_MODE          = os.environ.get("TEST_MODE", "false").lower() == "true"
SLACK_CHANNEL      = "C0B0TGBDCDU" if TEST_MODE else "C0AASQKLY59"

DELTA_FILE         = "mkv_yesterday_metrics.json"   # stores prev day for delta

# ── EXACT subject lines as they arrive in Gmail ───────────────────────────────
GOOGLE_SUBJECTS = {
    "campaign"    : "Your Google Ads report is ready: Campaign performance",
    "conversions" : "Your Google Ads report is ready: Conversions",
    "search_terms": "Your Google Ads report is ready: Search terms",
    "auction"     : "Your Google Ads report is ready: Auction insights - search",
    "landing"     : "Your Google Ads report is ready: MKV Google Ads - Landing Pages",
}

# Meta arrives as a forward — search by subject keyword in shahbazmkv inbox
META_SUBJECT_KEYWORD = "Your Daily Facebook ads report"

REPORT_DATE = (datetime.now() - timedelta(days=1)).strftime("%d %b %Y")  # yesterday's data

# ── IMAP HELPERS ──────────────────────────────────────────────────────────────

def imap_connect(retries=3):
    """Connect to Gmail IMAP with retry logic."""
    for attempt in range(1, retries + 1):
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            mail.select("inbox")
            print(f"  ✅  Gmail connected (attempt {attempt})")
            return mail
        except Exception as e:
            print(f"  ⚠️   Gmail connect attempt {attempt} failed: {e}")
            if attempt < retries:
                time.sleep(5)
    print("  ❌  Could not connect to Gmail after 3 attempts")
    return None


def fetch_email_by_subject(mail, subject_line):
    """
    Search inbox for an email whose subject contains subject_line.
    Tries today first, then yesterday as fallback.
    Returns the raw email.Message object or None.
    """
    for days_ago in [0, 1]:
        since = (datetime.now() - timedelta(days=days_ago)).strftime("%d-%b-%Y")
        # Use SUBJECT search — works even for forwarded emails
        query = f'(SUBJECT "{subject_line}" SINCE {since})'
        _, msg_ids = mail.search(None, query)
        ids = msg_ids[0].split() if msg_ids[0] else []
        if ids:
            latest = ids[-1]
            _, msg_data = mail.fetch(latest, "(RFC822)")
            raw = msg_data[0][1]
            msg = message_from_bytes(raw)
            subj = _decode_subject(msg.get("Subject", ""))
            print(f"  ✅  Found: '{subj[:60]}' (SINCE {since})")
            return msg
    print(f"  ⚠️   Not found: '{subject_line}'")
    return None


def _decode_subject(raw_subject):
    parts = decode_header(raw_subject)
    decoded = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            decoded += part.decode(enc or "utf-8", errors="ignore")
        else:
            decoded += str(part)
    return decoded

# ── CSV EXTRACTION ────────────────────────────────────────────────────────────

def extract_csv_from_message(msg):
    """
    Extract CSV content from an email message.
    Handles: direct CSV attachment, GZ attachment, download link in body.
    Returns a string (CSV text) or None.
    """
    # 1. Check for CSV / GZ attachments
    for part in msg.walk():
        fname = part.get_filename() or ""
        ctype = part.get_content_type()
        is_csv = (
            fname.endswith(".csv") or
            fname.endswith(".csv.gz") or
            ctype in ("text/csv", "application/csv",
                      "application/octet-stream", "application/gzip")
        )
        if is_csv and part.get_payload(decode=True):
            payload = part.get_payload(decode=True)
            if fname.endswith(".gz") or ctype == "application/gzip":
                try:
                    payload = gzip.decompress(payload)
                except Exception:
                    pass
            return _decode_csv_bytes(payload)

    # 2. Look for a download link in the plain-text body
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            body = part.get_payload(decode=True)
            if body:
                text = body.decode("utf-8", errors="ignore")
                # Google Ads report download links
                link_match = re.search(
                    r'https://storage\.googleapis\.com/[^\s"<>]+\.csv[^\s"<>]*',
                    text
                )
                if link_match:
                    url = link_match.group(0).strip()
                    print(f"    → Downloading CSV from link: {url[:80]}...")
                    return _download_url(url)

    return None


def _decode_csv_bytes(payload):
    """Try multiple encodings to decode CSV bytes."""
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return payload.decode(enc)
        except Exception:
            continue
    return payload.decode("utf-8", errors="replace")


def _download_url(url, retries=3):
    """Download a URL and return content as string."""
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return _decode_csv_bytes(resp.read())
        except Exception as e:
            print(f"    ⚠️   Download attempt {attempt} failed: {e}")
            if attempt < retries:
                time.sleep(3)
    return None

# ── CSV → DATAFRAME ───────────────────────────────────────────────────────────

def csv_to_df(csv_text, report_name=""):
    """
    Convert CSV text to a clean DataFrame.
    Skips Google Ads header/footer rows automatically.
    """
    if not csv_text:
        return None
    try:
        lines = csv_text.split("\n")
        # Find the header row (first row containing known column keywords)
        header_keywords = [
            "Campaign", "Impressions", "Clicks", "Cost", "Search term",
            "Reach", "Results", "Ad set", "Conversions", "Landing page",
            "Display URL", "Impression share"
        ]
        start = 0
        for i, line in enumerate(lines):
            if any(kw in line for kw in header_keywords):
                start = i
                break

        df = pd.read_csv(io.StringIO("\n".join(lines[start:])))
        # Drop completely empty rows and Google summary/footer rows
        df = df.dropna(how="all")
        first_col = df.columns[0]
        df = df[~df[first_col].astype(str).str.match(
            r"^(Total|Report|©|Grand|\s*$)", na=False
        )]
        df = df.reset_index(drop=True)
        print(f"    → {report_name}: {len(df)} rows loaded")
        return df
    except Exception as e:
        print(f"    ❌  CSV parse error ({report_name}): {e}")
        return None

# ── NORMALISE HELPERS ─────────────────────────────────────────────────────────

def norm_cols(df):
    """Normalise column names: lowercase, replace spaces/slashes with _."""
    df = df.copy()
    df.columns = [
        c.strip().lower()
         .replace(" ", "_").replace("/", "_")
         .replace("(", "").replace(")", "")
        for c in df.columns
    ]
    return df


def to_num(series):
    """Convert a Series to numeric, stripping currency symbols & commas."""
    return pd.to_numeric(
        series.astype(str)
              .str.replace(",", "", regex=False)
              .str.replace("AED", "", regex=False)
              .str.replace("د.إ", "", regex=False)
              .str.replace("%", "", regex=False)
              .str.strip(),
        errors="coerce"
    ).fillna(0)


def find_col(df, *keywords):
    """Return first column name whose lowercase contains any keyword."""
    for kw in keywords:
        for col in df.columns:
            if kw in col:
                return col
    return None

# ── PARSERS ───────────────────────────────────────────────────────────────────

def parse_campaign(df):
    if df is None or df.empty:
        return {}
    df = norm_cols(df)
    imp_col = find_col(df, "impress")
    clk_col = find_col(df, "clicks")
    cst_col = find_col(df, "cost")       # avoid "cost_per_conv"
    cnv_col = find_col(df, "conversions")
    cam_col = find_col(df, "campaign")

    # Exclude cost-per-conversion columns from spend total
    if cst_col and ("conv" in cst_col or "per" in cst_col):
        cst_col = None
        for col in df.columns:
            if "cost" in col and "conv" not in col and "per" not in col:
                cst_col = col
                break

    impr = to_num(df[imp_col]).sum() if imp_col else 0
    clks = to_num(df[clk_col]).sum() if clk_col else 0
    cost = to_num(df[cst_col]).sum() if cst_col else 0
    conv = to_num(df[cnv_col]).sum() if cnv_col else 0

    # Top campaigns breakdown
    campaigns = []
    if cam_col:
        for _, row in df.iterrows():
            c_impr = to_num(pd.Series([row[imp_col]])).iloc[0] if imp_col else 0
            c_clks = to_num(pd.Series([row[clk_col]])).iloc[0] if clk_col else 0
            c_cost = to_num(pd.Series([row[cst_col]])).iloc[0] if cst_col else 0
            campaigns.append({
                "name"  : str(row[cam_col])[:45],
                "spend" : round(c_cost, 2),
                "clicks": int(c_clks),
                "ctr"   : round(c_clks / c_impr * 100, 2) if c_impr > 0 else 0,
            })
        campaigns.sort(key=lambda x: x["spend"], reverse=True)

    return {
        "impressions"   : int(impr),
        "clicks"        : int(clks),
        "cost"          : round(cost, 2),
        "conversions"   : int(conv),
        "ctr"           : round(clks / impr * 100, 2) if impr > 0 else 0,
        "cost_per_conv" : round(cost / conv, 2) if conv > 0 else 0,
        "campaigns"     : campaigns[:3],
    }


def parse_conversions(df):
    if df is None or df.empty:
        return {}
    df = norm_cols(df)
    action_col = find_col(df, "action", "type", "conversion_name")
    value_col  = find_col(df, "conversions", "value")
    cpc_col    = find_col(df, "cost_per", "cost__conv")
    rows = []
    if action_col:
        for _, row in df.iterrows():
            val = f" — {row[value_col]}" if value_col else ""
            cpc = f" | AED {row[cpc_col]}/conv" if cpc_col else ""
            rows.append(f"• {row[action_col]}{val}{cpc}")
    return {"breakdown": rows[:5]}


def parse_search_terms(df, top_n=5):
    if df is None or df.empty:
        return {}
    df = norm_cols(df)
    term_col = find_col(df, "search_term", "query", "term")
    clk_col  = find_col(df, "clicks")
    if not term_col or not clk_col:
        return {}
    df[clk_col] = to_num(df[clk_col])
    top = df.nlargest(top_n, clk_col)
    return {
        "terms": [
            f"• {row[term_col]}  ({int(row[clk_col])} clicks)"
            for _, row in top.iterrows()
        ]
    }


def parse_auction(df, top_n=5):
    if df is None or df.empty:
        return {}
    df = norm_cols(df)
    domain_col  = find_col(df, "domain", "display_url", "competitor", "name")
    overlap_col = find_col(df, "overlap", "impression_share")
    rows = []
    for _, row in df.head(top_n).iterrows():
        overlap = f"  {row[overlap_col]}" if overlap_col else ""
        rows.append(f"• {row[domain_col]}{overlap}")
    return {"competitors": rows}


def parse_landing(df, top_n=3):
    if df is None or df.empty:
        return {}
    df = norm_cols(df)
    url_col = find_col(df, "landing_page", "final_url", "url")
    clk_col = find_col(df, "clicks")
    ctr_col = find_col(df, "ctr")
    if not url_col or not clk_col:
        return {}
    df[clk_col] = to_num(df[clk_col])
    top = df.nlargest(top_n, clk_col)
    pages = []
    for _, r in top.iterrows():
        url   = str(r[url_col]).replace("https://www.mkvluxury.com", "")
        ctr   = f" | CTR {r[ctr_col]}" if ctr_col else ""
        pages.append(f"• {url[:55]}  —  {int(r[clk_col])} clicks{ctr}")
    return {"pages": pages}


def parse_meta(df):
    """
    Parse Meta Ads CSV.
    Meta exports: Amount spent (AED), Reach, Impressions, Clicks (all),
                  Results, Cost per result, CTR (all)
    """
    if df is None or df.empty:
        return {}
    df = norm_cols(df)
    imp_col  = find_col(df, "impress")
    reach_col= find_col(df, "reach")
    clk_col  = find_col(df, "clicks")
    cst_col  = find_col(df, "spent", "spend", "amount")
    res_col  = find_col(df, "results")
    cpr_col  = find_col(df, "cost_per_result", "cost_per")

    spent = round(to_num(df[cst_col]).sum(), 2) if cst_col else 0
    impr  = int(to_num(df[imp_col]).sum())  if imp_col  else 0
    reach = int(to_num(df[reach_col]).sum()) if reach_col else 0
    clks  = int(to_num(df[clk_col]).sum())  if clk_col  else 0
    res   = int(to_num(df[res_col]).sum())   if res_col  else 0
    cpr   = round(to_num(df[cpr_col]).mean(), 2) if cpr_col else 0

    ctr   = round(clks / impr * 100, 2) if impr > 0 else 0

    return {
        "spent"          : spent,
        "impressions"    : impr,
        "reach"          : reach,
        "clicks"         : clks,
        "results"        : res,
        "cost_per_result": cpr,
        "ctr"            : ctr,
    }

# ── DAY-OVER-DAY DELTA ────────────────────────────────────────────────────────

def load_yesterday():
    try:
        if os.path.exists(DELTA_FILE):
            with open(DELTA_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_today(g_camp, meta):
    data = {
        "date"       : REPORT_DATE,
        "g_cost"     : g_camp.get("cost", 0),
        "g_clicks"   : g_camp.get("clicks", 0),
        "g_conv"     : g_camp.get("conversions", 0),
        "g_ctr"      : g_camp.get("ctr", 0),
        "m_spent"    : meta.get("spent", 0),
        "m_results"  : meta.get("results", 0),
    }
    try:
        with open(DELTA_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"  ⚠️   Could not save delta file: {e}")


def delta(today_val, yesterday_val, prefix="AED ", pct=False):
    """Return a formatted delta string e.g. '+AED 120 ↑' or '-5% ↓'."""
    if yesterday_val == 0:
        return ""
    diff = today_val - yesterday_val
    pct_change = round(diff / yesterday_val * 100, 1)
    arrow = "↑" if diff > 0 else "↓" if diff < 0 else "→"
    if pct:
        return f"  ({'+' if diff >= 0 else ''}{pct_change}% {arrow})"
    return f"  ({'+' if diff >= 0 else ''}{prefix}{abs(diff):,.0f} {arrow})"

# ── PERFORMANCE SCORE ─────────────────────────────────────────────────────────

def score_report(g_camp, meta):
    """
    Score 0–100. Thresholds calibrated for luxury car rental industry.
    CTR benchmark: 2–3% for luxury automotive.
    """
    score  = 0
    notes  = []

    ctr  = g_camp.get("ctr", 0)
    conv = g_camp.get("conversions", 0)
    cost = g_camp.get("cost", 0)
    cpc  = g_camp.get("cost_per_conv", 0)

    # CTR (25 pts)
    if ctr >= 3:
        score += 25; notes.append("✅ CTR above industry benchmark (3%+)")
    elif ctr >= 1.5:
        score += 15; notes.append("🟡 CTR average — test new headlines")
    elif ctr > 0:
        score += 5;  notes.append("🔴 Low CTR — urgent ad copy review needed")

    # Conversions (30 pts)
    if conv >= 5:
        score += 30; notes.append("✅ Strong conversions")
    elif conv >= 1:
        score += 15; notes.append("🟡 Some conversions — optimise landing pages")
    else:
        notes.append("🔴 Zero conversions — check tracking & landing pages")

    # Cost efficiency (20 pts)
    if 0 < cpc < 100:
        score += 20; notes.append("✅ Cost/conv efficient")
    elif 0 < cpc < 200:
        score += 10; notes.append("🟡 Cost/conv moderate")
    elif cpc >= 200:
        notes.append("🔴 High cost per conversion")

    # Spend active (10 pts)
    if cost > 0:
        score += 10

    # Meta (15 pts)
    if meta.get("spent", 0) > 0:
        score += 10; notes.append("✅ Meta Ads active")
        if meta.get("ctr", 0) >= 1.5:
            score += 5; notes.append("✅ Meta CTR strong")

    grade = (
        "🟢 Excellent"        if score >= 80 else
        "🟡 Good"             if score >= 60 else
        "🟠 Needs Improvement" if score >= 40 else
        "🔴 Needs Attention"
    )
    return min(score, 100), grade, notes

# ── SLACK BLOCK KIT MESSAGE ───────────────────────────────────────────────────

def build_blocks(g_camp, g_conv, g_search, g_auction, g_landing, meta, yesterday):
    score, grade, notes = score_report(g_camp, meta)

    # ── Deltas
    d_cost  = delta(g_camp.get("cost", 0),    yesterday.get("g_cost", 0))
    d_clk   = delta(g_camp.get("clicks", 0),  yesterday.get("g_clicks", 0), prefix="")
    d_conv  = delta(g_camp.get("conversions",0),yesterday.get("g_conv",0), prefix="")
    d_mcost = delta(meta.get("spent", 0),     yesterday.get("m_spent", 0))

    # ── Section texts
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
        g_text = "_No Google Ads data received yet_"

    conv_text   = "\n".join(g_conv.get("breakdown", [])) or "_No conversion data_"
    search_text = "\n".join(g_search.get("terms", []))    or "_No search term data_"
    comp_text   = "\n".join(g_auction.get("competitors",[]))or "_No competitor data_"
    land_text   = "\n".join(g_landing.get("pages", []))   or "_No landing page data_"

    if meta.get("spent", 0) > 0:
        meta_text = (
            f"*Spent:* AED {meta['spent']:,.2f}{d_mcost}    "
            f"*Results:* {meta['results']}    "
            f"*Cost/Result:* AED {meta['cost_per_result']:,.2f}\n"
            f"*Impressions:* {meta['impressions']:,}    "
            f"*Reach:* {meta['reach']:,}    "
            f"*CTR:* {meta['ctr']}%"
        )
    else:
        meta_text = "_No Meta Ads data today_"

    score_text = f"*Score: {score}/100 — {grade}*\n" + "\n".join(notes)

    mode_tag = "🧪 TEST MODE" if TEST_MODE else "🚀 LIVE"

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text",
                  "text": f"📊 MKV Daily Ads Snapshot — {REPORT_DATE}",
                  "emoji": True}},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*🔵 Google Ads Performance*\n{g_text}"}},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*🎯 Conversions Breakdown*\n{conv_text}"}},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*🔍 Top Search Terms*\n{search_text}"}},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*⚔️ Competitor Auction Insights*\n{comp_text}"}},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*📄 Top Landing Pages*\n{land_text}"}},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*🟦 Meta Ads Performance*\n{meta_text}"}},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*🏆 Performance Score*\n{score_text}"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
                       "text": (f"_MKV Luxury Car Rental  •  Auto-generated  •  {mode_tag}_")}]},
    ]
    return blocks


def build_error_blocks(errors):
    """Post a warning block if some reports could not be fetched."""
    error_list = "\n".join(f"• {e}" for e in errors)
    return [
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": (f"⚠️  *MKV Ads Report — Partial Data Warning* ({REPORT_DATE})\n"
                           f"The following reports could not be fetched:\n{error_list}\n"
                           f"_Check Gmail and re-run if needed._")}},
    ]

# ── SLACK POST ────────────────────────────────────────────────────────────────

def post_slack(blocks, fallback_text="MKV Daily Ads Snapshot"):
    client = WebClient(token=SLACK_TOKEN)
    try:
        client.chat_postMessage(
            channel=SLACK_CHANNEL,
            blocks=blocks,
            text=fallback_text,
        )
        print(f"  ✅  Posted to Slack channel: {SLACK_CHANNEL}")
    except SlackApiError as e:
        print(f"  ❌  Slack error: {e.response['error']}")

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  MKV Daily Ads Snapshot v2.0")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Report date: {REPORT_DATE}")
    print(f"  Mode: {'🧪 TEST' if TEST_MODE else '🚀 LIVE'}  |  Channel: {SLACK_CHANNEL}")
    print("=" * 60)

    errors = []

    # ── Connect Gmail
    mail = imap_connect()
    if not mail:
        post_slack(build_error_blocks(["Could not connect to Gmail after 3 attempts"]),
                   "MKV Ads Report — Gmail Connection Failed")
        return

    # ── Fetch Google Ads reports
    print("\n📥 Fetching Google Ads reports...")
    raw_reports = {}
    for key, subject in GOOGLE_SUBJECTS.items():
        msg = fetch_email_by_subject(mail, subject)
        if msg:
            csv_text = extract_csv_from_message(msg)
            raw_reports[key] = csv_to_df(csv_text, key) if csv_text else None
            if raw_reports[key] is None:
                errors.append(f"Google Ads '{key}' — email found but CSV empty/unreadable")
        else:
            raw_reports[key] = None
            errors.append(f"Google Ads '{key}' — email not found in Gmail")

    # ── Fetch Meta Ads report (forwarded, in shahbazmkv inbox)
    print("\n📥 Fetching Meta Ads report (forwarded)...")
    meta_msg = fetch_email_by_subject(mail, META_SUBJECT_KEYWORD)
    if meta_msg:
        meta_csv = extract_csv_from_message(meta_msg)
        df_meta  = csv_to_df(meta_csv, "meta") if meta_csv else None
        if df_meta is None:
            errors.append("Meta Ads — email found but CSV not readable")
    else:
        df_meta = None
        errors.append("Meta Ads — email not found in Gmail")

    mail.logout()

    # ── Parse all reports
    print("\n🔄 Parsing reports...")
    g_camp    = parse_campaign(raw_reports.get("campaign"))
    g_conv    = parse_conversions(raw_reports.get("conversions"))
    g_search  = parse_search_terms(raw_reports.get("search_terms"))
    g_auction = parse_auction(raw_reports.get("auction"))
    g_landing = parse_landing(raw_reports.get("landing"))
    meta      = parse_meta(df_meta)

    # ── Delta vs yesterday
    yesterday = load_yesterday()
    save_today(g_camp, meta)

    # ── Print summary
    print(f"\n  Google Ads → Spend: AED {g_camp.get('cost',0):.2f} | "
          f"Clicks: {g_camp.get('clicks',0)} | "
          f"CTR: {g_camp.get('ctr',0)}% | "
          f"Conv: {g_camp.get('conversions',0)}")
    print(f"  Meta Ads   → Spent: AED {meta.get('spent',0):.2f} | "
          f"Results: {meta.get('results',0)} | "
          f"CTR: {meta.get('ctr',0)}%")

    # ── Post report to Slack
    print("\n📤 Posting to Slack...")
    blocks = build_blocks(g_camp, g_conv, g_search, g_auction, g_landing, meta, yesterday)
    post_slack(blocks)

    # ── Post error warning if any reports failed
    if errors:
        print(f"\n⚠️  {len(errors)} report(s) had issues — posting warning...")
        post_slack(build_error_blocks(errors), "MKV Ads Report — Data Warning")

    print("\n✅  Done!\n")


if __name__ == "__main__":
    main()
