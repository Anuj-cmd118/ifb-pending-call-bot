# IFB Pending Call Bot

A WhatsApp bot that turns the portal's Excel reports into decision-ready images.
Built for the "To Branch with Love" project. Phase 1: one-to-one chats (no groups yet).

## What it does

Send an Excel file exported from the portal; the bot detects which report it is.

| File sent | Bot offers |
|---|---|
| **Pending call** | **Executive pivot** - executive x days-pending table, colour-coded<br>**Ageing summary** - executive x days 0..N, Grand Total and a ">N Days" column, both colour-scaled<br>**Chase list** - top 10 calls past SLA, ranked by priority<br>**By executive** - one executive's calls broken down franchise-wise |
| **Customer satisfaction score** | **CSS by executive** - HAPPY / NOT HAPPY / OK / Grand Total / CSS, worst first<br>**Executive's franchises** - pick an executive; franchise-wise CSS with a "Yet to get 95%" column<br>**Franchise scorecard** - the 10 lowest by % not happy |
| **Calls Registered** | Recognised; no output yet |

Type `hi` at any time to start over.

### Executive pivot
Asks the age ("more than 2 / 3 days" or a custom number) and whether to apply the
standard filters, then returns the pivot image with a red-yellow-green Grand Total column.

### Chase list
Asks which age counts as an SLA breach, then ranks calls by priority and shows:
ticket, franchise, executive, days, reason tag and flags (ESC1/ESC2/ESC3, REPEAT).
Reason tags: Part pending, Technician action, Revisit / rework, Customer unavailable,
Other, No reason given. Customer names are deliberately not shown.

### By executive
Asks the age, lists the executives who have calls past that age (tap one, or type part
of a name), and returns that executive's pending calls franchise-wise.

### Ageing summary
Asks "up to 3 / up to 4 days / custom" (N), then shows one column per day from 0 to N, a
Grand Total of all calls, then ">N-1 Days" and ">N Days" columns side by side (e.g. N=4 gives
">3 Days" and ">4 Days"). All three total columns are red-yellow-green colour-scaled (more calls = redder). Standard filters apply.

### CSS by executive
CSS = (HAPPY - NOT HAPPY) / Grand Total, where Grand Total includes OK. Executives are sorted
worst CSS first, with a red (low) to green (high) colour scale. The period is whatever
date range the exported file covers.

### Executive's franchises (franchise-wise CSS)
Pick an executive (or type a name); the report lists that executive's franchises with HAPPY, NOT HAPPY,
OK, Grand Total and CSS, best first (ties alphabetical). **Yet to get 95%** = extra HAPPY responses still
needed to reach 95% = 19 x Total - 20 x (HAPPY - NOT HAPPY); a negative number means the franchise is ahead.
CSS cells are green at or above the target and red below it. The target is `CSS_TARGET_PCT` in `insights.py`.

### CSAT scorecard
Per franchise: responses, happy %, OK and not-happy counts. Franchises with fewer than
20 responses are not ranked.

## Rules you can change

**Standard filters** (top of `report_engine.py`) - always ignored in the pending reports:
- Call types: COURTESY VISIT, DEALER DEFECTIVE REQUEST
- Pending reasons: "Request soft closure verification", "Cancellation Request verification"

**Chase list and CSAT** (top of `insights.py`):
- `SLA_DAYS = 3` - a call is breached when DAYS OLD is greater than this
- `CHASE_TOP_N = 10` - calls on the chase list
- `MIN_RESPONSES = 20` - minimum CSAT responses to rank a franchise
- Priority points: 1 per day (capped at 30), +5 past SLA, +10 Esc Level 1, +20 higher
  escalation, +8 repeat call, +5 no reason given

**Excluded executives** (`EXCLUDE_EXECUTIVES` at the top of `report_engine.py`): Darma Raju E,
Dantuluri Jayanth Varma and calls with no executive are left out of every executive-wise
report (pivot, ageing, chase list, by executive, CSS). Matching ignores case and spaces.

**Counting:** the portal repeats a ticket once per spare-part line, so each ticket is
counted once. A call is a unique Ticket No.

## Files

| File | Purpose |
|---|---|
| `bot.py` | WhatsApp webhook and the conversation flow |
| `report_engine.py` | Reads the pending call Excel; builds the pivot images (executive-wise and franchise-wise) |
| `insights.py` | File-type detection, chase list, CSAT scorecard |
| `requirements.txt` | Python packages |
| `render.yaml` | Render service definition (optional; the start command below is what matters) |
| `.env.example` | Names of the environment variables |
| `.gitignore` | Keeps `.env` and Excel files out of GitHub |
| `*_sample.png` | Example outputs |

All three `.py` files must be on GitHub together. `bot.py` imports the other two.

## Test the report engine alone (no WhatsApp)

    pip install -r requirements.txt
    python report_engine.py "Pending_call.xlsx" --min-days 3

## Environment variables

Set these in Render (Environment tab) or in a local `.env`. Never commit tokens.

| Name | Value |
|---|---|
| `WHATSAPP_TOKEN` | System-user access token (permanent) with `whatsapp_business_messaging` and `whatsapp_business_management` |
| `WHATSAPP_PHONE_ID` | Phone Number ID from Meta -> WhatsApp -> API setup (not the phone number, not the messaging account ID) |
| `VERIFY_TOKEN` | Any word you choose; must match the one typed in Meta's webhook form |
| `ALLOWED_NUMBERS` | Comma-separated numbers allowed to use the bot: country code, no `+`, no spaces, e.g. `919XXXXXXXXX` |
| `CONTACT_EMAIL` | Optional; shown on the `/privacy` page |
| `PYTHON_VERSION` | `3.12.3` (Render) |

## Deploy on Render (free)

1. Push this folder to a **private** GitHub repo.
2. Render -> New -> Web Service -> connect the repo.
3. Settings:
   - Language: Python 3
   - Region: Singapore
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn -w 1 --threads 4 -b 0.0.0.0:$PORT --timeout 120 bot:app`
   - Instance type: Free
   - Health check path: `/health`
4. Add the environment variables above.
5. When it says **Live**, check `https://<service>.onrender.com/health` (shows "ok")
   and `/privacy` (shows a privacy page).

Keep **one worker** (`-w 1`): conversation state is held in memory.
Auto-deploy is on: every push to GitHub redeploys the bot.

## Meta (WhatsApp Cloud API) setup

1. developers.facebook.com -> Create app -> use case "Connect with customers through WhatsApp".
2. WhatsApp -> API setup (Step 1 "Try it out"): note the **Phone Number ID**, add your own
   number as a recipient, and send the sample message to confirm it works.
3. Make a permanent token: Business Settings -> Users -> System users -> add the app and
   your WhatsApp account as assets -> Generate token with the two WhatsApp permissions.
   Check it in the Access Token Debugger (Expires: Never).
4. WhatsApp -> Configuration (Production setup -> Configure Webhooks):
   - Callback URL: `https://<service>.onrender.com/webhook`
   - Verify token: your `VERIFY_TOKEN`
   - Verify and save, then make sure the **messages** field is subscribed.
5. App settings -> Basic: Privacy Policy URL = `https://<service>.onrender.com/privacy`,
   pick an app category, save. Then **Publish** the app (it must be Live to receive messages).
6. Subscribe your WhatsApp account to the app (one time, from Terminal):

       curl -X POST "https://graph.facebook.com/v21.0/<MESSAGING_ACCOUNT_ID>/subscribed_apps" \
         -H "Authorization: Bearer <YOUR_TOKEN>"

   Expected reply: `{"success":true}`. You do not repeat this on normal restarts.
7. Message the test number from your phone and send the Excel as a **document**.

You do **not** need Business verification, a registered business number, or Production
setup for this prototype. The test number is the sender.

## Keep it awake

Render's free plan sleeps after ~15 minutes without traffic (first request then takes ~1 minute).
Add an uptime monitor that calls `https://<service>.onrender.com/health` **every 5 minutes**
(an hourly ping is not enough). Check the monitor's terms before using it for company work.

## Troubleshooting

Open Render -> Logs, turn on Live tail, search `webhook`, send a message, and read the lines:

| What you see | Meaning |
|---|---|
| No `POST /webhook` line | Meta isn't sending events: check the app is Live, `messages` is subscribed, and run the subscribe command (step 6) |
| `POST /webhook` then `send error:` | Token wrong, expired, or no access to the WhatsApp account |
| `POST /webhook` and a "not registered" reply | Your number is not in `ALLOWED_NUMBERS` (format `91XXXXXXXXXX`) |
| `handler error:` | A bug; copy the line (without tokens) |
| Build fails with "No module named insights" | `insights.py` is missing from GitHub |
| Reply is slow after quiet time | Service was asleep; check the uptime monitor |

## Limits and notes

- The Meta **test number** can only message numbers added as recipients (small cap) and
  cannot message franchisees on its own. Franchise messaging needs a real registered
  business number and approved message templates, which is an IFB decision.
- Free Render has no disk: sessions are lost on restart (fine for now; a database is the
  next step for promise logs and day-on-day comparison).
- Call data passes through WhatsApp (Meta) and the host. Get IT's approval before using
  real data beyond the pilot.
- Revoke and regenerate the access token if it is ever exposed (screenshots, chat, GitHub).

## Roadmap

1. Small free database (remember data between restarts)
2. Compare with yesterday; scheduled daily reports
3. Franchise-wise lists to franchisees, promise log, escalation
4. Franchise scorecard with closure rates (needs the closed-calls file)
5. Stock matching and failure patterns; AMC renewal tracker (need stock and AMC files)
6. Pulling data straight from the portal instead of an uploaded Excel (needs IT)
