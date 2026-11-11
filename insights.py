"""
insights.py  -  Level 1 ("make the report decide") + file-type detection + CSAT scorecard.

Every rule an executive might want to change is a constant near the top.
"""
import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import report_engine as eng

# ------------------------------------------------------------ editable rules
SLA_DAYS = 3                 # a call is an SLA breach when DAYS OLD > this
CHASE_TOP_N = 10             # how many calls go on the "chase today" list
MIN_RESPONSES = 20           # CSAT: franchises with fewer responses are not ranked
CSS_TARGET_PCT = 95          # CSS target (whole percent) used by "Yet to get 95%"

# priority points (higher = chase first)
PTS_AGE_CAP = 30             # 1 point per day, capped
PTS_BREACH = 5
PTS_ESC_L1 = 10
PTS_ESC_HIGHER = 20          # Esc Level 2/3, Snr Mangmt, Soc Media
PTS_REPEAT = 8
PTS_NO_REASON = 5


# ------------------------------------------------------- file-type detection
def detect_report_type(source) -> str:
    """Returns 'pending' | 'registered' | 'csat' | 'unknown' using the title row,
    falling back to the column names."""
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    raw = pd.read_excel(source, header=None, nrows=6)
    title = str(raw.iloc[0, 0]).lower() if len(raw) else ""
    cols = {str(v).strip() for v in raw.values.ravel() if pd.notna(v)}
    if "pending call" in title:
        return "pending"
    if "customer sat" in title:
        return "csat"
    if "calls regist" in title:
        return "registered"
    if {"DAYS OLD", "Pending Reason", "Spare"} <= cols:
        return "pending"
    if {"FeedBack", "TicketNo"} <= cols:
        return "csat"
    if {"DAYS OLD", "CLOSED TIME"} <= cols:
        return "registered"
    return "unknown"


# ------------------------------------------------------------ Level 1 logic
def tag_reason(reason) -> str:
    if pd.isna(reason) or not str(reason).strip():
        return "No reason given"
    t = str(reason).lower()
    if "spare" in t:
        return "Part pending"
    if any(k in t for k in ("revisit", "did not resolve", "rework", "rewir")):
        return "Revisit / rework"
    if "technician" in t:
        return "Technician action"
    if any(k in t for k in ("customer", "appointment", "out of station", "call back", "withdrawn")):
        return "Customer unavailable"
    return "Other"


def prepare_pending(df: pd.DataFrame, sla_days=SLA_DAYS) -> pd.DataFrame:
    d = df.copy()
    d = d[~d[eng.COL_TYPE].isin(eng.STD_EXCLUDE_TYPES)]
    d = d[~d[eng.COL_REASON].astype(str).str.strip().str.lower()
          .isin([r.lower() for r in eng.STD_EXCLUDE_REASONS])]
    d["Reason tag"] = d[eng.COL_REASON].map(tag_reason)
    d["SLA breach"] = d[eng.COL_DAYS] > sla_days

    flag = d["repeatcall flag"].notna() if "repeatcall flag" in d else False
    if {"Customer Code", "Product"} <= set(d.columns):
        same = d.duplicated(["Customer Code", "Product"], keep=False) & d["Customer Code"].notna()
    else:
        same = False
    d["Repeat"] = flag | same

    esc = (d["ESCLATION LEVEL"] if "ESCLATION LEVEL" in d else pd.Series("", index=d.index)).fillna("").astype(str).str.strip()
    d["Esc"] = esc.where(~esc.str.lower().isin(["nan", "none"]), "")
    esc_pts = np.where(d["Esc"] == "", 0, np.where(d["Esc"].str.lower() == "esc level 1", PTS_ESC_L1, PTS_ESC_HIGHER))

    d["Priority"] = (d[eng.COL_DAYS].clip(upper=PTS_AGE_CAP)
                     + d["SLA breach"] * PTS_BREACH
                     + esc_pts
                     + d["Repeat"] * PTS_REPEAT
                     + (d["Reason tag"] == "No reason given") * PTS_NO_REASON)
    return d


def _flags(r) -> str:
    out = []
    if r["Esc"]:
        digits = "".join(ch for ch in r["Esc"] if ch.isdigit())
        out.append(f"ESC{digits}" if digits else r["Esc"].upper()[:9])
    if r["Repeat"]:
        out.append("REPEAT")
    return " ".join(out)


def build_chase_list(df, sla_days=SLA_DAYS, top_n=CHASE_TOP_N):
    d = prepare_pending(df, sla_days)
    breach = d[d["SLA breach"]]
    if breach.empty:
        return None, d
    top = breach.sort_values(["Priority", eng.COL_DAYS], ascending=False).head(top_n)
    return top, breach


def chase_summary(breach: pd.DataFrame, top_n, sla_days) -> str:
    tags = breach["Reason tag"].value_counts()
    parts = [f"Chase today: top {min(top_n, len(breach))} of {len(breach)} calls past SLA (>{sla_days} days).",
             f"Repeat: {int(breach['Repeat'].sum())} | Escalated: {int((breach['Esc'] != '').sum())}"]
    parts.append("Reasons: " + ", ".join(f"{k} {v}" for k, v in tags.items()))
    return "\n".join(parts)


# ------------------------------------------------------------- rendering
def _table_png(headers, rows, widths, title="", colour_cols=None, left_cols=(), total_row=False,
               total_colours=None) -> bytes:
    """colour_cols = {column_index: [colour per body row]}; total_row styles the last row."""
    colour_cols = colour_cols or {}
    if not eng.SHOW_IMAGE_TITLE:
        title = ""
    n_rows, n_cols = len(rows) + 1, len(headers)
    total_w = sum(widths)
    fig_h = 0.42 * n_rows + (0.6 if title else 0.15)
    fig, ax = plt.subplots(figsize=(total_w, fig_h), dpi=200)
    ax.axis("off")
    x_edges = np.cumsum([0] + list(widths)) / total_w
    row_h = 1.0 / (n_rows + (1.3 if title else 0))
    top_pad = 1.3 * row_h if title else 0

    def cell(r, c, text, bg="white", bold=False, ha="center"):
        y0 = 1 - (r + 1) * row_h - top_pad
        ax.add_patch(plt.Rectangle((x_edges[c], y0), x_edges[c + 1] - x_edges[c], row_h,
                                   facecolor=bg, edgecolor="#B8B8B8", lw=0.6, transform=ax.transAxes))
        tx = x_edges[c] + (0.5 * (x_edges[c + 1] - x_edges[c]) if ha == "center" else 0.008)
        ax.text(tx, y0 + row_h / 2, str(text), ha=ha, va="center", fontsize=9.5,
                fontweight="bold" if bold else "normal", transform=ax.transAxes)

    if title:
        ax.text(0.0, 1 - 0.6 * row_h, title, fontsize=9, color="#444444", va="center", transform=ax.transAxes)
    for c, h in enumerate(headers):
        cell(0, c, h, "#DCE6F1", True)
    for r, row in enumerate(rows, start=1):
        is_total = total_row and r == len(rows)
        for c, v in enumerate(row):
            if is_total:
                cell(r, c, v, (total_colours or {}).get(c, "#DCE6F1"), True)
            else:
                bg = colour_cols[c][r - 1] if c in colour_cols else "white"
                cell(r, c, v, bg, ha="left" if c in left_cols else "center")
    ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.08, facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def _short(v, n):
    s = "" if pd.isna(v) else str(v)
    return s if len(s) <= n else s[:n - 1] + "…"


def render_chase_list(top: pd.DataFrame, sla_days, total_breach) -> bytes:
    headers = ["#", "Ticket", "Franchise", "Executive", "Days", "Reason", "Flags"]
    widths = [0.4, 1.15, 3.2, 2.4, 0.6, 1.9, 1.5]
    rows, colours = [], []
    for i, (_, r) in enumerate(top.iterrows(), start=1):
        rows.append([i, int(r["Ticket No"]) if pd.notna(r["Ticket No"]) else "",
                     _short(r["Franchise Name"], 27), _short(r[eng.COL_EXEC].title(), 21),
                     int(r[eng.COL_DAYS]), r["Reason tag"], _flags(r)])
        d = r[eng.COL_DAYS]
        colours.append("#F8696B" if d >= 15 else "#FFC57A" if d >= 7 else "#FFEB84")
    title = f"Chase today - top {len(rows)} of {total_breach} calls past SLA (> {sla_days} days)"
    return _table_png(headers, rows, widths, title, colour_cols={4: colours}, left_cols=(2, 3))


# ------------------------------------------------------------ CSAT scorecard
def load_csat(source) -> pd.DataFrame:
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    raw = pd.read_excel(source, header=None)
    hdr = next((i for i in range(min(10, len(raw))) if "FeedBack" in raw.iloc[i].astype(str).values), 2)
    df = raw.iloc[hdr + 1:].copy()
    df.columns = [str(c).strip() for c in raw.iloc[hdr]]
    df = df.dropna(subset=["FeedBack"])
    df["FeedBack"] = df["FeedBack"].astype(str).str.strip().str.upper()
    return df


def csat_scorecard(source, top_n=10, min_n=MIN_RESPONSES):
    """Returns (png, summary). Franchises ranked worst-first by % NOT HAPPY (+ OK)."""
    df = load_csat(source)
    total = len(df)
    fb = df["FeedBack"].value_counts()
    happy, ok, nh = int(fb.get("HAPPY", 0)), int(fb.get("OK", 0)), int(fb.get("NOT HAPPY", 0))
    g = pd.crosstab(df["FrName"], df["FeedBack"])
    for c in ("HAPPY", "OK", "NOT HAPPY"):
        if c not in g:
            g[c] = 0
    g["n"] = g[["HAPPY", "OK", "NOT HAPPY"]].sum(axis=1)
    # Use the preferred minimum when enough franchises qualify; for small files lower it automatically.
    chosen = 1
    for m in sorted({min_n, 10, 5, 3, 2, 1}, reverse=True):
        if m <= min_n and (g["n"] >= m).sum() >= top_n:
            chosen = m
            break
    min_n = chosen
    g = g[g["n"] >= min_n].copy()
    if g.empty:
        return None, "No franchise has any CSAT responses in this file."
    g["happy_pct"] = 100 * g["HAPPY"] / g["n"]
    g["nh_pct"] = 100 * g["NOT HAPPY"] / g["n"]
    worst = g.sort_values(["nh_pct", "happy_pct"], ascending=[False, True]).head(top_n)

    headers = ["Rank", "Franchise", "Responses", "Happy %", "OK", "Not happy"]
    widths = [0.6, 3.2, 1.1, 1.0, 0.7, 1.1]
    rows, colours = [], []
    for i, (name, r) in enumerate(worst.iterrows(), start=1):
        rows.append([i, _short(name, 32), int(r["n"]), f"{r['happy_pct']:.1f}", int(r["OK"]), int(r["NOT HAPPY"])])
        colours.append("#F8696B" if r["happy_pct"] < 90 else "#FFC57A" if r["happy_pct"] < 95 else "#FFEB84")
    src = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    import re
    dates = re.findall(r"\d+/\d+/\d{4}", str(pd.read_excel(src, header=None, nrows=2).iloc[1, 0]))
    period = f" | {dates[0]} to {dates[-1]}" if dates else ""
    title = f"Franchise CSAT - lowest {len(rows)} by % not happy (min {min_n} responses){period}"
    png = _table_png(headers, rows, widths, title, colour_cols={3: colours}, left_cols=(1,))
    summary = (f"CSAT: {total} responses | Happy {100*happy/total:.1f}% | OK {100*ok/total:.1f}% | "
               f"Not happy {nh} ({100*nh/total:.1f}%).\n"
               f"{len(g)} franchises ranked (min {min_n} responses); showing the lowest {len(rows)}.")
    return png, summary


# ------------------------------------------------- ageing summary (0..N days + ">N")
def ageing_summary(source, upto=4, standard_filters=True):
    """Executive x days-old table: columns 0..upto, Grand Total (all calls) and '>upto Days'.
    Returns (png, summary)."""
    upto = max(1, min(int(upto), 15))
    df = eng.load_report(source)
    d = df
    if standard_filters:
        d = d[~d[eng.COL_TYPE].isin(eng.STD_EXCLUDE_TYPES)]
        d = d[~d[eng.COL_REASON].astype(str).str.strip().str.lower()
              .isin([r.lower() for r in eng.STD_EXCLUDE_REASONS])]
    if d.empty:
        return None, "No pending calls found with those filters."
    ct = pd.crosstab(d[eng.COL_EXEC], d[eng.COL_DAYS].clip(upper=upto + 1))
    ct = ct.reindex(columns=range(upto + 2), fill_value=0)
    out = ct[list(range(upto + 1))].copy()
    out["Grand Total"] = ct.sum(axis=1)
    out[f">{upto - 1} Days"] = ct[upto] + ct[upto + 1]      # N-1 column, placed beside N
    out[f">{upto} Days"] = ct[upto + 1]
    out = out.sort_values("Grand Total", ascending=False)
    body = out.copy()
    total = body.sum()

    n = upto + 1                                  # number of day columns
    headers = ["Row Labels"] + [str(i) for i in range(n)] + ["Grand Total", f">{upto - 1} Days", f">{upto} Days"]
    name_w = max(3.6, 0.13 * max(len(x) for x in body.index) + 0.6)
    widths = [name_w] + [0.6] * n + [1.3, 1.2, 1.2]
    rows = [[name] + ["" if v == 0 else int(v) for v in r.values[:n]] + [int(v) for v in r.values[n:]]
            for name, r in body.iterrows()]
    rows.append(["Grand Total"] + [int(v) for v in total.values])
    colours = {n + 1: eng._excel_3_colour_scale(body["Grand Total"].values),
               n + 2: eng._excel_3_colour_scale(body[f">{upto - 1} Days"].values),
               n + 3: eng._excel_3_colour_scale(body[f">{upto} Days"].values)}
    title = "Pending calls by days old" + (" (standard filters applied)" if standard_filters else " (no filters)")
    png = _table_png(headers, rows, widths, title, colour_cols=colours, total_row=True)
    worst = body[f">{upto} Days"].idxmax()
    summary = (f"{int(total['Grand Total'])} pending calls | {int(total[f'>{upto - 1} Days'])} older than {upto - 1} days "
               f"| {int(total[f'>{upto} Days'])} older than {upto} days "
               f"| most overdue (>{upto}): {worst.title()} ({int(body.loc[worst, f'>{upto} Days'])})"
               + ("\n(standard filters applied)" if standard_filters else "")
               + "\nExcluded: Darma Raju E, Dantuluri Jayanth Varma, unassigned")
    return png, summary


# ----------------------------------------- CSAT by executive: CSS = (Happy - Not happy) / Total
def csat_by_executive(source):
    """Executive-wise CSAT. CSS = (HAPPY - NOT HAPPY) / Grand Total, where the total includes OK.
    Rows sorted worst CSS first. Returns (png, summary)."""
    df = load_csat(source)
    col = next((c for c in df.columns if c.strip().lower() == "branch exe name"),
               next((c for c in df.columns if "exe" in c.lower() and "name" in c.lower()), None))
    if col is None:
        raise ValueError("no executive-name column in this CSAT file (columns: " + ", ".join(df.columns[:12]) + ")")
    df[col] = df[col].fillna("(no executive)").astype(str).str.strip()
    df = eng.drop_excluded_executives(df, col)
    df["FeedBack"] = df["FeedBack"].replace({"NOTHAPPY": "NOT HAPPY", "NOT_HAPPY": "NOT HAPPY"})
    if df.empty:
        return None, "No CSAT responses left after removing the excluded executives."
    g = pd.crosstab(df[col], df["FeedBack"])
    for c in ("HAPPY", "NOT HAPPY", "OK"):
        if c not in g:
            g[c] = 0
    g["Total"] = g[["HAPPY", "NOT HAPPY", "OK"]].sum(axis=1)
    g["CSS"] = (g["HAPPY"] - g["NOT HAPPY"]) / g["Total"]
    g = g.sort_values(["CSS", "Total"], ascending=[True, False])
    tot = g[["HAPPY", "NOT HAPPY", "OK", "Total"]].sum()
    tot_css = (tot["HAPPY"] - tot["NOT HAPPY"]) / tot["Total"]

    headers = ["Row Labels", "HAPPY", "NOT HAPPY", "OK", "Grand Total", "CSS"]
    name_w = max(3.6, 0.13 * max(len(n) for n in g.index) + 0.6)
    widths = [name_w, 1.0, 1.3, 0.7, 1.3, 0.9]
    blank = lambda v: "" if v == 0 else int(v)
    rows = [[n, blank(r["HAPPY"]), blank(r["NOT HAPPY"]), blank(r["OK"]), int(r["Total"]), f"{r['CSS']*100:.0f}%"]
            for n, r in g.iterrows()]
    rows.append(["Grand Total", int(tot["HAPPY"]), int(tot["NOT HAPPY"]), int(tot["OK"]), int(tot["Total"]), f"{tot_css*100:.0f}%"])
    colours = {5: eng._excel_3_colour_scale(g["CSS"].values, reverse=True)}

    import re
    src = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    dates = re.findall(r"\d+/\d+/\d{4}", str(pd.read_excel(src, header=None, nrows=2).iloc[1, 0]))
    period = f" | {dates[0]} to {dates[-1]}" if dates else ""
    title = f"CSS = (Happy - Not happy) / Total incl. OK{period}"
    png = _table_png(headers, rows, widths, title, colour_cols=colours, total_row=True)
    low = g.index[0]
    summary = (f"CSS overall {tot_css*100:.0f}% on {int(tot['Total'])} responses. "
               f"Lowest: {low.title()} {g.loc[low, 'CSS']*100:.0f}%.\nCSS = (Happy - Not happy) / Total incl. OK")
    return png, summary


# ------------------------- franchise-wise CSS (one executive's franchises) + "Yet to get 95%"
def _csat_with_exec(source):
    """CSAT frame with a cleaned executive column; excluded executives removed."""
    df = load_csat(source)
    col = next((c for c in df.columns if c.strip().lower() == "branch exe name"),
               next((c for c in df.columns if "exe" in c.lower() and "name" in c.lower()), None))
    if col is None:
        raise ValueError("no executive-name column in this CSAT file (columns: " + ", ".join(df.columns[:12]) + ")")
    df[col] = df[col].fillna("(no executive)").astype(str).str.strip()
    df = eng.drop_excluded_executives(df, col)
    df["FeedBack"] = df["FeedBack"].replace({"NOTHAPPY": "NOT HAPPY", "NOT_HAPPY": "NOT HAPPY"})
    return df, col


def csat_executives(source):
    """Executives in a CSAT file with their response counts (most first)."""
    df, col = _csat_with_exec(source)
    return df[col].value_counts()


def _css_colour(css, target):
    """Green (deeper the further above target) when CSS >= target, red below it."""
    if css >= target:
        t = 0.0 if target >= 1 else min(1.0, max(0.0, (css - target) / (1 - target)))
        a, b = (169, 221, 142), (99, 190, 123)
        return tuple((a[i] + (b[i] - a[i]) * t) / 255 for i in range(3))
    return (248 / 255, 105 / 255, 107 / 255)


def csat_franchise_css(source, executive=None, target_pct=CSS_TARGET_PCT):
    """Franchise-wise CSS. executive=None -> every franchise; otherwise that executive's franchises.
    CSS = (HAPPY - NOT HAPPY) / Total incl. OK.
    'Yet to get 95%' = extra HAPPY responses still needed to reach the target (negative = ahead):
        ceil((target*Total - 100*(HAPPY - NOT HAPPY)) / (100 - target)).
    Rows sorted by CSS, best first. Returns (png, summary)."""
    df, col = _csat_with_exec(source)
    if executive:
        df = df[df[col].str.lower() == executive.strip().lower()]
    if df.empty:
        return None, "No CSAT responses found for that selection."
    df = df.assign(FR=df["FrName"].astype(str).str.strip())
    g = pd.crosstab(df["FR"], df["FeedBack"])
    for c in ("HAPPY", "NOT HAPPY", "OK"):
        if c not in g:
            g[c] = 0
    g["Total"] = g[["HAPPY", "NOT HAPPY", "OK"]].sum(axis=1)
    g["CSS"] = (g["HAPPY"] - g["NOT HAPPY"]) / g["Total"]

    def need(h, nh, t):
        num = target_pct * t - 100 * (h - nh)
        return int(-((-num) // (100 - target_pct)))          # integer ceiling
    g["Need"] = [need(r["HAPPY"], r["NOT HAPPY"], r["Total"]) for _, r in g.iterrows()]
    g = g.reset_index()
    g = g.sort_values(["CSS", "FR"], ascending=[False, True])

    tot = g[["HAPPY", "NOT HAPPY", "OK", "Total"]].sum()
    tot_css = (tot["HAPPY"] - tot["NOT HAPPY"]) / tot["Total"]
    tot_need = need(tot["HAPPY"], tot["NOT HAPPY"], tot["Total"])

    tgt = target_pct / 100

    def fmt(v):                     # whole percent, but one decimal when rounding would cross the target line
        p = v * 100
        return f"{p:.1f}%" if (round(p) >= target_pct > p) else f"{p:.0f}%"
    headers = ["FR Name", "HAPPY", "NOT HAPPY", "OK", "Grand Total", "CSS", f"Yet to get {target_pct}%"]
    name_w = max(3.6, 0.13 * max(len(n) for n in g["FR"]) + 0.6)
    widths = [name_w, 1.0, 1.3, 0.7, 1.3, 0.9, 1.9]
    blank = lambda v: "" if v == 0 else int(v)
    rows = [[r["FR"], blank(r["HAPPY"]), blank(r["NOT HAPPY"]), blank(r["OK"]), int(r["Total"]),
             fmt(r["CSS"]), int(r["Need"])] for _, r in g.iterrows()]
    rows.append(["Grand Total", int(tot["HAPPY"]), int(tot["NOT HAPPY"]), int(tot["OK"]), int(tot["Total"]),
                 fmt(tot_css), int(tot_need)])
    need_cols = eng._excel_3_colour_scale(list(g["Need"].values) + [tot_need])
    colours = {5: [_css_colour(v, tgt) for v in g["CSS"].values], 6: need_cols[:-1]}
    total_colours = {5: _css_colour(tot_css, tgt), 6: need_cols[-1]}
    png = _table_png(headers, rows, widths, "", colour_cols=colours, total_row=True, total_colours=total_colours)

    who = f"{executive.title()}: " if executive else ""
    below = int((g["CSS"] * 100 < target_pct).sum())
    ahead = (f"{-tot_need} responses ahead of {target_pct}%" if tot_need < 0
             else f"{tot_need} more HAPPY needed to reach {target_pct}%")
    summary = (f"{who}CSS {tot_css*100:.1f}% on {int(tot['Total'])} responses across {len(g)} franchises. "
               f"Below {target_pct}%: {below}. Overall: {ahead}.\n"
               f"CSS = (Happy - Not happy) / Total incl. OK. Yet to get {target_pct}% = extra HAPPY needed "
               f"(negative = ahead).")
    return png, summary
