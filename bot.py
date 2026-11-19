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
import re
import threading
import traceback
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
SESSION_TTL = 30 * 60  # seconds idle before the uploaded Excel is dropped from memory
PENDING_TTL = 10 * 60  # after a pending-call report: the parsed file is kept this long for more reports


def _touch(s):
    s["ts"] = time.time()


def _reaper():
    while True:
        time.sleep(60)
        now = time.time()
        for k in [k for k, v in list(sessions.items()) if now - v.get("ts", now) > v.get("ttl", SESSION_TTL)]:
            sessions.pop(k, None)
        if len(seen_ids) > 5000:
            seen_ids.clear()


threading.Thread(target=_reaper, daemon=True).start()


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
def ask_action(to, body="Got the pending call file. What do you need?"):
    send_list(to, body, "Choose report",
              [("act_pivot", "Executive pivot", "Executive x days, filtered"),
               ("act_ageing", "Ageing summary", "Days 0-N plus total and >N days"),
               ("act_chase", "Chase list", "Top calls past SLA, ranked"),
               ("act_exec", "By executive", "One executive, franchise-wise"),
               ("act_tech", "Franchise technicians", "One franchise, technician-wise"),
               ("act_lists", "Franchise call lists", "Call list image per franchise")])

def ask_csat(to):
    send_list(to, "Got the customer satisfaction file. What do you need?", "Choose report",
              [("csat_exec", "CSS by executive", "All executives, worst first"),
               ("csat_exfr", "Executive's franchises", "Franchise-wise CSS, one executive"),
               ("csat_fr", "Franchise scorecard", "10 lowest franchises by % not happy")])

def ask_csat_executive(to, s):
    ex = ins.csat_executives(s["file"])
    if ex.empty:
        send_text(to, "No CSAT responses found for any executive in this file.")
        finish(to)
        return
    s["cexec_names"] = list(ex.index)[:10]
    rows = [(f"cexec_{i}", n.title()[:24], f"{int(ex[n])} responses") for i, n in enumerate(s["cexec_names"])]
    s["stage"] = "csat_pick"
    send_list(to, "Which executive? Pick one, or type a name.", "Choose executive", rows)

def ask_soft(to):
    send_list(to, "Got the soft closure file. What do you need?", "Choose report",
              [("soft_exec", "Closed: by executive", "Executives x days taken, with total"),
               ("soft_all", "All executives combined", "Closed calls by call type"),
               ("soft_exfr", "Executive's franchises", "Franchise x days, one executive"),
               ("soft_tech", "Franchise technicians", "Pick executive, then each franchise")])

def ask_soft_executive(to, s):
    ex = ins.soft_closure_executives(s["file"])
    if ex.empty:
        send_text(to, "No soft closed calls found for any executive in this file.")
        finish(to)
        return
    s["sexec_names"] = list(ex.index)[:10]
    rows = [(f"sexec_{i}", n.title()[:24], f"{int(ex[n])} calls closed") for i, n in enumerate(s["sexec_names"])]
    s["stage"] = "soft_pick"
    send_list(to, "Which executive? Pick one, or type a name.", "Choose executive", rows)

SPAGE = 8   # 8 franchises + "More" + "Done" = 10 rows (WhatsApp list limit)

def pick_soft_executive(to, s, name):
    """Executive chosen in the soft closure flow: franchise-wise report, or the technician franchise picker."""
    if s.get("soft_mode") == "tech":
        fr = ins.soft_closure_franchise_counts(s["file"], name)
        if fr.empty:
            send_text(to, f"No soft closed calls for {name.title()} on the report day after the exclusions.")
            finish(to)
            return
        s["sexec"], s["sfr_names"], s["sfr_counts"] = name, list(fr.index), fr
        ask_soft_franchise(to, s, 0)
    else:
        generate_soft(to, s, "soft_exfr", name)

def ask_soft_franchise(to, s, page=0):
    names, cnt = s["sfr_names"], s["sfr_counts"]
    chunk = names[page * SPAGE:(page + 1) * SPAGE]
    rows = [(f"sfr_{page * SPAGE + i}", f"{page * SPAGE + i + 1}. {n.title()}"[:24], f"{int(cnt[n])} calls closed")
            for i, n in enumerate(chunk)]
    left = len(names) - (page + 1) * SPAGE
    if left > 0:
        rows.append((f"sfrpage_{page + 1}", "More franchises >", f"{left} more"))
    rows.append(("sfr_done", "Done", "Finish"))
    s["stage"], s["spage"] = "soft_fran", page
    send_list(to, f"{s['sexec'].title()}: tap a franchise for its technician-wise closed calls, or type part of a name.",
              "Choose franchise", rows)

def generate_soft_tech(to, s, franchise):
    send_text(to, f"Building the technician-wise report for {franchise.title()}...")
    png, summary = ins.soft_closure_technicians(s["file"], s["sexec"], franchise)
    send_image(to, png, summary) if png else send_text(to, summary)
    ask_soft_franchise(to, s, s.get("spage", 0))      # back to the picker; session stays open

def generate_soft(to, s, kind, name=None):
    send_text(to, "Building the soft closure report...")
    if kind == "soft_exec":
        png, summary = ins.soft_closure_by_executive(s["file"])
    elif kind == "soft_all":
        png, summary = ins.soft_closure_combined(s["file"])
    else:
        png, summary = ins.soft_closure_franchises(s["file"], name)
    send_image(to, png, summary) if png else send_text(to, summary)
    finish(to)

def generate_csat_exfr(to, s, name):
    send_text(to, f"Building {name.title()}'s franchise-wise CSS report...")
    png, summary = ins.csat_franchise_css(s["file"], name)
    send_image(to, png, summary) if png else send_text(to, summary)
    finish(to)

def ask_age(to, action):
    body = {"chase": "Chase list: calls past SLA, ranked by priority. Which age counts as breached?",
            "exec": "Franchise-wise report for one executive. Show calls pending for:",
            "tech": "Technician-wise report for one franchise. Show calls pending for:",
            "ageing": "Ageing summary: each day from 0 up to N, then '>N-1 days' and '>N days' columns. Choose N:"
            }.get(action, "Show calls pending for:")
    if action == "ageing":
        btns = [("age_3", "Up to 3 days"), ("age_4", "Up to 4 days"), ("age_custom", "Other / custom")]
    elif action == "exec":                      # includes 0-day calls via "All incl. 0 days"
        btns = [("age_-1", "All incl. 0 days"), ("age_3", "More than 3 days"), ("age_custom", "Other / custom")]
    else:
        btns = [("age_2", "More than 2 days"), ("age_3", "More than 3 days"), ("age_custom", "Other / custom")]
    send_buttons(to, body, btns)

def ask_types(to, s):
    send_buttons(to, f"{age_phrase(s['min_days']).capitalize()}. Apply the standard filters?\n\n"
                     "Standard = ignore Courtesy Visit, Dealer Defective, and pending reasons "
                     "'Request soft closure verification' / 'Cancellation Request verification'.",
                 [("filt_std", "Standard filters"), ("filt_none", "No filters")])

def generate(to, s):
    send_text(to, "Generating your report...")
    png, summary = eng.make_report(s["df"], s["min_days"],
                                   standard_filters=(s["filters"] == "filt_std"))
    send_image(to, png, summary) if png else send_text(to, summary)
    finish(to)

def generate_chase(to, s):
    send_text(to, "Building your chase list...")
    df = s["df"]
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
    send_image(to, png, summary) if png else send_text(to, summary)
    finish(to)

def ask_executive(to, s):
    ex = eng.list_executives(s["df"], s["min_days"])
    if ex.empty:
        send_text(to, f"No {age_phrase(s['min_days'])} after the standard filters.")
        finish(to)
        return
    s["exec_names"] = list(ex.index)[:10]
    rows = [(f"exec_{i}", n.title()[:24], f"{int(ex[n])} calls{desc_age(s['min_days'])}")
            for i, n in enumerate(s["exec_names"])]
    s["stage"] = "exec"
    send_list(to, "Which executive? Pick one, or type a name.", "Choose executive", rows)

def generate_exec(to, s, name):
    send_text(to, f"Building {name.title()}'s franchise-wise report...")
    png, summary = eng.make_report(s["df"], s["min_days"], executive=name)
    send_image(to, png, summary + "\n(standard filters applied)") if png else send_text(to, summary)
    finish(to)

def generate_ageing(to, s):
    send_text(to, "Building the ageing summary...")
    png, summary = ins.ageing_summary(s["df"], s["min_days"])
    send_image(to, png, summary) if png else send_text(to, summary)
    finish(to)

def generate_csat_exec(to, s):
    send_text(to, "Building the executive-wise CSS report...")
    png, summary = ins.csat_by_executive(s["file"])
    send_image(to, png, summary) if png else send_text(to, summary)
    finish(to)

def ask_list_executive(to, s):
    ex = eng.list_executives(s["df"], -1)
    if ex.empty:
        send_text(to, "No pending calls found after the standard filters.")
        finish(to)
        return
    s["lexec_names"] = list(ex.index)[:10]
    rows = [(f"lexec_{i}", n.title()[:24], f"{int(ex[n])} pending calls") for i, n in enumerate(s["lexec_names"])]
    s["stage"] = "lists_pick"
    send_list(to, "Call lists for which executive's franchises? Pick one, or type a name.", "Choose executive", rows)

LPAGE = 8   # 8 franchises + "More" + "Done" = 10 rows (WhatsApp list limit)
LGROUP_MAX = 3

def ask_list_franchise(to, s, page=0):
    names, cnt = s["lfr_names"], s["lfr_counts"]
    chunk = names[page * LPAGE:(page + 1) * LPAGE]
    rows = [(f"lfr_{page * LPAGE + i}", f"{page * LPAGE + i + 1}. {n.title()}"[:24], f"{int(cnt[n])} pending calls")
            for i, n in enumerate(chunk)]
    left = len(names) - (page + 1) * LPAGE
    if left > 0:
        rows.append((f"lfrpage_{page + 1}", "More franchises >", f"{left} more"))
    rows.append(("lfr_done", "Done", "Finish"))
    s["stage"], s["lpage"] = "lists_fran", page
    mode = "2+ days only" if s.get("lmin") else "all calls"
    send_list(to, f"{s['lexec'].title()} ({mode}): tap a franchise, or type numbers like *2,5* "
                  f"(up to {LGROUP_MAX}) to get each as its own image in one go.",
              "Choose franchise", rows)

def generate_lists(to, s, name):
    s["lexec"] = name
    s["stage"] = "lists_age"
    send_buttons(to, f"{name.title()}: which calls should the lists include?",
                 [("lage_0", "All calls"), ("lage_2", "2+ days only")])

def start_list_picker(to, s, min_age):
    s["lmin"] = min_age
    fr = ins.call_list_franchises(s["df"], s["lexec"], min_age)
    if fr.empty:
        send_text(to, f"No {'2+ day old ' if min_age else ''}pending calls for {s['lexec'].title()} after the standard filters.")
        finish(to)
        return
    s["lfr_names"], s["lfr_counts"] = list(fr.index), fr
    ask_list_franchise(to, s, 0)

def generate_list_one(to, s, franchise):
    """franchise = one name, or a list of 2-3 names: each gets its OWN image(s), sent one after another
    from a single selection (not combined into one list)."""
    names = [franchise] if isinstance(franchise, str) else list(franchise)
    label = names[0].title() if len(names) == 1 else f"{len(names)} franchises (separate images)"
    send_text(to, f"Building the call list for {label}...")
    for n in names:
        pages = ins.franchise_call_list(s["df"], s["lexec"], n, s.get("lmin", 0))
        if not pages:
            send_text(to, f"No pending calls for {n.title()}.")
        for png, cap in pages:
            try:
                send_image(to, png, cap)
            except Exception as e:                # one failed image must not stop the rest
                print("call-list image failed:", repr(e))
                send_text(to, f"Could not send the image for {cap.split(':')[0]}.")
            time.sleep(0.4)
    ask_list_franchise(to, s, s.get("lpage", 0))  # back to the picker; session stays open

def age_phrase(n):
    return "pending calls" if n < 0 else f"calls older than {n} days"

def desc_age(n):
    return "" if n < 0 else f" past {n} days"

def ask_franchise(to, s):
    fr = eng.list_franchises(s["df"], s["min_days"])
    if fr.empty:
        send_text(to, f"No {age_phrase(s['min_days'])} after the standard filters.")
        finish(to)
        return
    if len(fr) == 1:                              # single-franchise file: no need to ask
        generate_tech(to, s, fr.index[0])
        return
    s["fr_names"] = list(fr.index)[:10]
    rows = [(f"fran_{i}", n.title()[:24], f"{n.title()} ({int(fr[n])} calls)"[:72])
            for i, n in enumerate(s["fr_names"])]
    s["stage"] = "fran_pick"
    send_list(to, "Which franchise? Pick one, or type part of its name.", "Choose franchise", rows)

def generate_tech(to, s, name):
    send_text(to, f"Building {name.title()}'s technician-wise report...")
    png, summary = eng.make_report(s["df"], s["min_days"], franchise=name)
    send_image(to, png, summary + "\n(standard filters applied, mandatory calls excluded)") if png else send_text(to, summary)
    finish(to)

MPAGE = 8   # 8 executives + "More" + "Done" = 10 rows (WhatsApp list limit)

def ask_mand_exec(to, s, page=0):
    names = s["mnames"]
    chunk = names[page * MPAGE:(page + 1) * MPAGE]
    cnt = ins.mandatory_executives(s["df"])
    rows = [(f"mex_{page * MPAGE + i}", n.title()[:24], f"{int(cnt[n])} mandatory calls") for i, n in enumerate(chunk)]
    left = len(names) - (page + 1) * MPAGE
    if left > 0:
        rows.append((f"mexpage_{page + 1}", "More executives >", f"{left} more"))
    rows.append(("mex_done", "Done", "Finish"))
    s["stage"], s["mpage"] = "mand", page
    send_list(to, "Pick an executive for his franchise-wise mandatory data (or type part of the name).",
              "Choose executive", rows)

def generate_mand_exec(to, s, name):
    send_text(to, f"Building {name.title()}'s franchise-wise mandatory report...")
    png, summary = ins.mandatory_franchises(s["df"], name)
    send_image(to, png, summary) if png else send_text(to, summary)
    ask_mand_exec(to, s, s.get("mpage", 0))       # back to the picker; session stays open

def finish(to):
    s = sessions.get(to)
    if s and s.get("rtype") == "pending" and s.get("df") is not None:
        # keep the parsed pending file for PENDING_TTL so another report needs no re-upload
        keep = {"rtype": "pending", "df": s["df"], "file": None}
        s.clear()
        s.update(keep, stage="action", ttl=PENDING_TTL)
        _touch(s)
        ask_action(to, "Same pending file is kept for 10 minutes. Pick another report, "
                       "or send a new Excel / type *hi* to start over.")
        return
    send_text(to, "Send another Excel any time, or type *hi* to start over.")
    sessions.pop(to, None)

def after_age(to, s):
    s["stage"] = "types"
    if s.get("action") == "chase":
        generate_chase(to, s)
    elif s.get("action") == "exec":
        ask_executive(to, s)
    elif s.get("action") == "ageing":
        generate_ageing(to, s)
    elif s.get("action") == "tech":
        ask_franchise(to, s)
    else:
        ask_types(to, s)

def handle(msg):
    to = msg["from"]
    if ALLOWED and to not in ALLOWED:
        send_text(to, "Sorry, this number isn't registered to use the pending call bot.")
        return
    s = sessions.setdefault(to, {"stage": "idle"})
    _touch(s)
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
            df = None
            if rtype == "pending":
                df = eng.load_report(data)                # parse ONCE; cached for every later click
            elif rtype == "softclosure":
                ins.load_soft_closure(data)
            elif rtype == "mandatory":
                df = ins.load_mandatory(data)             # parsed once, cached for the executive picks
        except Exception as e:
            send_text(to, f"I couldn't read that file: {e}")
            sessions.pop(to, None)
            return
        s.clear(); s.update({"file": None if rtype in ("pending", "mandatory") else data, "df": df, "rtype": rtype})
        _touch(s)
        if rtype == "pending":
            s["stage"] = "action"; ask_action(to)
        elif rtype == "csat":
            s["stage"] = "csat"; ask_csat(to)
        elif rtype == "softclosure":
            s["stage"] = "soft"; ask_soft(to)
        elif rtype == "mandatory":
            send_text(to, "Building the Mandatory appointments report...")
            png, summary = ins.mandatory_appointments(df)
            send_image(to, png, summary) if png else send_text(to, summary)
            if png:
                s["mnames"] = list(ins.mandatory_executives(df).index)
                ask_mand_exec(to, s, 0)
            else:
                finish(to)
        elif rtype == "registered":
            send_text(to, "I recognise this as the *Calls Registered* report. I can't summarise it yet - "
                          "tell the developer what you want from it and it will be added next.")
            sessions.pop(to, None)
        else:
            send_text(to, "I don't recognise this file. Send the *Pending call*, *Customer satisfaction score*, *Soft Closure* or *Mandatory Appointment* Excel.")
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
        send_text(to, "Hi! Send me the *Pending call*, *Customer satisfaction score*, *Soft Closure* or *Mandatory Appointment* Excel from the portal.")
        return

    if s["stage"] == "csat" and reply in ("csat_exec", "csat_exfr", "csat_fr"):
        if reply == "csat_exec":
            generate_csat_exec(to, s)
        elif reply == "csat_exfr":
            ask_csat_executive(to, s)
        else:
            generate_csat(to, s)
    elif s["stage"] == "csat_pick" and reply and reply.startswith("cexec_"):
        generate_csat_exfr(to, s, s["cexec_names"][int(reply.split("_")[1])])
    elif s["stage"] == "csat_pick" and text:
        q = text.lower()
        names = [n for n in ins.csat_executives(s["file"]).index if q in n.lower()]
        if len(names) == 1:
            generate_csat_exfr(to, s, names[0])
        elif names:
            send_text(to, "More than one match: " + ", ".join(n.title() for n in names) + ". Type a fuller name.")
        else:
            send_text(to, f"No executive matching '{text}' in this file. Try another name.")
    elif s["stage"] == "soft" and reply in ("soft_exec", "soft_all", "soft_exfr", "soft_tech"):
        if reply in ("soft_exfr", "soft_tech"):
            s["soft_mode"] = "tech" if reply == "soft_tech" else "exfr"
            ask_soft_executive(to, s)
        else:
            generate_soft(to, s, reply)
    elif s["stage"] == "soft_pick" and reply and reply.startswith("sexec_"):
        pick_soft_executive(to, s, s["sexec_names"][int(reply.split("_")[1])])
    elif s["stage"] == "soft_pick" and text:
        q = text.lower()
        names = [n for n in ins.soft_closure_executives(s["file"]).index if q in n.lower()]
        if len(names) == 1:
            pick_soft_executive(to, s, names[0])
        elif names:
            send_text(to, "More than one match: " + ", ".join(n.title() for n in names) + ". Type a fuller name.")
        else:
            send_text(to, f"No executive matching '{text}' in this file. Try another name.")
    elif s["stage"] == "fran_pick" and reply and reply.startswith("fran_"):
        generate_tech(to, s, s["fr_names"][int(reply.split("_")[1])])
    elif s["stage"] == "fran_pick" and text:
        q = text.lower()
        names = [n for n in eng.list_franchises(s["df"], s["min_days"]).index if q in n.lower()]
        if len(names) == 1:
            generate_tech(to, s, names[0])
        elif 1 < len(names) <= 6:
            send_text(to, "More than one match: " + ", ".join(n.title() for n in names) + ". Type a fuller name.")
        elif names:
            send_text(to, f"{len(names)} franchises match. Type more of the name.")
        else:
            send_text(to, f"No franchise matching '{text}' has {age_phrase(s['min_days'])}. Try another name.")
    elif s["stage"] == "soft_fran" and reply == "sfr_done":
        finish(to)
    elif s["stage"] == "soft_fran" and reply and reply.startswith("sfrpage_"):
        ask_soft_franchise(to, s, int(reply.split("_")[1]))
    elif s["stage"] == "soft_fran" and reply and reply.startswith("sfr_"):
        generate_soft_tech(to, s, s["sfr_names"][int(reply.split("_")[1])])
    elif s["stage"] == "soft_fran" and text:
        q = text.lower()
        names = [n for n in s["sfr_names"] if q in n.lower()]
        if len(names) == 1:
            generate_soft_tech(to, s, names[0])
        elif 1 < len(names) <= 6:
            send_text(to, "More than one match: " + ", ".join(n.title() for n in names) + ". Type a fuller name.")
        elif names:
            send_text(to, f"{len(names)} franchises match. Type more of the name.")
        else:
            send_text(to, f"No franchise matching '{text}'. Try another name, or type *hi* to start over.")
    elif s["stage"] == "mand" and reply == "mex_done":
        finish(to)
    elif s["stage"] == "mand" and reply and reply.startswith("mexpage_"):
        ask_mand_exec(to, s, int(reply.split("_")[1]))
    elif s["stage"] == "mand" and reply and reply.startswith("mex_"):
        generate_mand_exec(to, s, s["mnames"][int(reply.split("_")[1])])
    elif s["stage"] == "mand" and text:
        q = text.lower()
        names = [n for n in s["mnames"] if q in n.lower()]
        if len(names) == 1:
            generate_mand_exec(to, s, names[0])
        elif names:
            send_text(to, "More than one match: " + ", ".join(n.title() for n in names[:6]) + ". Type a fuller name.")
        else:
            send_text(to, f"No executive matching '{text}'. Try another name, or type *hi* to start over.")
    elif s["stage"] == "lists_fran" and reply == "lfr_done":
        finish(to)
    elif s["stage"] == "lists_fran" and reply and reply.startswith("lfrpage_"):
        ask_list_franchise(to, s, int(reply.split("_")[1]))
    elif s["stage"] == "lists_fran" and reply and reply.startswith("lfr_"):
        generate_list_one(to, s, s["lfr_names"][int(reply.split("_")[1])])
    elif s["stage"] == "lists_age" and reply in ("lage_0", "lage_2"):
        start_list_picker(to, s, int(reply.split("_")[1]))
    elif s["stage"] == "lists_fran" and text and re.fullmatch(r"\d+([ ,;+&]+\d+)*", text):
        idx = []
        for t in re.findall(r"\d+", text):
            i = int(t) - 1
            if i not in idx:
                idx.append(i)
        if not all(0 <= i < len(s["lfr_names"]) for i in idx):
            send_text(to, f"Use numbers between 1 and {len(s['lfr_names'])}, as shown before each franchise name.")
        elif len(idx) > LGROUP_MAX:
            send_text(to, f"Combine at most {LGROUP_MAX} franchises at a time.")
        else:
            generate_list_one(to, s, [s["lfr_names"][i] for i in idx])
    elif s["stage"] == "lists_fran" and text:
        q = text.lower()
        names = [n for n in s["lfr_names"] if q in n.lower()]
        if len(names) == 1:
            generate_list_one(to, s, names[0])
        elif 1 < len(names) <= 6:
            send_text(to, "More than one match: " + ", ".join(n.title() for n in names) + ". Type a fuller name.")
        elif names:
            send_text(to, f"{len(names)} franchises match. Type more of the name.")
        else:
            send_text(to, f"No franchise matching '{text}'. Try another name, or type *hi* to start over.")
    elif s["stage"] == "lists_pick" and reply and reply.startswith("lexec_"):
        generate_lists(to, s, s["lexec_names"][int(reply.split("_")[1])])
    elif s["stage"] == "lists_pick" and text:
        q = text.lower()
        names = [n for n in eng.list_executives(s["df"], -1).index if q in n.lower()]
        if len(names) == 1:
            generate_lists(to, s, names[0])
        elif names:
            send_text(to, "More than one match: " + ", ".join(n.title() for n in names) + ". Type a fuller name.")
        else:
            send_text(to, f"No executive matching '{text}' has pending calls. Try another name.")
    elif s["stage"] == "action" and reply == "act_lists":
        s["action"] = "lists"; ask_list_executive(to, s)
    elif s["stage"] == "action" and reply in ("act_pivot", "act_ageing", "act_chase", "act_exec", "act_tech"):
        s["action"] = {"act_pivot": "pivot", "act_ageing": "ageing", "act_chase": "chase", "act_exec": "exec",
                       "act_tech": "tech"}[reply]
        s["stage"] = "age"; ask_age(to, s["action"])
    elif s["stage"] == "age" and reply and reply.startswith("age_") and reply != "age_custom":
        s["min_days"] = int(reply.split("_")[1]); after_age(to, s)
    elif s["stage"] == "age" and reply == "age_custom":
        s["stage"] = "age_number"
        extra = " For every pending call, type *all*." if s.get("action") in ("pivot", "exec", "tech") else ""
        send_text(to, "Type the number of days. e.g. *5* means calls pending more than 5 days." + extra)
    elif s["stage"] == "age_number" and text.lower() == "all" and s.get("action") in ("pivot", "exec", "tech"):
        s["min_days"] = -1; after_age(to, s)
    elif s["stage"] == "age_number" and text.isdigit():
        s["min_days"] = int(text); after_age(to, s)
    elif s["stage"] == "exec" and reply and reply.startswith("exec_"):
        generate_exec(to, s, s["exec_names"][int(reply.split("_")[1])])
    elif s["stage"] == "exec" and text:
        q = text.lower()
        names = [n for n in eng.list_executives(s["df"], s["min_days"]).index if q in n.lower()]
        if len(names) == 1:
            generate_exec(to, s, names[0])
        elif names:
            send_text(to, "More than one match: " + ", ".join(n.title() for n in names) + ". Type a fuller name.")
        else:
            send_text(to, f"No executive matching '{text}' has {age_phrase(s['min_days'])}. Try another name.")
    elif s["stage"] == "types" and reply in ("filt_std", "filt_none"):
        s["filters"] = reply
        generate(to, s)
    elif s["stage"] == "idle":
        send_text(to, "Send me the *Pending call*, *Customer satisfaction score*, *Soft Closure* or *Mandatory Appointment* Excel to get started.")
    else:
        send_text(to, "Please use the options above, or type *hi* to start over.")


# --------------------------------------------------------------- webhook
VERSION = "2026-11-19-v18-soft-technicians"

@app.get("/version")
def version():
    """Open /version in a browser to see which code Render is really running."""
    return {"version": VERSION,
            "excluded_executives": getattr(eng, "EXCLUDE_EXECUTIVES", "MISSING - old report_engine.py on GitHub")}

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
or shared with anyone. Session data is deleted after the report is sent (a pending call file is kept for up to 10 minutes
so you can run more reports on it), after 30 minutes idle, or when the service restarts.</p>
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
        traceback.print_exc()                      # full detail goes to the Render logs
        print("handler error:", repr(e))
        send_text(msg["from"], f"Something went wrong ({type(e).__name__}: {str(e)[:120]}). "
                               "Please type *hi* and try again.")

if __name__ == "__main__":
    app.run(port=5000)
