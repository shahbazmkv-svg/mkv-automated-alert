"""
MKV Luxury — Daily Leads Report (Gallabox → Slack)

Counts leads exactly as Gallabox's Analytics → Conversation Report does:
one lead = one conversation, dated by its "Created At" in Dubai time,
per channel (3 WhatsApp numbers + Instagram). Agent split = "Assigned To".
Source = the WhatsApp number used (Main = Google Ads, Car Rental = Meta + others,
Rent to Own, Instagram); on Car Rental, Meta is confirmed by the ad referral or welcome text.
Any lead starting "UID:" (website chat) counts as Google.

Gallabox's /conversations API only returns the latest 100 conversations per
channel and status (no pagination), so every run saves the conversations it
sees in mtd_store.json. MTD = all conversations created this month that any
run has seen, so a missed run is filled in by the next one.
"""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

# ── CREDENTIALS (GitHub Secrets) ──────────────────────────────────────────────
ACCOUNT_ID = os.environ.get("GALLABOX_ACCOUNT_ID", "").strip()
API_KEY    = os.environ.get("GALLABOX_API_KEY", "").strip()
API_SECRET = os.environ.get("GALLABOX_API_SECRET", "").strip()
if not (ACCOUNT_ID and API_KEY and API_SECRET):
    raise SystemExit("[ERROR] Set GALLABOX_ACCOUNT_ID, GALLABOX_API_KEY and GALLABOX_API_SECRET in GitHub Secrets")

# ── SLACK ─────────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
CHANNEL_LIVE    = "C0ABN1ZKSGN"   # #mkv-daily-lead-report
CHANNEL_TEST    = "C0B0TGBDCDU"   # #mkv-test-automation
TEST_MODE       = os.environ.get("TEST_MODE", "false").lower() == "true"

MTD_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mtd_store.json")
BASE_URL  = "https://server.gallabox.com/devapi/accounts/" + ACCOUNT_ID
HEADERS   = {"apiKey": API_KEY, "apiSecret": API_SECRET, "Content-Type": "application/json"}

# Same names and order as the channel picker in Gallabox
GALLABOX_CHANNELS = [
    {"name": "Main",           "id": "675a90ddda3020e52915beff", "type": "whatsapp"},
    {"name": "Car Rental",     "id": "66e930025e9ef7252ccc8a25", "type": "whatsapp"},
    {"name": "Rent to Own",    "id": "699d8cca452cc56936e21e45", "type": "whatsapp"},
    {"name": "Instagram",      "id": "68bece5baa64e82aba9cd45f", "type": "instagram"},
]
CHANNEL_NAMES = [c["name"] for c in GALLABOX_CHANNELS]
STATUSES      = ["OPEN", "CLOSE"]   # each status returns its own latest 100
API_PAGE_CAP  = 100
NOT_TAGGED    = "Not tagged"

DUBAI         = timezone(timedelta(hours=4))
now_dubai     = datetime.now(DUBAI)
report_day    = (now_dubai - timedelta(days=1)).date()        # "yesterday"
report_key    = report_day.isoformat()                        # 2026-11-04
report_month  = report_key[:7]                                # 2026-11
report_dt     = now_dubai.strftime("%d %b %Y | %I:%M %p Dubai Time")
report_str    = report_day.strftime("%d %b %Y")
month_str     = report_day.strftime("%b %Y")
month_start   = datetime(report_day.year, report_day.month, 1, tzinfo=DUBAI)
KEEP_DAYS     = 45                                            # prune store after this


# ══════════════════════════════════════════════════════════════════════════════
# STORE
# ══════════════════════════════════════════════════════════════════════════════
def load_store():
    try:
        with open(MTD_STORE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data.get("convs"), dict):
            return data
        print("  Old store format found - starting the new format (rebuilt from API)")
    except FileNotFoundError:
        pass
    except Exception as e:
        print("  [WARN] Could not read store: " + str(e))
    return {"convs": {}}


def save_store(store):
    cutoff = (now_dubai - timedelta(days=KEEP_DAYS)).date().isoformat()
    store["convs"] = {k: v for k, v in store["convs"].items() if v["date"] >= cutoff}
    store["updated"] = now_dubai.isoformat(timespec="seconds")
    with open(MTD_STORE, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=1, ensure_ascii=False, sort_keys=True)


# ══════════════════════════════════════════════════════════════════════════════
# GALLABOX
# ══════════════════════════════════════════════════════════════════════════════
def parse_utc(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def agent_of(conv):
    user = conv.get("user") or {}
    if user.get("name"):
        return user["name"].strip()
    bot = conv.get("bot") or {}
    if bot.get("name"):
        return "Bot: " + bot["name"].strip()
    return "Unassigned"


def fetch_channel(ch):
    """All conversations Gallabox will return for one channel (OPEN + CLOSE)."""
    out, warnings = [], []
    for status in STATUSES:
        params = {"channelId": ch["id"], "channelType": ch["type"], "status": status, "limit": API_PAGE_CAP}
        r = requests.get(BASE_URL + "/conversations", headers=HEADERS, params=params, timeout=30)
        r.raise_for_status()
        convs = r.json()
        if not isinstance(convs, list):
            raise RuntimeError("Unexpected response for " + ch["name"] + ": " + str(convs)[:200])
        out.extend(convs)
        # If a full page reaches no further back than the month start, older
        # conversations of this month may be cut off - the store covers them.
        if len(convs) >= API_PAGE_CAP:
            oldest = min(parse_utc(c.get("updatedAt", "")) or now_dubai for c in convs)
            if oldest > month_start:
                warnings.append(ch["name"] + "/" + status + " reaches back only to "
                                + oldest.astimezone(DUBAI).strftime("%d %b"))
    return out, warnings


def collect_conversations(store):
    seen_now, warnings = 0, []
    for ch in GALLABOX_CHANNELS:
        convs, warn = fetch_channel(ch)
        warnings += warn
        for c in convs:
            cid = c.get("id") or c.get("_id")
            ts = parse_utc(c.get("createdAt", ""))
            if not cid or not ts:
                continue
            seen_now += 1
            entry = store["convs"].setdefault(cid, {})   # keeps "origin" found on earlier runs
            entry.update({
                "channel": ch["name"],
                "date":    ts.astimezone(DUBAI).date().isoformat(),
                "contact": c.get("contactId") or (c.get("contact") or {}).get("id", ""),
                "agent":   agent_of(c),          # latest "Assigned To" - refreshed every run
                "status":  c.get("status", ""),
            })
    print("  Conversations returned by API: " + str(seen_now))
    return warnings


# ── Lead origin: read the lead's first message once and remember it ──────────
# Number-to-source mapping (set by MKV):
#   Main        +971 56 279 4545  -> Google Ads only
#   Car Rental  +971 52 438 5627  -> Meta Ads + others
#   Rent to Own +971 56 230 5052  -> Rent to Own only
#   Instagram   own channel
# On Car Rental, a Meta "Click to WhatsApp" ad attaches a referral
# (source_type "ad") to the first message and pre-fills one of the welcome
# texts below. Any lead whose first message starts "UID:" (website chat) is a
# Google lead, whichever number it arrives on.
CHANNEL_ID = {c["name"]: c["id"] for c in GALLABOX_CHANNELS}

META_WELCOME_TEXTS = [            # pre-filled by MKV's Meta ads (lower-case, prefix match)
    "can you share more details on this offer",
    "hi! i would like to book the car without deposit",
]
OTHER_WELCOME_TEXTS = [           # (prefix or phrase, label) for non-Meta leads on Car Rental
    ("oneclickdrive", "OneClickDrive"),
]


def detect_origin(item):
    cid, v = item
    try:
        r = requests.get(BASE_URL + "/messages", headers=HEADERS, timeout=20,
                         params={"channelId": CHANNEL_ID[v["channel"]], "contactId": v["contact"], "limit": 100})
        r.raise_for_status()
        msgs = r.json()
        if not isinstance(msgs, list):
            return cid, None
    except Exception as e:
        print("  [WARN] messages for " + cid + ": " + str(e))
        return cid, None                                     # retried next run

    inbound = sorted((m for m in msgs if m.get("conversationId") == cid and m.get("sender") == v["contact"]),
                     key=lambda m: m.get("createdAt", ""))
    if not inbound:
        return cid, {"origin": "outbound", "first_text": ""}
    first = inbound[0]
    body  = first.get(first.get("channelType")) or {}
    body  = body if isinstance(body, dict) else {}
    text  = body.get("text")
    text  = ((text.get("body") if isinstance(text, dict) else text) or "").strip()
    found = {"first_text": text[:80]}
    ref   = body.get("referral") or {}
    low   = text.lower()
    if ref.get("source_type") == "ad":
        url = (ref.get("source_url") or "").lower()
        placement = "Instagram" if "instagram." in url else "Facebook" if ("fb.me" in url or "facebook." in url) else "Meta"
        found.update({"origin": "meta_ad", "placement": placement,
                      "ad_id": str(ref.get("source_id") or ""), "ad_name": (ref.get("headline") or "")[:40]})
    elif any(low.startswith(t) for t in META_WELCOME_TEXTS):
        found.update({"origin": "meta_ad", "placement": "Meta (welcome text)"})
    elif low.startswith("uid:"):
        found["origin"] = "website"
    else:
        found["origin"] = next((label for phrase, label in OTHER_WELCOME_TEXTS if phrase in low), "direct")
    return cid, found


def detect_origins(store):
    todo = [(k, v) for k, v in store["convs"].items()
            if "first_text" not in v and v.get("contact") and v["date"] >= month_start.date().isoformat()]
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=6) as ex:
        for cid, found in ex.map(detect_origin, todo):
            if found:
                store["convs"][cid].update(found)
    print("  Ad origin checked for " + str(len(todo)) + " new conversation(s)")


def fetch_contact(contact_id):
    try:
        r = requests.get(BASE_URL + "/contacts/" + contact_id, headers=HEADERS, timeout=15)
        r.raise_for_status()
        d    = r.json()
        tags = [(t.get("name") or "").lower() for t in (d.get("tags") or [])]
        kv   = d.get("fieldValuesKV") or {}
        return {
            "triggered": "triggered" in tags,
            "source":    (kv.get("lead_source") or "").strip() or NOT_TAGGED,
            "stage":     (kv.get("lead_stage") or "").strip() or NOT_TAGGED,
        }
    except Exception as e:
        print("  [WARN] contact " + contact_id + ": " + str(e))
        return {"triggered": False, "source": NOT_TAGGED, "stage": NOT_TAGGED}


# ══════════════════════════════════════════════════════════════════════════════
# AGGREGATE
# ══════════════════════════════════════════════════════════════════════════════
MKT_ORDER = ["Google Ads", "Meta Ads", "Other (Car Rental no.)", "Rent to Own", "Instagram"]


def marketing_source(v):
    """(source, detail) from the WhatsApp number the lead used + its welcome text."""
    ch, origin = v["channel"], v.get("origin", "")
    if origin == "website":                                     # "UID:..." = Google lead, on any number
        return "Google Ads", "Website chat (UID)"
    if ch == "Main":
        return "Google Ads", "Direct WhatsApp"
    if ch == "Rent to Own":
        return "Rent to Own", ""
    if ch == "Instagram":
        return "Instagram", ""
    # Car Rental number: Meta ads + others
    if origin == "meta_ad":
        place = v.get("placement", "Meta")
        return "Meta Ads", place if place.startswith("Meta") else place + " ad"
    if origin == "outbound":
        return "Other (Car Rental no.)", "Started by MKV"
    if origin in ("", "direct", "website"):
        return "Other (Car Rental no.)", "Direct"
    return "Other (Car Rental no.)", origin                     # e.g. OneClickDrive


def build_stats(store):
    month = [v for v in store["convs"].values() if v["date"].startswith(report_month) and v["date"] <= report_key]
    contact_ids = sorted({v["contact"] for v in month if v["contact"]})
    with ThreadPoolExecutor(max_workers=8) as ex:
        details = dict(zip(contact_ids, ex.map(fetch_contact, contact_ids)))

    def blank():
        return {"y": 0, "m": 0}
    s = {"channel": {n: blank() for n in CHANNEL_NAMES}, "agent": {}, "agent_trig": {},
         "agent_ch": {}, "agent_mkt": {}, "marketing": {k: blank() for k in MKT_ORDER}, "detail": {}, "meta_ad": {},
         "source": {}, "stage": {}}

    for v in month:
        is_y = v["date"] == report_key
        d    = details.get(v["contact"], {"triggered": False, "source": NOT_TAGGED, "stage": NOT_TAGGED})
        mkt, detail = marketing_source(v)
        if detail:
            row = s["detail"].setdefault(mkt, {}).setdefault(detail, blank())
            row["m"] += 1
            row["y"] += is_y
        if mkt == "Meta Ads" and v.get("ad_name"):
            row = s["meta_ad"].setdefault(v["ad_name"], blank())
            row["m"] += 1
            row["y"] += is_y
        amk = s["agent_mkt"].setdefault(v["agent"], {})
        amk[mkt] = amk.get(mkt, 0) + 1
        for bucket, key in (("channel", v["channel"]), ("agent", v["agent"]), ("marketing", mkt),
                            ("source", d["source"]), ("stage", d["stage"])):
            row = s[bucket].setdefault(key, blank())
            row["m"] += 1
            row["y"] += is_y
        trig = s["agent_trig"].setdefault(v["agent"], blank())
        trig["m"] += d["triggered"]
        trig["y"] += d["triggered"] and is_y
        ach = s["agent_ch"].setdefault(v["agent"], {})
        ach[v["channel"]] = ach.get(v["channel"], 0) + 1
    return s


# ══════════════════════════════════════════════════════════════════════════════
# SLACK MESSAGE
# ══════════════════════════════════════════════════════════════════════════════
def table(rows, first_col, order=(), width=22, show_zero=False):
    keys = [k for k in order if k in rows] + sorted(k for k in rows if k not in order)
    if not show_zero:
        keys = [k for k in keys if rows[k]["y"] or rows[k]["m"]]
    out  = "{:<{w}} {:>9} {:>7}\n".format(first_col, "Yesterday", "MTD", w=width) + "-" * (width + 18) + "\n"
    for k in keys:
        out += "{:<{w}} {:>9} {:>7}\n".format(k[:width], rows[k]["y"], rows[k]["m"], w=width)
    out += "-" * (width + 18) + "\n"
    out += "{:<{w}} {:>9} {:>7}".format("TOTAL", sum(r["y"] for r in rows.values()),
                                        sum(r["m"] for r in rows.values()), w=width)
    return "```" + out + "```"


def agent_table(s):
    w   = 20
    out = "{:<{w}} {:>5} {:>5} {:>6} {:>6}\n".format("Agent", "Y.Rec", "Y.Trg", "M.Rec", "M.Trg", w=w)
    out += "-" * (w + 26) + "\n"
    agents = sorted(s["agent"], key=lambda a: (a == "Unassigned", a.startswith("Bot:"), -s["agent"][a]["m"], a))
    for a in agents:
        r, t = s["agent"][a], s["agent_trig"].get(a, {"y": 0, "m": 0})
        out += "{:<{w}} {:>5} {:>5} {:>6} {:>6}\n".format(a[:w], r["y"], t["y"], r["m"], t["m"], w=w)
    out += "-" * (w + 26) + "\n"
    out += "{:<{w}} {:>5} {:>5} {:>6} {:>6}".format(
        "TOTAL", sum(r["y"] for r in s["agent"].values()), sum(r["y"] for r in s["agent_trig"].values()),
        sum(r["m"] for r in s["agent"].values()), sum(r["m"] for r in s["agent_trig"].values()), w=w)
    return "```" + out + "```"


def agent_marketing_table(s):
    cols  = [("Google Ads", "Google"), ("Meta Ads", "Meta"), ("Other (Car Rental no.)", "Other"),
             ("Rent to Own", "RTO"), ("Instagram", "Insta")]
    w     = 18
    out   = "{:<{w}}".format("Agent (MTD)", w=w) + "".join("{:>7}".format(c[1]) for c in cols) + "\n"
    out  += "-" * (w + 7 * len(cols)) + "\n"
    agents = sorted(s["agent_mkt"], key=lambda a: (a == "Unassigned", a.startswith("Bot:"), -s["agent"][a]["m"], a))
    for a in agents:
        row  = s["agent_mkt"][a]
        out += "{:<{w}}".format(a[:w], w=w) + "".join("{:>7}".format(row.get(c[0], 0)) for c in cols) + "\n"
    return "```" + out.rstrip() + "```"


def detail_lines(s):
    lines = []
    for src in MKT_ORDER:
        parts = sorted(s["detail"].get(src, {}).items(), key=lambda kv: -kv[1]["m"])
        if parts:
            lines.append("• " + src + ": " + " · ".join("{} {}/{}".format(k, v["y"], v["m"]) for k, v in parts))
    ads = sorted(s["meta_ad"].items(), key=lambda kv: -kv[1]["m"])[:3]
    if ads:
        lines.append("• Top Meta ads: " + " · ".join("\"{}\" {}/{}".format(k, v["y"], v["m"]) for k, v in ads))
    return ("_Breakdown (yesterday/MTD):_\n" + "\n".join(lines) + "\n") if lines else ""


def build_message(s, warnings):
    stg_order = ["Lead created", "Qualified lead", "Converted lead", NOT_TAGGED]
    msg = (
        ":bar_chart: *MKV LUXURY — LEADS & AGENTS REPORT*\n"
        + ":calendar: " + report_dt + "\n"
        + "_Yesterday = " + report_str + " | MTD = 1 " + month_str + " to " + report_str
        + " | Lead = new conversation (Gallabox Conversation Report, Created At)_\n\n"
        + "*:moneybag: LEADS BY SOURCE*\n" + table(s["marketing"], "Source", MKT_ORDER, show_zero=True) + "\n"
        + detail_lines(s)
        + "_Google = Main no. (+971 56 279 4545) + any UID website chat · Meta = Car Rental no. (+971 52 438 5627) ad clicks · "
        + "Rent to Own = +971 56 230 5052 · Instagram = mkvluxurydubai_\n"
        + "*:dart: AGENT PERFORMANCE* _(Assigned To)_\n" + agent_table(s) + "\n"
        + "*:busts_in_silhouette: AGENT SPLIT BY SOURCE*\n" + agent_marketing_table(s) + "\n"
        + "*:chart_with_upwards_trend: LEAD STAGE*\n" + table(s["stage"], "Stage", stg_order)
    )
    if warnings:
        msg += "\n:warning: _API window: " + "; ".join(warnings) + ". MTD relies on saved history for earlier days._"
    return msg


def send_to_slack(message):
    channel = CHANNEL_TEST if TEST_MODE else CHANNEL_LIVE
    if not SLACK_BOT_TOKEN:
        print("  [ERROR] SLACK_BOT_TOKEN not set")
        return False
    r = requests.post("https://slack.com/api/chat.postMessage",
                      headers={"Authorization": "Bearer " + SLACK_BOT_TOKEN, "Content-Type": "application/json"},
                      json={"channel": channel, "text": message, "unfurl_links": False}, timeout=15)
    ok = r.json().get("ok")
    print("  Slack " + ("OK -> " + channel if ok else "error: " + str(r.json().get("error"))))
    return ok


# ══════════════════════════════════════════════════════════════════════════════
def main():
    print("=" * 56)
    print("  MKV LUXURY - DAILY LEADS REPORT")
    print("  Reporting for: " + report_str + " | MTD month: " + report_month)
    print("  TEST_MODE: " + str(TEST_MODE))
    print("=" * 56)

    store    = load_store()
    warnings = collect_conversations(store)
    detect_origins(store)
    save_store(store)                       # save before posting so a Slack failure loses nothing
    stats    = build_stats(store)

    for n in CHANNEL_NAMES:
        print("  {:<12} yesterday {:>3} | MTD {:>4}".format(n, stats["channel"][n]["y"], stats["channel"][n]["m"]))
    for w in warnings:
        print("  [WARN] " + w)

    if os.environ.get("DRY_RUN", "").lower() == "true":
        print(build_message(stats, warnings))
        return
    send_to_slack(build_message(stats, warnings))


if __name__ == "__main__":
    main()
