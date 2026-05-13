# MKV Luxury — Automated Operations System

Fully cloud-based automation for MKV Car Rental.
Runs via GitHub Actions + Slack interactive app — **no PC required.**

---

## Scripts & Schedules

| Script | Schedule | Channel | Purpose |
|---|---|---|---|
| `mkv_booking_bot.py` | Every 15 min | `#mkv-bookings` | New bookings, documents, extensions |
| `mkv_fleet_availability.py` | 10:00 AM daily | `#mkv-test-automation` | Fleet availability + daily delivery/return |
| `mkv_gallabox_snapshot.py` | 11:00 AM daily | `#mkv-daily-lead-report` | Leads snapshot |
| `mkv_website_monitor.py` | 11:30 AM daily | `#mkv-test-automation` | Website uptime |
| `mkv_pickup_alert.py` | 7:00 PM daily | `#mkv-schedule-for-delivery` + `#mkv-car-pickup` | Tomorrow's delivery + pickup alerts |
| `mkv_daily_snapshot.py` | Morning daily | `#mkv-test-automation` | Executive snapshot (leads, fleet, website, Trustpilot) |
| `slack_app.py` | Always on (server) | — | Handles delivery + pickup modal submissions |

---

## Slack Channels

| Channel | ID | Role |
|---|---|---|
| `#mkv-bookings` | `C0ABPC606F7` | ROOT — new bookings, documents, extensions |
| `#mkv-delivery` | `C0ACB9C8J01` | Delivery completed cards + 🔑 Pickup button |
| `#mkv-car-pickup` | `C0ABW979FML` | Contract closed cards + 7PM pickup alerts |
| `#mkv-schedule-for-delivery` | — | 7PM delivery alerts |
| `#mkv-test-automation` | `C0B0TGBDCDU` | Fleet availability + executive snapshot |
| `#mkvtest` | `C0AVCCCG0S0` | Testing only |

---

## Booking Flow

### New Booking (`mkv_booking_bot.py` — every 15 min)
1. Detects new bookings from Appic API (`get-mkv-bookings.php`)
2. Posts booking card to `#mkv-bookings` with 🚗 Delivery button
3. Fetches documents via checkin/out API → posts Passport, Licence, Emirates ID in thread
4. Auto-detects end date changes → posts extension note in thread
5. Stores thread timestamps in `booking_thread_store.json`

### Delivery (`slack_app.py`)
- Staff clicks 🚗 Delivery → modal opens with Driver Name, Delivery Time (GCC 24h auto), Out KM, Fuel, Photos, Remarks
- On submit → posts DELIVERY COMPLETED card to `#mkv-delivery` with 🔑 Pickup button

### Pickup (`slack_app.py`)
- Staff clicks 🔑 Pickup → modal opens showing AGR#, Out KM, Delivered Time in header
- Driver fills: In KM, In Time (GCC 24h auto), Salik, Fines, Fuel Charge, Damage, Amt Collected, Payment Mode
- KM Driven auto-calculated (In KM − Out KM)
- On submit → posts CONTRACT CLOSED card to `#mkv-car-pickup`

### Scheduled Alerts (`mkv_pickup_alert.py` — 7PM)
- Delivery alert → tomorrow's `startDate` contracts → `#mkv-schedule-for-delivery` with thread link to `#mkv-bookings`
- Pickup alert → tomorrow's `endDate` contracts → `#mkv-car-pickup` with thread link to `#mkv-bookings`

---

## Fleet Availability (`mkv_fleet_availability.py` — 10AM)

### Data Sources
| Data | Appic API |
|---|---|
| Fleet counts (STR/Lease/LTR/Service) | `get-mkv-vehicle-assignments.php` |
| Available car list | `get-mkv-available-vehicle.php` per plate |
| Next booking date per plate | `get-mkv-bookings.php` (next 90 days) |
| To be delivered today | `get-mkv-bookings.php` (startDate = today) |
| To be returned today | `get-mkv-bookings.php` (endDate = today) |

### Card Output
```
📋 MKV Fleet Availability — DD Mon YYYY
─────────────────────────────────────────
[Total Fleet] [Lease] [Long-term] [Short-term STR] [Available]

✅ AVAILABLE CARS (N)
• Car Name  PLATE  · Next: DD Mon YYYY
• Car Name  PLATE  · No upcoming booking

🚗 TO BE DELIVERED TODAY (N)
• Car  PLATE  · Customer  · Time

🔑 TO BE RETURNED TODAY (N)
• Car  PLATE  · Customer  · Due Time
```

### Fleet Mismatch Alert
Every run compares `TOTAL_FLEET` (hardcoded = 63) against Appic assignments total.
If different → posts `⚠️ FLEET COUNT MISMATCH DETECTED` alert before the main card.

### When Fleet Changes
| Event | Alert | Action needed in script |
|---|---|---|
| Car added to Appic | ⚠️ Next 10AM | Add plate to `MASTER_PLATES` · increment `TOTAL_FLEET` |
| Car removed/sold | ⚠️ Next 10AM | Remove plate from `MASTER_PLATES` · decrement `TOTAL_FLEET` |
| Car moved to Lease/LTR | None | No change — assignments API handles counts |
| Car in service | None | No change — availability API marks it unavailable |

---

## VAT Calculation Logic

Appic returns VAT-inclusive amounts. Card displays:

```
Rental        : AED X,XXX   ← VAT-inclusive from Appic (amount field)
Zero Deposit  : AED XXX     ← zeroDepositFee
Add-ons       : AED XXX     ← addOnCharges (if any)
──────────────────────────────────────
Total w/o VAT : AED X,XXX   ← amountWithoutVat from Appic
VAT 5%        : AED XXX     ← vatAmount from Appic
Grand Total   : AED X,XXX   ← grandTotal from Appic
──────────────────────────────────────
Advance       : AED XXX     ← advanceReceived
Balance       : AED XXX     ← grandTotal − advance
Payment Mode  : Cash
KM Allowed    : XXX KM      ← parsed from remarks (X KM PER DAY × duration)
```

---

## Key Config

| Variable | Location | Value |
|---|---|---|
| `TOTAL_FLEET` | `mkv_fleet_availability.py` line 29 | `62` ← update when fleet changes |
| `MASTER_PLATES` | `mkv_fleet_availability.py` lines 32–46 | 62 plates |
| `TEST_MODE` | `mkv_booking_bot.py`, `mkv_pickup_alert.py` | `False` (live) |
| `SEED_MODE` | `mkv_booking_bot.py` inside `main()` | `False` (live) |
| `CHANNEL_PICKUP` | `slack_app.py` line 17 | `C0ABW979FML` |
| `CHANNEL_DELIVERY` | `slack_app.py` line 16 | `C0ACB9C8J01` |

---

## GitHub Secrets Required

| Secret | Purpose |
|---|---|
| `SLACK_BOT_TOKEN` | All Slack API calls |
| `SLACK_SIGNING_SECRET` | Slack app signature verification |
| `APPIC_KEY` | `96QQYxPRVRTiHjL0tEmgP0cr5FkLvED0` |
| `WEBHOOK_DELIVERY` | Scheduled delivery alert webhook |
| `WEBHOOK_PICKUP` | Scheduled pickup alert webhook |
| `GALLABOX_API_KEY` | Leads snapshot |
| `GALLABOX_API_SECRET` | Leads snapshot |
| `GALLABOX_ACCOUNT_ID` | Leads snapshot |
| `CLAUDE_API_KEY` | Executive narrative in daily snapshot |

---

## Notes
- All scripts run entirely in the cloud — no local PC needed
- `slack_app.py` must be running on a server (Railway / Render) with the Slack app configured
- Threading fix in `slack_app.py` ensures pickup modal posts within Slack's 3-second timeout
- `booking_thread_store.json` is the source of truth for thread timestamps and booking state
- Last verified: May 2026

