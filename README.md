# MKV Pickup Alert — GitHub Actions

Automated daily Slack alert for tomorrow's vehicle deliveries and returns.
Runs at **7:00 PM Dubai Time** every day via GitHub Actions — no PC required.

---

## Slack Channels
| Alert | Channel |
|---|---|
| Deliveries | #mkv-schedule-for-delivery |
| Returns / Pickups | #mkv-car-pickup |

---

## Setup (One Time)

### 1. Create GitHub Repo
- Go to github.com → New repository
- Name: `mkv-pickup-alert`
- Private ✅
- Upload all files from this folder

### 2. Add GitHub Secrets
Go to: **Settings → Secrets and variables → Actions → New repository secret**

| Secret Name | Value |
|---|---|
| `APPIC_KEY` | `96QQYxPRVRTiHjL0tEmgP0cr5FkLvED0` |
| `WEBHOOK_DELIVERY` | `https://hooks.slack.com/services/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` |
| `WEBHOOK_PICKUP` | `https://hooks.slack.com/services/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` |

### 3. Enable Actions
- Go to **Actions** tab in the repo
- Click **"I understand my workflows, go ahead and enable them"**

### 4. Test Manually
- Go to **Actions → MKV Pickup Alert → Run workflow**
- Check Slack for the alert

---

## Schedule
- Runs daily at `15:00 UTC` = `19:00 Dubai Time (GST)`
- Can be triggered manually anytime from the Actions tab

---

## Files
```
mkv-pickup-alert/
├── .github/workflows/pickup_alert.yml   ← Scheduler
├── mkv_pickup_alert.py                  ← Main script
├── requirements.txt                     ← Python deps
└── README.md
```
