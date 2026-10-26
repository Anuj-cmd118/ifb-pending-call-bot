# IFB Pending Call Report Bot

A WhatsApp bot (Meta Cloud API) that turns the Excel reports downloaded from the service portal into colour-coded PNG summaries. An executive sends an Excel file in a one-to-one chat, answers a few button questions, and gets the report image back in seconds.

## Features

| Report sent | What the bot can produce |
|---|---|
| **Pending Call** Excel | **Executive pivot** – franchise x days-old pivot, colour-coded, days above 30 grouped as "30+" |
| | **Chase list** – top 10 calls past SLA, ranked by priority score (age, SLA breach, escalation, repeat call, missing reason) |
| | **By executive** – franchise-wise report for a single executive (pick from a list or type a name) |
| **Customer Satisfaction Score** Excel | **CSAT scorecard** – the 10 lowest franchises by % NOT HAPPY (minimum 20 responses), with overall Happy / OK / Not Happy split |
| **Calls Registered** Excel | Recognised, but not summarised yet (bot says so) |

The bot detects the report type automatically from the title row / column names, so the user never has to say which file they are sending.

## Project structure

| File | Purpose |
|---|---|
| `bot.py` | Flask app: WhatsApp webhook, conversation flow, send/receive helpers, `/health` and `/privacy` routes |
| `report_engine.py` | Excel -> pivot -> colour-coded PNG. Can be run standalone |
| `insights.py` | File-type detection, chase-list priority logic, reason tagging, CSAT scorecard, table renderer |
| `render.yaml` | Render.com blueprint for deployment |
| `requirements.txt` | Python dependencies |
| `.gitignore` | Keeps `.env` and Excel files out of git |

## Conversation flow

1. User sends the portal Excel (`.xlsx` / `.xls`).
2. Bot detects the report type.
   - **Pending call** -> asks: *Executive pivot / Chase list / By executive*
   - **CSAT** -> scorecard is generated immediately
3. For pending-call reports, bot asks how old the calls should be (*More than 2 days / More than 3 days / Other*). For **Executive pivot** it then asks whether to apply the standard filters. **Chase list** and **By executive** always apply the standard filters.
4. Bot sends back the image with a short text summary.

Typing `hi`, `hello`, `menu`, `start` or `cancel` resets the conversation at any point.

## Quick start: run the engine only (no WhatsApp needed)

```bash
pip install -r requirements.txt
python report_engine.py "Pending_call.xlsx" --min-days 3
```

## Connect WhatsApp (Meta Cloud API)

1. Go to [developers.facebook.com](https://developers.facebook.com) -> **Create App** (type: Business) -> add the **WhatsApp** product.
2. Note the **Phone Number ID** and generate an access token (use a System User for a permanent token). Meta provides a free test number; add your own phone as a recipient to try it.
3. Create a `.env` file in the project root (see [Environment variables](#environment-variables)).
4. Start the bot locally and expose it:
   ```bash
   python bot.py          # runs on port 5000
   ngrok http 5000
   ```
5. In the app's **WhatsApp -> Configuration**:
   - Callback URL = `https://<your-host>/webhook`
   - Verify token = the value of `VERIFY_TOKEN`
   - Subscribe to the **messages** field
6. Message the test number, send the Excel, and answer the questions.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `WHATSAPP_TOKEN` | Yes | Meta access token |
| `WHATSAPP_PHONE_ID` | Yes | Phone Number ID (not the phone number itself) |
| `VERIFY_TOKEN` | No | Webhook verification token. Defaults to `ifb-pending-bot`; set your own |
| `ALLOWED_NUMBERS` | No | Comma-separated WhatsApp numbers (with country code, no `+`) allowed to use the bot. Empty = anyone can use it |
| `CONTACT_EMAIL` | No | Shown on the `/privacy` page |

Example `.env`:

```env
WHATSAPP_TOKEN=your_token_here
WHATSAPP_PHONE_ID=your_phone_number_id
VERIFY_TOKEN=choose-a-secret
ALLOWED_NUMBERS=919876543210,919812345678
CONTACT_EMAIL=you@example.com
```

## HTTP endpoints

| Route | Purpose |
|---|---|
| `GET /health` | Returns `ok`; used by an uptime monitor |
| `GET /webhook` | Meta webhook verification |
| `POST /webhook` | Incoming WhatsApp messages |
| `GET /privacy` | Privacy policy page (required by Meta for app review) |

## Business rules

### Pending call reports
- "More than N days" means `DAYS OLD > N`.
- Days above 30 are grouped into a **30+** column (`cap_days` in `build_pivot`).
- **Standard filters** ignore:
  - Call types: `COURTESY VISIT`, `DEALER DEFECTIVE REQUEST`
  - Pending reasons: `Request soft closure verification`, `Cancellation Request verification`

  Edit `STD_EXCLUDE_TYPES` / `STD_EXCLUDE_REASONS` at the top of `report_engine.py` to change them.

### Chase list (`insights.py`)
Calls past SLA are scored; the highest scores are listed first.

| Rule | Default |
|---|---|
| SLA breach when days old > | `3` (`SLA_DAYS`; overridden by the age the user picks) |
| Calls on the list | `10` (`CHASE_TOP_N`) |
| Age points | 1 per day, capped at 30 (`PTS_AGE_CAP`) |
| SLA breach | +5 |
| Escalation Level 1 | +10 |
| Escalation Level 2/3, Senior Mgmt, Social Media | +20 |
| Repeat call (repeat flag, or same Customer Code + Product) | +8 |
| No pending reason given | +5 |

Pending reasons are auto-tagged as *Part pending*, *Revisit / rework*, *Technician action*, *Customer unavailable*, *No reason given* or *Other* (`tag_reason`). Row colours: red = 15+ days, orange = 7-14 days, yellow = under 7 days.

### CSAT scorecard
- Franchises with fewer than **20** responses are not ranked (`MIN_RESPONSES`).
- Ranked worst-first by % NOT HAPPY, then by Happy %.
- Happy % colouring: red below 90%, orange below 95%, yellow otherwise.

All of these constants sit near the top of `insights.py` and can be edited directly.

## Deploy on Render (free)

1. Push this folder to a **private** GitHub repo (`.gitignore` keeps `.env` and Excel files out).
2. On [render.com](https://render.com): **New -> Blueprint** (or Web Service) and pick the repo.
   Start command:
   ```bash
   gunicorn -w 1 --threads 4 -b 0.0.0.0:$PORT --timeout 120 bot:app
   ```
3. In the service's **Environment** tab, set `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_ID`, `VERIFY_TOKEN`, `ALLOWED_NUMBERS` (and optionally `CONTACT_EMAIL`).
4. Open `https://<your-service>.onrender.com/health` - it should show `ok`.
5. In the Meta dashboard -> **WhatsApp -> Configuration**: Callback URL = `https://<your-service>.onrender.com/webhook`, Verify token = your `VERIFY_TOKEN`, subscribe to **messages**.
6. Add a free uptime monitor (e.g. UptimeRobot) pinging `/health` every 5 minutes so the free instance never sleeps.

> Keep **one worker** (`-w 1`): sessions are stored in memory.

## Limitations

- Sessions are held in memory; a restart resets all conversations.
- Uploaded files are processed in memory and never written to disk.
- The *Calls Registered* report is detected but not yet summarised.
- Without `ALLOWED_NUMBERS`, anyone who messages the number can use the bot.

## Privacy

The bot exposes a privacy policy at `/privacy`. Files are processed in memory only, not stored, and not shared; messages pass through Meta's WhatsApp Business Platform.
