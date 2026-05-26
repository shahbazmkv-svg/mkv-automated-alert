

def upload_file_to_slack(channel, thread_ts, filename, content):
    try:
        r1 = requests.get(
            "https://slack.com/api/files.getUploadURLExternal",
            headers=SLACK_HEADERS,
            params={"filename": filename, "length": len(content)},
            timeout=10
        )
        d1 = r1.json()
        if not d1.get("ok"):
            print(f"  getUploadURL failed: {d1.get('error')}")
            return False

        upload_url = d1["upload_url"]
        file_id = d1["file_id"]

        r2 = requests.post(upload_url, data=content, timeout=30)
        if r2.status_code != 200:
            print(f"  Upload failed: {r2.status_code}")
            return False

        r3 = requests.post(
            "https://slack.com/api/files.completeUploadExternal",
            headers=SLACK_HEADERS,
            json={"files": [{"id": file_id}], "channel_id": channel, "thread_ts": thread_ts},
            timeout=10
        )
        d3 = r3.json()
        if not d3.get("ok"):
            print(f"  completeUpload failed: {d3.get('error')}")
            return False
        return True
    except Exception as e:
        print(f"  upload_file_to_slack error: {e}")
        return False

def post_documents(b, agr_no, customer, channel, thread_ts):
    doc_fields = [
        ("passportImg", "Passport"),
        ("passportExpImg", "Passport Expiry"),
        ("licenseImg", "Driving Licence"),
        ("licenseExpiryImg", "Licence Expiry"),
        ("tradeLicenseImg", "Trade Licence"),
        ("emiratesIdImg", "Emirates ID"),
        ("visaImg", "Visa"),
    ]
    docs = []
    for field, label in doc_fields:
        url = str(b.get(field) or "").strip()
        if url.startswith("http"):
            docs.append((label, url))

    if not docs:
        print(f"  No documents found for {agr_no}")
        return

    post_message(channel, [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*DOCUMENTS*\nAGR#: {agr_no} | {customer}"}}
    ], f"Documents: {agr_no}", thread_ts=thread_ts)

    for label, url in docs:
        try:
            print(f"  Downloading: {label}")
            r = requests.get(url, timeout=20)
            if r.status_code != 200:
                print(f"  Download failed ({r.status_code}): {url}")
                continue
            ct = r.headers.get("Content-Type", "")
            ext = ".pdf" if "pdf" in ct else ".png" if "png" in ct else ".jpg"
            filename = f"{agr_no}_{label.replace(' ', '_')}{ext}"
            ok = upload_file_to_slack(channel, thread_ts, filename, r.content)
            print(f"  Upload {'OK' if ok else 'FAILED'}: {filename}")
        except Exception as e:
            print(f"  Doc upload error {label}: {e}")

def fetch_bookings():
        f"{'Pickup':<14}: PENDING\n"
        f"```"
    )
    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "NEW BOOKING â€” MKV CAR RENTAL"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": f"Detected: {now_str}  |  Auto-alert via GitHub Actions"}]},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": body}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": "All updates will appear in this thread"}]},
    ]
        f"{'Pickup':<14}: PENDING\n"
        f"```"
    )
    booking_data = json.dumps({
        "id": f["agr_no"],
        "car": f"{f['vehicle']} [{f['plate']}]",
        "date": fmt_date(f["start"]),
        "time": f["s_time"],
        "location": f["location"],
        "driver": "",
        "out_km": "",
    })
    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "NEW BOOKING â€” MKV CAR RENTAL"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": f"Detected: {now_str}  |  Auto-alert via GitHub Actions"}]},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": body}},
        {"type": "divider"},
        {"type": "actions",
         "elements": [
             {"type": "button",
              "text": {"type": "plain_text", "text": "🚗  Delivery"},
              "style": "primary",
              "action_id": "open_delivery",
              "value": booking_data},
             {"type": "button",
              "text": {"type": "plain_text", "text": "🔑  Pickup"},
              "action_id": "open_pickup",
              "value": booking_data},
         ]},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": "All updates will appear in this thread"}]},
    ]
    )
    booking_data = json.dumps({
        "id": f["agr_no"],
        "car": f"{f['vehicle']} [{f['plate']}]",
        "date": fmt_date(f["start"]),
        "time": f["s_time"],
        "location": f["location"],
        "driver": "",
        "out_km": "",
    })
    blocks = [
         "text": {"type": "mrkdwn", "text": body}},
        {"type": "divider"},
        {"type": "actions",
         "elements": [
             {"type": "button",
              "text": {"type": "plain_text", "text": "🚗  Delivery"},
              "style": "primary",
              "action_id": "open_delivery",
              "value": booking_data},
             {"type": "button",
              "text": {"type": "plain_text", "text": "🔑  Pickup"},
              "action_id": "open_pickup",
              "value": booking_data},
         ]},
        {"type": "context",

                # Fetch and post documents in thread
                docs = fetch_documents(f["agr_no"], start, end)
                post_documents(TARGET_CHANNEL, ts, f["agr_no"], f["customer"], docs)
                # Fetch and post Appic customer documents in the booking thread.
                post_documents(b, f["agr_no"], f["customer"], TARGET_CHANNEL, ts)
