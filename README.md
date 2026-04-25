# MKV Automation — GitHub Actions

Fully cloud-based automation for MKV Car Rental.
Runs daily via GitHub Actions — **no PC required.**

---

## Workflows

| Workflow | Schedule | Channel |
|---|---|---|
| 📊 Daily Leads Report | 11:00 AM Dubai | #mkv-daily-lead-report |
| 🚗 Pickup Alert | 7:00 PM Dubai | #mkv-schedule-for-delivery + #mkv-car-pickup |

---

## Files

```
mkv-pickup-alert/
├── .github/
│   └── workflows/
│       ├── gallabox_snapshot.yml   ← Leads report (11 AM)
│       └── pickup_alert.yml        ← Delivery + pickup alert (7 PM)
├── mkv_gallabox_snapshot.py        ← Leads script
├── mkv_pickup_alert.py             ← Pickup/delivery script
├── requirements.txt
└── README.md
```

---

## One-Time Setup

### Step 1 — Create GitHub Repo
- Go to github.com → New repository
- Name: `mkv-pickup-alert`
- Set to **Private** ✅
- Upload all files (keep folder structure intact)

### Step 2 — Add GitHub Secrets
Go to: **Settings → Secrets and variables → Actions → New repository secret**

Add all 7 secrets below:

| Secret Name | Value |
|---|---|
| `APPIC_KEY` | `96QQYxPRVRTiHjL0tEmgP0cr5FkLvED0` |
| `WEBHOOK_DELIVERY` | `https://hooks.slack.com/services/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` |
| `WEBHOOK_PICKUP` | `https://hooks.slack.com/services/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` |
| `GALLABOX_API_KEY` | `69e7694e2da59f609317986b` |
| `GALLABOX_API_SECRET` | `984394d316324482a8615eba6742b3ab` |
| `GALLABOX_ACCOUNT_ID` | `66e3f05033e71154d5fdd76c` |
| `WEBHOOK_LEADS` | `https://hooks.slack.com/services/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` |

### Step 3 — Enable Actions
- Go to **Actions** tab in the repo
- Click **"I understand my workflows, go ahead and enable them"**

### Step 4 — Test Both Manually
- Actions → **MKV Daily Leads Report** → Run workflow → check #mkv-daily-lead-report
- Actions → **MKV Pickup Alert** → Run workflow → check #mkv-schedule-for-delivery + #mkv-car-pickup

---

## Schedule Reference (UTC vs Dubai)
| Workflow | UTC Cron | Dubai Time |
|---|---|---|
| Leads Report | `0 7 * * *` | 11:00 AM |
| Pickup Alert | `0 15 * * *` | 7:00 PM |

---

## Notes
- PC can be completely off — runs entirely in the cloud
- Gallabox MTD data recalculates fresh from month start each day
- Manual trigger available anytime from the Actions tab
