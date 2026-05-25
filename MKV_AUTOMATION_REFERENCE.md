# MKV Luxury — Automation Master Reference
**Last Updated:** 26 May 2026  
**Purpose:** Complete troubleshooting & configuration reference for all automation scripts

---

## 1. SYSTEM OVERVIEW

All automation runs via **GitHub Actions** (cloud) triggered by **cron-job.org**.  
Meta daily report runs locally on **your PC** via `mkv_meta_local.py`.

```
cron-job.org → GitHub Actions (keepalive.yml) → Python scripts → Slack
                                                         ↑
                                          Meta token from meta_token.txt (local PC)
```

---

## 2. SLACK CHANNELS

| Channel Name | Channel ID | Used By |
|---|---|---|
| #mkv-marketing-team | C0AASQKLY59 | Ads reports, Weekly optimizer (LIVE) |
| #mkv-daily-lead-report | C0ABN1ZKSGN | Gallabox leads report |
| #mkv-test-automation | C0B0TGBDCDU | All test/dev runs |

---

## 3. SCRIPTS & WHAT THEY DO

| Script | Purpose | Runs On |
|---|---|---|
| `mkv_ads_api_report.py` | Daily Google Ads + Meta snapshot | GitHub Actions daily 1:30 PM Dubai |
| `mkv_gallabox_snapshot.py` | Daily WhatsApp leads by source/agent/stage | GitHub Actions daily 11:00 AM Dubai |
| `mkv_weekly_optimizer.py` | Monday 7-day Google+Meta+Gallabox correlation + actions | GitHub Actions every Monday 8:00 AM Dubai |
| `mkv_meta_local.py` | Meta daily report (MKV Luxury + Lease to Own) | Local PC — auto-triggers when meta_token.txt changes |
| `mkv_fleet_availability.py` | Fleet availability snapshot | GitHub Actions daily 10:30 AM Dubai |
| `mkv_booking_alert.py` | Booking alerts | GitHub Actions daily 12:45 PM Dubai |
| `mkv_pickup_alert.py` | Pickup & delivery alerts | GitHub Actions daily 7:00 PM Dubai |
| `mkv_website_monitor.py` | Website snapshot | GitHub Actions daily 11:45 AM Dubai |

---

## 4. GITHUB ACTIONS — keepalive.yml

**File location:** `.github/workflows/keepalive.yml`  
**All jobs in ONE file — never split into multiple files**

### Job Names & Manual Triggers

| Job ID | Description | Cron Schedule |
|---|---|---|
| `fleet-availability` | Fleet snapshot | Manual only |
| `daily-leads` | Gallabox leads | Manual only |
| `website-snapshot` | Website monitor | Manual only |
| `pickup-alert` | Pickup alerts | Manual only |
| `booking-bot` | Booking alerts | Manual only |
| `ads-snapshot` | Daily ads report | `30 9 * * *` (1:30 PM Dubai) |
| `weekly-optimizer` | Weekly optimizer | `0 4 * * 1` (8:00 AM Dubai Mon) |

### Manual Trigger (GitHub UI)
Actions → Keep Alive → Run workflow → select job → Run workflow  
- `test_mode: true` → posts to #mkv-test-automation  
- `test_mode: false` → posts to #mkv-marketing-team (default for cron)

### ⚠️ CRITICAL RULE
**One `name:`, one `on:`, one `jobs:` per yml file.**  
Never paste a second workflow block at the bottom — that causes:  
`'name' is already defined, 'on' is already defined, 'jobs' is already defined`

---

## 5. CRON-JOB.ORG SCHEDULE

All jobs triggered via cron-job.org → GitHub API dispatches

| Title | Time | Frequency |
|---|---|---|
| MKV Fleet Availability | 10:30 AM | Daily |
| Daily Leads Report | 11:00 AM | Daily |
| Website Report | 11:45 AM | Daily |
| MKV Pickup Alert | 7:00 PM | Daily |
| MKV Bookings | 12:45 PM | Daily |
| MKV Ads Snapshot | 8:00 AM | Daily |
| **MKV Weekly Optimizer** | **8:00 AM** | **Every Monday** |

### Cron-job.org Headers (same for all jobs)
- **Accept:** `application/vnd.github.v3+json`
- **Authorization:** `Bearer <your GitHub token>`
- **Content-Type:** `application/json`

### Weekly Optimizer POST body:
```json
{
  "ref": "main",
  "inputs": {
    "job": "weekly",
    "test_mode": "false"
  }
}
```

---

## 6. GITHUB SECRETS

| Secret Name | What It Is | Used By |
|---|---|---|
| `SLACK_BOT_TOKEN` | Slack bot token | All scripts |
| `GOOGLE_ADS_DEVELOPER_TOKEN` | Google Ads API | Ads report, Weekly optimizer |
| `GOOGLE_ADS_CLIENT_ID` | Google Ads OAuth | Ads report, Weekly optimizer |
| `GOOGLE_ADS_CLIENT_SECRET` | Google Ads OAuth | Ads report, Weekly optimizer |
| `GOOGLE_ADS_REFRESH_TOKEN` | Google Ads OAuth | Ads report, Weekly optimizer |
| `GOOGLE_ADS_CUSTOMER_ID` | `3847584613` | Ads report, Weekly optimizer |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | Google Ads MCC | Ads report, Weekly optimizer |
| `GALLABOX_ACCOUNT_ID` | `66e3f05033e71154d5fdd76c` | Leads, Weekly optimizer |
| `GALLABOX_API_KEY` | `6a1064ed5a8546db4ab5870b` | Leads, Weekly optimizer |
| `GALLABOX_API_SECRET` | `e9e9903954a645f3adf7be9a86d7a4d2` | Leads, Weekly optimizer |
| `META_TOKEN` | Contents of meta_token.txt | Weekly optimizer |
| `GMAIL_ADDRESS` | `shahbazmkv@gmail.com` | Ads report |
| `GMAIL_APP_PASSWORD` | 16-char Gmail app password | Ads report |
| `APPIC_KEY` | Fleet/booking API key | Fleet, Bookings, Pickup |
| `WEBHOOK_LEADS` | Leads webhook | Gallabox |

---

## 7. META ADS CONFIGURATION

### Accounts
| Account | ID |
|---|---|
| MKV Luxury | `699611181993619` |
| Lease to Own | `900731551390821` |

### Meta App (MKV Ads Bot)
| | |
|---|---|
| App ID | `1492858999005557` |
| App Secret | `64853acc953dbe9fd94a27e493988ee8` |

### Token Management
- Token file: `meta_token.txt` (local PC only — never commit to GitHub)
- Token type: **60-day long-lived token**
- **Current token expires: 23 July 2026**
- **Refresh reminder: 16 July 2026**
- Script: `refresh_token.bat`

### Token Refresh Process
1. Run `refresh_token.bat`
2. Opens Graph API Explorer → select MKV Ads Bot app
3. Permissions needed: `ads_read`, `ads_management`, `read_insights`
4. Copy short token → bat exchanges it for 60-day token automatically
5. **Paste token directly into Notepad** → save as `meta_token.txt`  
   *(do NOT use bat paste prompt — special characters get corrupted)*
6. After refresh → also update `META_TOKEN` GitHub Secret

### ⚠️ Token Troubleshooting
| Error | Cause | Fix |
|---|---|---|
| `Invalid OAuth access token - Cannot parse` (code 190) | Token corrupted/truncated when pasted via bat | Paste directly into Notepad |
| `Got unexpected null` (code 190) | Token expired | Run refresh_token.bat |
| `Invalid Client ID` (code 101) | Wrong App ID in exchange URL | Use App ID: 1492858999005557 |
| `No data today` in Slack | Token fired too fast after save | Increase sleep from 2s to 30s in mkv_meta_local.py |

### Test token validity:
```
https://graph.facebook.com/v20.0/act_699611181993619/insights?fields=spend,clicks&date_preset=yesterday&access_token=YOUR_TOKEN
```

---

## 8. GALLABOX CONFIGURATION

| | |
|---|---|
| Account ID | `66e3f05033e71154d5fdd76c` |
| API Key | `6a1064ed5a8546db4ab5870b` |
| API Secret | `e9e9903954a645f3adf7be9a86d7a4d2` |
| Base URL | `https://server.gallabox.com/devapi/accounts/` |

### WhatsApp Channels
| Channel Name | Channel ID |
|---|---|
| MKV Luxury Main | `675a90ddda3020e52915beff` |
| MKV Luxury Car Rental | `66e930025e9ef7252ccc8a25` |
| Rent to Own | `699d8cca452cc56936e21e45` |

### Lead Source → Ad Channel Mapping
| Gallabox Source | Maps To |
|---|---|
| Google Ads | google |
| Facebook / Instagram | meta |
| Instagram DMs | meta |
| OneClickDrive | organic |
| Website | organic |

---

## 9. WEEKLY OPTIMIZER — HOW IT WORKS

Posts every Monday 8:00 AM Dubai to **#mkv-marketing-team** — 3 separate Slack messages:

**Post 1 — 🔵 Google Ads Weekly**
- 7-day spend, clicks, conversions, CTR, CPC, CPA
- Top campaigns (up to 5)
- Top search terms (up to 5)
- Google WA leads from Gallabox + Cost per Lead

**Post 2 — 🟦 Meta Ads Weekly**
- MKV Luxury 7-day + Lease to Own 7-day
- Top campaigns, placement breakdown
- Meta WA leads from Gallabox + Cost per Lead
- Audience fatigue warning if frequency ≥ 3.0
- Token expiry warning if < 7 days left

**Post 3 — 📊 Correlation & Actions**
- Channel correlation table (Google vs Meta vs Organic)
- Blended CPL, lead quality %, best channel winner
- Gallabox 7-day breakdown (by source, stage, top agents)
- Up to 7 ranked optimization actions

### WoW Delta Store
Week-over-week comparisons stored in `mkv_weekly_store.json`  
Persisted between Monday runs via GitHub Actions cache.

---

## 10. TROUBLESHOOTING QUICK REFERENCE

### Slack "not_in_channel" error
```
/invite @MKVReservationBot
```
Add bot to the relevant channel.

### GitHub Actions yml parse error
`'name' is already defined` → you have two workflow blocks in one file.  
Fix: keep only ONE `name:`, ONE `on:`, ONE `jobs:` at the top level.

### Meta "No data today" in Slack
1. Check token validity (browser test above)
2. If token valid → increase sleep delay in mkv_meta_local.py line: `time.sleep(2)` → `time.sleep(30)`
3. If token invalid → run refresh_token.bat

### Google Ads "Zero conversions" in report
- Check conversion tracking tags on landing pages
- Verify Google Tag Manager is firing on thank-you/confirmation pages

### Gallabox leads showing zero
- API may be rate-limited — check Gallabox dashboard
- Verify channel IDs haven't changed

### Weekly optimizer "Meta data unavailable"
- META_TOKEN GitHub Secret is expired or not set
- Update secret with current meta_token.txt contents

---

## 11. FILE LOCATIONS (Local PC)

```
C:\Users\shahb\Desktop\MKV Ads Bot\
├── mkv_meta_local.py
├── meta_token.txt          ← never share/commit this
├── refresh_token.bat       ← never share/commit this
└── mkv_weekly_store.json   ← auto-created
```

---

## 12. KEY DATES & REMINDERS

| Date | Action |
|---|---|
| **16 July 2026** | Run refresh_token.bat — get new Meta token |
| **23 July 2026** | Current Meta token expires |
| After each refresh | Update META_TOKEN in GitHub Secrets |

---

## 13. PERFORMANCE BENCHMARKS (Luxury Automotive Dubai)

| Metric | Target | Alert Threshold |
|---|---|---|
| Google CTR | 2–4% | < 1.5% |
| Google CPA | < AED 100 | > AED 300 |
| Meta CPR | < AED 50 | > AED 150 |
| Meta Frequency | < 2.5 | ≥ 3.0 (creative fatigue) |
| Cost per WA Lead | < AED 80 | > AED 150 |
| Lead Quality Rate | > 40% | < 30% |
