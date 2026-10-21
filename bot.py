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
def ask_age(to):
    send_buttons(to, "Got the file. Show calls pending for:",
                 [("age_2", "More than 2 days"), ("age_3", "More than 3 days"),
                  ("age_custom", "Other / custom")])
    # (3 buttons max. Add 'more than 5' via custom.)

def ask_types(to, s):
    send_buttons(to, f"Calls older than {s['min_days']} days. Apply the standard filters?\n\n"
                     "Standard = ignore Courtesy Visit, Dealer Defective, and pending reasons "
                     "'Request soft closure verification' / 'Cancellation Request verification'.",
                 [("filt_std", "Standard filters"), ("filt_none", "No filters")])

def generate(to, s):
    send_text(to, "Generating your report...")
    png, summary = eng.make_report(s["file"], s["min_days"],
                                   standard_filters=(s["filters"] == "filt_std"))
    if png is None:
        send_text(to, summary)
    else:
        send_image(to, png, summary)
    send_text(to, "Send another Excel any time, or type *hi* to start over.")
    sessions.pop(to, None)

def handle(msg):
    to = msg["from"]
    if ALLOWED and to not in ALLOWED:
        send_text(to, "Sorry, this number isn't registered to use the pending call bot.")
        return
    s = sessions.setdefault(to, {"stage": "idle"})
    kind = msg["type"]

    # ---- Excel received
    if kind == "document":
        doc = msg["document"]
        name = (doc.get("filename") or "").lower()
        if not name.endswith((".xlsx", ".xls")):
            send_text(to, "Please send the Excel file (.xlsx) downloaded from the portal.")
            return
        try:
            s["file"] = download_media(doc["id"])
            eng.load_report(s["file"])                   # validate now, not later
        except Exception as e:
            send_text(to, f"I couldn't read that file: {e}")
            sessions.pop(to, None)
            return
        s["stage"] = "age"
        ask_age(to)
        return

    # ---- button / list replies
    reply = None
    if kind == "interactive":
        i = msg["interactive"]
        reply = (i.get("button_reply") or i.get("list_reply") or {}).get("id")
    text = msg.get("text", {}).get("body", "").strip() if kind == "text" else ""

    if text.lower() in ("hi", "hello", "menu", "start", "cancel"):
        sessions.pop(to, None)
        send_text(to, "Hi! Send me the *Pending call* Excel from the portal and I'll make the report image.")
        return

    if s["stage"] == "age" and reply in ("age_2", "age_3"):
        s["min_days"] = int(reply[-1]); s["stage"] = "types"; ask_types(to, s)
    elif s["stage"] == "age" and reply == "age_custom":
        s["stage"] = "age_number"
        send_text(to, "Type the number of days. e.g. *5* means calls pending more than 5 days.")
    elif s["stage"] == "age_number" and text.isdigit():
        s["min_days"] = int(text); s["stage"] = "types"; ask_types(to, s)
    elif s["stage"] == "types" and reply in ("filt_std", "filt_none"):
        s["filters"] = reply
        generate(to, s)
    elif s["stage"] == "idle":
        send_text(to, "Send me the *Pending call* Excel file to get started.")
    else:
        send_text(to, "Please use the options above, or type *hi* to start over.")


# --------------------------------------------------------------- webhook
@app.get("/health")
def health():
    return "ok", 200          # used by the uptime pinger to keep the free host awake

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
