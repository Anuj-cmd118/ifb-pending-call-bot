"""
bot.py - WhatsApp Cloud API bot (one-to-one chats, Phase 1)

Flow
  1. Executive sends the portal Excel (.xlsx)
  2. Bot asks: how old?        (buttons: >2 / >3 / >5 / custom)
  3. Bot asks: standard filters? (buttons: standard IFB filters / no filters)
  4. Bot sends back the colour-coded image

Run:  python bot.py        (then expose port 5000 with ngrok / a server)
"""
import os
import threading
import time

import requests
from dotenv import load_dotenv
from flask import Flask, request

import insights as ins
import report_engine as eng

load_dotenv()
TOKEN = os.environ["WHATSAPP_TOKEN"]            # permanent access token (Meta)
PHONE_ID = os.environ["WHATSAPP_PHONE_ID"]      # phone number ID (not the number itself)
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "ifb-pending-bot")
ALLOWED = {n.strip() for n in os.environ.get("ALLOWED_NUMBERS", "").split(",") if n.strip()}
API = f"https://graph.facebook.com/v21.0"
HEAD = {"Authorization": f"Bearer {TOKEN}"}

app = Flask(__name__)
sessions = {}          # {phone: {"stage":..., "file": bytes, "min_days": int, ...}}
seen_ids = set()       # WhatsApp can deliver the same message twice


# ------------------------------------------------------------ send helpers
def _post(payload):
    r = requests.post(f"{API}/{PHONE_ID}/messages", headers=HEAD, json=payload, timeout=30)
    if not r.ok:
        print("send error:", r.status_code, r.text)

def send_text(to, text):
    _post({"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}})

def send_buttons(to, body, buttons):
    """buttons = [(id, title<=20 chars)], max 3"""
    _post({"messaging_product": "whatsapp", "to": to, "type": "interactive",
           "interactive": {"type": "button", "body": {"text": body},
                           "action": {"buttons": [{"type": "reply", "reply": {"id": i, "title": t}}
                                                  for i, t in buttons]}}})

def send_list(to, body, button_label, rows):
    """rows = [(id, title<=24, description<=72)]"""
    _post({"messaging_product": "whatsapp", "to": to, "type": "interactive",
           "interactive": {"type": "list", "body": {"text": body},
                           "action": {"button": button_label,
                                      "sections": [{"title": "Options", "rows": [
                                          {"id": i, "title": t, "description": d} for i, t, d in rows]}]}}})

def send_image(to, png: bytes, caption: str):
    up = requests.post(f"{API}/{PHONE_ID}/media", headers=HEAD,
                       data={"messaging_product": "whatsapp", "type": "image/png"},
                       files={"file": ("report.png", png, "image/png")}, timeout=60)
    media_id = up.json()["id"]
    _post({"messaging_product": "whatsapp", "to": to, "type": "image",
           "image": {"id": media_id, "caption": caption}})

def download_media(media_id) -> bytes:
    meta = requests.get(f"{API}/{media_id}", headers=HEAD, timeout=30).json()
    return requests.get(meta["url"], headers=HEAD, timeout=120).content


# ------------------------------------------------------------ conversation
def ask_action(to):
    send_buttons(to, "Got the pending call file. What do you need?",
                 [("act_pivot", "Executive pivot"), ("act_chase", "Chase list"),
                  ("act_exec", "By executive")])

def ask_age(to, action):
    body = {"chase": "Chase list: calls past SLA, ranked by priority. Which age counts as breached?",
            "exec": "Franchise-wise report for one executive. Show calls pending for:"
            }.get(action, "Show calls pending for:")
    send_buttons(to, body,
                 [("age_2", "More than 2 days"), ("age_3", "More than 3 days"),
                  ("age_custom", "Other / custom")])

def ask_types(to, s):
    send_buttons(to, f"Calls older than {s['min_days']} days. Apply the standard filters?\n\n"
                     "Standard = ignore Courtesy Visit, Dealer Defective, and pending reasons "
                     "'Request soft closure verification' / 'Cancellation Request verification'.",
                 [("filt_std", "Standard filters"), ("filt_none", "No filters")])

def generate(to, s):
    send_text(to, "Generating your report...")
    png, summary = eng.make_report(s["file"], s["min_days"],
                                   standard_filters=(s["filters"] == "filt_std"))
    send_image(to, png, summary) if png else send_text(to, summary)
    finish(to)

def generate_chase(to, s):
    send_text(to, "Building your chase list...")
    df = eng.load_report(s["file"])
    top, breach = ins.build_chase_list(df, s["min_days"])
    if top is None:
        send_text(to, f"No calls older than {s['min_days']} days after the standard filters.")
    else:
        send_image(to, ins.render_chase_list(top, s["min_days"], len(breach)),
                   ins.chase_summary(breach, ins.CHASE_TOP_N, s["min_days"]))
    finish(to)

def generate_csat(to, s):
    send_text(to, "Building the franchise CSAT scorecard...")
    png, summary = ins.csat_scorecard(s["file"])
    send_image(to, png, summary)
    finish(to)

def ask_executive(to, s):
    ex = eng.list_executives(s["file"], s["min_days"])
    if ex.empty:
        send_text(to, f"No calls older than {s['min_days']} days after the standard filters.")
        finish(to)
        return
    s["exec_names"] = list(ex.index)[:10]
    rows = [(f"exec_{i}", n.title()[:24], f"{int(ex[n])} calls past {s['min_days']} days")
            for i, n in enumerate(s["exec_names"])]
    s["stage"] = "exec"
    send_list(to, "Which executive? Pick one, or type a name.", "Choose executive", rows)

def generate_exec(to, s, name):
    send_text(to, f"Building {name.title()}'s franchise-wise report...")
    png, summary = eng.make_report(s["file"], s["min_days"], executive=name)
    send_image(to, png, summary + "\n(standard filters applied)") if png else send_text(to, summary)
    finish(to)

def finish(to):
    send_text(to, "Send another Excel any time, or type *hi* to start over.")
    sessions.pop(to, None)

def after_age(to, s):
    s["stage"] = "types"
    if s.get("action") == "chase":
        generate_chase(to, s)
    elif s.get("action") == "exec":
        ask_executive(to, s)
    else:
        ask_types(to, s)

def handle(msg):
    to = msg["from"]
    if ALLOWED and to not in ALLOWED:
        send_text(to, "Sorry, this number isn't registered to use the pending call bot.")
        return
    s = sessions.setdefault(to, {"stage": "idle"})
    kind = msg["type"]

    # ---- Excel received: detect which report it is
    if kind == "document":
        doc = msg["document"]
        name = (doc.get("filename") or "").lower()
        if not name.endswith((".xlsx", ".xls")):
            send_text(to, "Please send the Excel file (.xlsx) downloaded from the portal.")
            return
        try:
            data = download_media(doc["id"])
            rtype = ins.detect_report_type(data)
            if rtype == "pending":
                eng.load_report(data)                     # validate now, not later
        except Exception as e:
            send_text(to, f"I couldn't read that file: {e}")
            sessions.pop(to, None)
            return
        s.clear(); s.update({"file": data, "rtype": rtype})
        if rtype == "pending":
            s["stage"] = "action"; ask_action(to)
        elif rtype == "csat":
            generate_csat(to, s)
        elif rtype == "registered":
            send_text(to, "I recognise this as the *Calls Registered* report. I can't summarise it yet - "
                          "tell the developer what you want from it and it will be added next.")
            sessions.pop(to, None)
        else:
            send_text(to, "I don't recognise this file. Send the *Pending call* or *Customer satisfaction score* Excel.")
            sessions.pop(to, None)
        return

    # ---- button / list replies
    reply = None
    if kind == "interactive":
        i = msg["interactive"]
        reply = (i.get("button_reply") or i.get("list_reply") or {}).get("id")
    text = msg.get("text", {}).get("body", "").strip() if kind == "text" else ""

    if text.lower() in ("hi", "hello", "menu", "start", "cancel"):
        sessions.pop(to, None)
        send_text(to, "Hi! Send me the *Pending call* or *Customer satisfaction score* Excel from the portal.")
        return

    if s["stage"] == "action" and reply in ("act_pivot", "act_chase", "act_exec"):
        s["action"] = {"act_pivot": "pivot", "act_chase": "chase", "act_exec": "exec"}[reply]
        s["stage"] = "age"; ask_age(to, s["action"])
    elif s["stage"] == "age" and reply in ("age_2", "age_3"):
        s["min_days"] = int(reply[-1]); after_age(to, s)
    elif s["stage"] == "age" and reply == "age_custom":
        s["stage"] = "age_number"
        send_text(to, "Type the number of days. e.g. *5* means calls pending more than 5 days.")
    elif s["stage"] == "age_number" and text.isdigit():
        s["min_days"] = int(text); after_age(to, s)
    elif s["stage"] == "exec" and reply and reply.startswith("exec_"):
        generate_exec(to, s, s["exec_names"][int(reply.split("_")[1])])
    elif s["stage"] == "exec" and text:
        q = text.lower()
        names = [n for n in eng.list_executives(s["file"], s["min_days"]).index if q in n.lower()]
        if len(names) == 1:
            generate_exec(to, s, names[0])
        elif names:
            send_text(to, "More than one match: " + ", ".join(n.title() for n in names) + ". Type a fuller name.")
        else:
            send_text(to, f"No executive matching '{text}' has calls older than {s['min_days']} days. Try another name.")
    elif s["stage"] == "types" and reply in ("filt_std", "filt_none"):
        s["filters"] = reply
        generate(to, s)
    elif s["stage"] == "idle":
        send_text(to, "Send me the *Pending call* or *Customer satisfaction score* Excel to get started.")
    else:
        send_text(to, "Please use the options above, or type *hi* to start over.")


# --------------------------------------------------------------- webhook
@app.get("/health")
def health():
    return "ok", 200          # used by the uptime pinger to keep the free host awake

@app.get("/privacy")
def privacy():
    contact = os.environ.get("CONTACT_EMAIL", "the developer of this bot")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Privacy Policy - Pending Call Bot</title></head>
<body style="font-family:sans-serif;max-width:700px;margin:40px auto;padding:0 16px;line-height:1.6">
<h1>Privacy Policy</h1>
<p>The Pending Call Bot is an internal tool that turns a pending-call Excel report sent
over WhatsApp into a summary image.</p>
<h2>What we receive</h2>
<p>Your WhatsApp number, the messages you send to the bot, and the Excel file you upload.</p>
<h2>How it is used</h2>
<p>The file is processed in memory only to create the report image and is not saved to disk
or shared with anyone. Session data is deleted after the report is sent or when the service restarts.</p>
<h2>Sharing</h2>
<p>We do not sell or share your data. Messages pass through Meta's WhatsApp Business Platform,
which has its own privacy policy.</p>
<h2>Contact</h2>
<p>Questions or deletion requests: {contact}.</p>
</body></html>""", 200

@app.get("/webhook")
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge", ""), 200
    return "forbidden", 403

@app.post("/webhook")
def incoming():
    data = request.get_json(silent=True) or {}
    for entry in data.get("entry", []):
        for ch in entry.get("changes", []):
            for msg in ch.get("value", {}).get("messages", []):
                if msg["id"] in seen_ids:
                    continue
                seen_ids.add(msg["id"])
                # reply 200 fast; do the heavy work in a thread
                threading.Thread(target=_safe, args=(msg,), daemon=True).start()
    return "ok", 200

def _safe(msg):
    try:
        handle(msg)
    except Exception as e:
        print("handler error:", repr(e))
        send_text(msg["from"], "Something went wrong. Please type *hi* and try again.")

if __name__ == "__main__":
    app.run(port=5000)
