# Pending Call Report Bot - Phase 1 (one-to-one WhatsApp)

## Files
- report_engine.py  Excel -> pivot -> colour-coded PNG (test alone: python report_engine.py file.xlsx --min-days 3)
- bot.py            WhatsApp Cloud API webhook + question flow
- .env.example      copy to .env and fill in

## Run the engine only (no WhatsApp needed)
    pip install -r requirements.txt
    python report_engine.py "Pending_call.xlsx" --min-days 3

## Connect WhatsApp (Meta Cloud API)
1. developers.facebook.com -> Create App (type: Business) -> add WhatsApp product.
2. Note the Phone Number ID and generate a token (use a System User for a permanent one).
   Meta gives a free test number; add your own phone as a recipient to try it.
3. cp .env.example .env and fill WHATSAPP_TOKEN / WHATSAPP_PHONE_ID.
4. python bot.py   then   ngrok http 5000
5. In the app's WhatsApp > Configuration: Callback URL = https://<ngrok-url>/webhook,
   Verify token = value of VERIFY_TOKEN, subscribe to "messages".
6. Message the test number, send the Excel, answer the questions.

## Notes
- "More than N days" means DAYS OLD > N.
- Days above 30 are grouped into a "30+" column (cap_days in build_pivot).
- Sessions are held in memory; restart = conversations reset. Fine for Phase 1.
- Standard filters (always ignored): call types COURTESY VISIT and DEALER DEFECTIVE REQUEST;
  pending reasons "Request soft closure verification" and "Cancellation Request verification".
  Edit STD_EXCLUDE_TYPES / STD_EXCLUDE_REASONS at the top of report_engine.py to change them.

## Deploy on Render (free)
1. Push this folder to a PRIVATE GitHub repo (the .gitignore keeps .env and Excel files out).
2. render.com -> New -> Blueprint (or Web Service) -> pick the repo.
   Start command: gunicorn -w 1 --threads 4 -b 0.0.0.0:$PORT --timeout 120 bot:app
3. In the service's Environment tab set WHATSAPP_TOKEN, WHATSAPP_PHONE_ID, VERIFY_TOKEN, ALLOWED_NUMBERS.
4. Open https://<service>.onrender.com/health -> should show "ok".
5. Meta dashboard -> WhatsApp -> Configuration: Callback URL = https://<service>.onrender.com/webhook,
   Verify token = your VERIFY_TOKEN, subscribe to "messages".
6. Add a free uptime monitor (e.g. UptimeRobot) pinging /health every 5 minutes so it never sleeps.
Keep ONE worker (-w 1): sessions live in memory.
