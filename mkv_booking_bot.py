import os, json, hashlib, hmac, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote_plus
import requests

SLACK_BOT_TOKEN      = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
PORT                 = int(os.environ.get("PORT", 3000))

HEADERS = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}", "Content-Type": "application/json; charset=utf-8"}

FUEL_OPTIONS = [
    {"text": {"type": "plain_text", "text": "4/4 — Full"},  "value": "4/4"},
    {"text": {"type": "plain_text", "text": "3/4"},          "value": "3/4"},
    {"text": {"type": "plain_text", "text": "2/4 — Half"},   "value": "2/4"},
    {"text": {"type": "plain_text", "text": "1/4"},          "value": "1/4"},
    {"text": {"type": "plain_text", "text": "0/4 — Empty"},  "value": "0/4"},
]

def slack(endpoint, payload):
    r = requests.post(f"https://slack.com/api/{endpoint}", headers=HEADERS, json=payload, timeout=15)
    res = r.json()
    if not res.get("ok"): print(f"Slack error [{endpoint}]: {res.get('error')}")
    return res

def open_modal(trigger_id, modal): slack("views.open", {"trigger_id": trigger_id, "view": modal})

def post_msg(channel, blocks, text, ts=None):
    p = {"channel": channel, "text": text, "blocks": blocks}
    if ts: p["thread_ts"] = ts
    slack("chat.postMessage", p)

def val(state, block_id):
    try:
        action = state["values"][block_id]["value"]
        if isinstance(action, dict):
            if action.get("value") is not None:
                return action["value"]
            if action.get("selected_option"):
                return action["selected_option"]["value"]
            if action.get("selected_date"):
                return action["selected_date"]
        return str(action) if action else "—"
    except: return "—"

def verify(body, ts, sig):
    try:
        if abs(time.time() - int(ts)) > 300: return False
        base = f"v0:{ts}:{body.decode()}"
        comp = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode(), base.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(comp, sig)
    except: return False

def delivery_modal(b, trigger, ch, ts):
    open_modal(trigger, {"type":"modal","callback_id":"delivery_submit",
        "private_metadata": json.dumps({"channel":ch,"ts":ts,"booking":b}),
        "title":{"type":"plain_text","text":"Vehicle Delivered"},
        "submit":{"type":"plain_text","text":"Submit"},
        "close":{"type":"plain_text","text":"Cancel"},
        "blocks":[
            {"type":"section","text":{"type":"mrkdwn","text":f"*{b.get('id','—')}* | {b.get('car','—')} | {b.get('date','—')} {b.get('time','')}"}},
            {"type":"divider"},
            {"type":"input","block_id":"driver_name","label":{"type":"plain_text","text":"Driver Name"},
             "element":{"type":"plain_text_input","action_id":"value","placeholder":{"type":"plain_text","text":"e.g. Ahmed"}}},
            {"type":"input","block_id":"out_km","label":{"type":"plain_text","text":"Out KM"},
             "element":{"type":"plain_text_input","action_id":"value","placeholder":{"type":"plain_text","text":"e.g. 12500"}}},
            {"type":"input","block_id":"fuel_level","label":{"type":"plain_text","text":"Fuel Level"},
             "element":{"type":"static_select","action_id":"value","placeholder":{"type":"plain_text","text":"Select fuel level"},"options":FUEL_OPTIONS}},
            {"type":"input","block_id":"photos_uploaded","label":{"type":"plain_text","text":"Photos Uploaded"},
             "element":{"type":"static_select","action_id":"value","placeholder":{"type":"plain_text","text":"Select"},
             "options":[{"text":{"type":"plain_text","text":"Yes"},"value":"Yes"},{"text":{"type":"plain_text","text":"No"},"value":"No"}]}},
            {"type":"input","block_id":"remarks","label":{"type":"plain_text","text":"Remarks"},"optional":True,
             "element":{"type":"plain_text_input","action_id":"value","multiline":True,"placeholder":{"type":"plain_text","text":"Optional"}}},
        ]})

def pickup_modal(b, trigger, ch, ts):
    open_modal(trigger, {"type":"modal","callback_id":"pickup_submit",
        "private_metadata": json.dumps({"channel":ch,"ts":ts,"booking":b}),
        "title":{"type":"plain_text","text":"Contract Closed"},
        "submit":{"type":"plain_text","text":"Submit"},
        "close":{"type":"plain_text","text":"Cancel"},
        "blocks":[
            {"type":"section","text":{"type":"mrkdwn","text":f"*{b.get('id','—')}* | {b.get('car','—')} | Driver: {b.get('driver','—')} | Out KM: {b.get('out_km','—')}"}},
            {"type":"divider"},
            {"type":"input","block_id":"in_km","label":{"type":"plain_text","text":"In KM"},
             "element":{"type":"plain_text_input","action_id":"value","placeholder":{"type":"plain_text","text":"e.g. 12850"}}},
            {"type":"input","block_id":"extra_km","label":{"type":"plain_text","text":"Extra KM (if any)"},"optional":True,
             "element":{"type":"plain_text_input","action_id":"value","placeholder":{"type":"plain_text","text":"e.g. 350"}}},
            {"type":"input","block_id":"salik","label":{"type":"plain_text","text":"Salik"},"optional":True,
             "element":{"type":"plain_text_input","action_id":"value","placeholder":{"type":"plain_text","text":"e.g. AED 50"}}},
            {"type":"input","block_id":"fines","label":{"type":"plain_text","text":"Fines"},"optional":True,
             "element":{"type":"plain_text_input","action_id":"value","placeholder":{"type":"plain_text","text":"e.g. AED 0"}}},
            {"type":"input","block_id":"fuel_charge","label":{"type":"plain_text","text":"Fuel Charge"},"optional":True,
             "element":{"type":"plain_text_input","action_id":"value","placeholder":{"type":"plain_text","text":"e.g. AED 0"}}},
            {"type":"input","block_id":"damage_charges","label":{"type":"plain_text","text":"Damage Charges"},"optional":True,
             "element":{"type":"plain_text_input","action_id":"value","placeholder":{"type":"plain_text","text":"e.g. AED 0"}}},
            {"type":"input","block_id":"amount_collected","label":{"type":"plain_text","text":"Amount Collected"},
             "element":{"type":"plain_text_input","action_id":"value","placeholder":{"type":"plain_text","text":"e.g. AED 2,143"}}},
            {"type":"input","block_id":"payment_mode","label":{"type":"plain_text","text":"Payment Mode"},
             "element":{"type":"static_select","action_id":"value","placeholder":{"type":"plain_text","text":"Select"},
             "options":[{"text":{"type":"plain_text","text":"Cash"},"value":"Cash"},{"text":{"type":"plain_text","text":"Card"},"value":"Card"},{"text":{"type":"plain_text","text":"Bank Transfer"},"value":"Bank Transfer"}]}},
            {"type":"input","block_id":"remarks","label":{"type":"plain_text","text":"Remarks"},"optional":True,
             "element":{"type":"plain_text_input","action_id":"value","multiline":True,"placeholder":{"type":"plain_text","text":"Optional"}}},
        ]})

def extension_modal(b, trigger, ch, ts):
    open_modal(trigger, {"type":"modal","callback_id":"extension_submit",
        "private_metadata": json.dumps({"channel":ch,"ts":ts,"booking":b}),
        "title":{"type":"plain_text","text":"Contract Extension"},
        "submit":{"type":"plain_text","text":"Submit"},
        "close":{"type":"plain_text","text":"Cancel"},
        "blocks":[
            {"type":"section","text":{"type":"mrkdwn","text":f"*{b.get('id','—')}* | {b.get('car','—')} | {b.get('date','—')}"}},
            {"type":"context","elements":[{"type":"mrkdwn","text":"_Other charges (Salik, Fines, KM, Damage) will be captured at final Pickup_"}]},
            {"type":"divider"},
            {"type":"input","block_id":"driver_name","label":{"type":"plain_text","text":"Driver Name"},
             "element":{"type":"plain_text_input","action_id":"value","placeholder":{"type":"plain_text","text":"e.g. Ahmed"}}},
            {"type":"input","block_id":"new_return_date","label":{"type":"plain_text","text":"New Return Date"},
             "element":{"type":"datepicker","action_id":"value","placeholder":{"type":"plain_text","text":"Select new return date"}}},
            {"type":"input","block_id":"extension_payment_mode","label":{"type":"plain_text","text":"Extension Payment Mode"},
             "element":{"type":"static_select","action_id":"value","placeholder":{"type":"plain_text","text":"Select payment mode"},
             "options":[{"text":{"type":"plain_text","text":"Cash"},"value":"Cash"},{"text":{"type":"plain_text","text":"Card"},"value":"Card"},{"text":{"type":"plain_text","text":"Bank Transfer"},"value":"Bank Transfer"}]}},
            {"type":"input","block_id":"remarks","label":{"type":"plain_text","text":"Remarks"},"optional":True,
             "element":{"type":"plain_text_input","action_id":"value","multiline":True,"placeholder":{"type":"plain_text","text":"Optional"}}},
        ]})

def handle_delivery(payload):
    meta    = json.loads(payload["view"]["private_metadata"])
    state   = payload["view"]["state"]
    user    = payload["user"]["name"]
    booking = meta["booking"]
    driver  = val(state, "driver_name")
    out_km  = val(state, "out_km")
    booking.update({"driver": driver, "out_km": out_km})
    post_msg(meta["channel"], [
        {"type":"section","text":{"type":"mrkdwn","text":(
            f"✅ *DELIVERY COMPLETED*\n```\n"
            f"{'Driver':<14}: {driver}\n"
            f"{'Out KM':<14}: {out_km}\n"
            f"{'Fuel Level':<14}: {val(state,'fuel_level')}\n"
            f"{'Photos':<14}: {val(state,'photos_uploaded')}\n"
            f"{'Remarks':<14}: {val(state,'remarks')}\n```"
        )}},
        {"type":"divider"},
        {"type":"actions","elements":[
            {"type":"button","text":{"type":"plain_text","text":"🔑  Pickup"},"style":"primary","action_id":"open_pickup","value":json.dumps(booking)},
            {"type":"button","text":{"type":"plain_text","text":"📋  Extension"},"action_id":"open_extension","value":json.dumps(booking)},
        ]},
        {"type":"context","elements":[{"type":"mrkdwn","text":f"Submitted by @{user} | Pickup: PENDING"}]},
    ], f"✅ Delivery completed by {user}", meta["ts"])

def handle_pickup(payload):
    meta  = json.loads(payload["view"]["private_metadata"])
    state = payload["view"]["state"]
    user  = payload["user"]["name"]
    post_msg(meta["channel"], [
        {"type":"section","text":{"type":"mrkdwn","text":(
            f"✅ *CONTRACT CLOSED*\n```\n"
            f"{'In KM':<16}: {val(state,'in_km')}\n"
            f"{'Extra KM':<16}: {val(state,'extra_km')}\n"
            f"{'Salik':<16}: {val(state,'salik')}\n"
            f"{'Fines':<16}: {val(state,'fines')}\n"
            f"{'Fuel Charge':<16}: {val(state,'fuel_charge')}\n"
            f"{'Damage':<16}: {val(state,'damage_charges')}\n"
            f"{'Amt Collected':<16}: {val(state,'amount_collected')}\n"
            f"{'Payment Mode':<16}: {val(state,'payment_mode')}\n"
            f"{'Remarks':<16}: {val(state,'remarks')}\n```\n"
            f"*CONTRACT CLOSED — NO FURTHER ACTION REQUIRED*"
        )}},
        {"type":"context","elements":[{"type":"mrkdwn","text":f"Submitted by @{user}"}]},
    ], f"✅ Contract closed by {user}", meta["ts"])

def handle_extension(payload):
    meta     = json.loads(payload["view"]["private_metadata"])
    state    = payload["view"]["state"]
    user     = payload["user"]["name"]
    booking  = meta["booking"]
    driver   = val(state, "driver_name")
    new_date = val(state, "new_return_date")
    payment  = val(state, "extension_payment_mode")
    remarks  = val(state, "remarks")
    post_msg(meta["channel"], [
        {"type":"section","text":{"type":"mrkdwn","text":(
            f"📋 *CONTRACT EXTENDED*\n```\n"
            f"{'Driver':<20}: {driver}\n"
            f"{'New Return Date':<20}: {new_date}\n"
            f"{'Payment Mode':<20}: {payment}\n"
            f"{'Remarks':<20}: {remarks}\n```\n"
            f"*CONTRACT ACTIVE — EXTENDED | Final Pickup: PENDING*"
        )}},
        {"type":"divider"},
        {"type":"actions","elements":[
            {"type":"button","text":{"type":"plain_text","text":"🔑  Final Pickup"},
             "style":"primary","action_id":"open_pickup","value":json.dumps(booking)},
        ]},
        {"type":"context","elements":[{"type":"mrkdwn",
            "text":f"Extended by @{user} | Driver: {driver} | New Return: {new_date} | Payment: {payment}"}]},
    ], f"📋 Contract extended by {user}", meta["ts"])

# ── HTTP Handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, f, *a): print(f"[{self.address_string()}] {f%a}")

    def send_json(self, code=200, body=b""):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        print(f"GET {self.path}")
        self.send_json(200, b'{"status":"ok","service":"MKV Slack App"}')

    def do_POST(self):
        n    = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        ts   = self.headers.get("X-Slack-Request-Timestamp", "0")
        sig  = self.headers.get("X-Slack-Signature", "")
        print(f"POST {self.path} [{n} bytes]")

        # URL verification challenge
        try:
            jb = json.loads(body.decode())
            if jb.get("type") == "url_verification":
                print("URL verification OK")
                self.send_json(200, json.dumps({"challenge": jb["challenge"]}).encode())
                return
        except: pass

        if not verify(body, ts, sig):
            print("Sig failed")
            self.send_json(401, b'{"error":"invalid_signature"}')
            return

        raw     = parse_qs(body.decode())
        payload = json.loads(unquote_plus(raw.get("payload", ["{}"])[0]))
        ptype   = payload.get("type")
        print(f"Type: {ptype}")

        if ptype == "block_actions":
            aid     = payload["actions"][0]["action_id"]
            trigger = payload["trigger_id"]
            ch      = payload["container"]["channel_id"]
            mts     = payload["container"]["message_ts"]
            try: bk = json.loads(payload["actions"][0].get("value", "{}"))
            except: bk = {}
            print(f"Action: {aid}")
            if aid == "open_delivery":   delivery_modal(bk, trigger, ch, mts)
            elif aid == "open_pickup":   pickup_modal(bk, trigger, ch, mts)
            elif aid == "open_extension": extension_modal(bk, trigger, ch, mts)
            self.send_json(200, b"")

        elif ptype == "view_submission":
            cb = payload["view"]["callback_id"]
            print(f"Submit: {cb}")
            if cb == "delivery_submit":   handle_delivery(payload)
            elif cb == "pickup_submit":   handle_pickup(payload)
            elif cb == "extension_submit": handle_extension(payload)
            self.send_json(200, b'{"response_action":"clear"}')
        else:
            self.send_json(200, b"")

if __name__ == "__main__":
    print(f"MKV Slack App starting on 0.0.0.0:{PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
