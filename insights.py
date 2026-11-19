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
CSS_TARGETS = (95, 96)       # "Yet to get X%" columns, in this order
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
    if "mandatory appointment" in title:
        return "mandatory"
    if "pending call" in title:
        return "pending"
    if "customer sat" in title:
        return "csat"
    if "soft closure" in title:
        return "softclosure"
    if "calls regist" in title:
        return "registered"
    if {"Visit Status", "Appt Date-CRM", "BSE Name"} <= cols:
        return "mandatory"
    if {"DAYS OLD", "Pending Reason", "Spare"} <= cols:
        return "pending"
    if {"FeedBack", "TicketNo"} <= cols:
        return "csat"
    if {"DAYS OLD", "CLOSED TIME"} <= cols:
        return "registered"
    if {"Soft Closure Date", "Call Book Date", "Reason"} <= cols and "CLOSED TIME" not in cols:
        return "softclosure"
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
    df = source if isinstance(source, pd.DataFrame) else eng.load_report(source)
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


def _css_need(h, nh, t, target):
    """Extra HAPPY responses a row still needs to reach `target`% CSS (0 = already there)."""
    num = target * t - 100 * (h - nh)
    return max(0, int(-((-num) // (100 - target))))              # integer ceiling


def _need_colour_fn(values):
    """0 -> green; more needed -> yellow to red (scaled to the largest value in the column)."""
    vmax = max([int(v) for v in values] + [1])

    def f(v):
        if v <= 0:
            return (99 / 255, 190 / 255, 123 / 255)
        t = min(1.0, v / vmax)
        a, b = (255, 235, 132), (248, 105, 107)
        return tuple((a[i] + (b[i] - a[i]) * t) / 255 for i in range(3))
    return f


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

    needs = {t: [_css_need(r["HAPPY"], r["NOT HAPPY"], r["Total"], t) for _, r in g.iterrows()] for t in CSS_TARGETS}
    headers = ["Row Labels", "HAPPY", "NOT HAPPY", "OK", "Grand Total", "CSS"] + [f"Yet to get {t}%" for t in CSS_TARGETS]
    name_w = max(3.6, 0.13 * max(len(n) for n in g.index) + 0.6)
    widths = [name_w, 1.0, 1.3, 0.7, 1.3, 0.9] + [1.9] * len(CSS_TARGETS)
    blank = lambda v: "" if v == 0 else int(v)
    rows = [[n, blank(r["HAPPY"]), blank(r["NOT HAPPY"]), blank(r["OK"]), int(r["Total"]), f"{r['CSS']*100:.0f}%"]
            + [needs[t][i] for t in CSS_TARGETS] for i, (n, r) in enumerate(g.iterrows())]
    need_tot = {t: int(sum(needs[t])) for t in CSS_TARGETS}          # sum of the rows, not recomputed from totals
    rows.append(["Grand Total", int(tot["HAPPY"]), int(tot["NOT HAPPY"]), int(tot["OK"]), int(tot["Total"]), f"{tot_css*100:.0f}%"]
                + [need_tot[t] for t in CSS_TARGETS])
    colours = {5: eng._excel_3_colour_scale(g["CSS"].values, reverse=True)}
    total_colours = {}
    for k, t in enumerate(CSS_TARGETS):
        f = _need_colour_fn(needs[t] + [need_tot[t]])
        colours[6 + k] = [f(v) for v in needs[t]]
        total_colours[6 + k] = f(need_tot[t])

    import re
    src = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    dates = re.findall(r"\d+/\d+/\d{4}", str(pd.read_excel(src, header=None, nrows=2).iloc[1, 0]))
    period = f" | {dates[0]} to {dates[-1]}" if dates else ""
    title = f"CSS = (Happy - Not happy) / Total incl. OK{period}"
    png = _table_png(headers, rows, widths, title, colour_cols=colours, total_row=True, total_colours=total_colours)
    low = g.index[0]
    yet = ", ".join(f"{need_tot[t]} for {t}%" for t in CSS_TARGETS)
    summary = (f"CSS overall {tot_css*100:.0f}% on {int(tot['Total'])} responses. "
               f"Lowest: {low.title()} {g.loc[low, 'CSS']*100:.0f}%.\n"
               f"Extra HAPPY still needed (sum of executives): {yet}.\nCSS = (Happy - Not happy) / Total incl. OK")
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


def csat_franchise_css(source, executive=None, targets=CSS_TARGETS):
    """Franchise-wise CSS. executive=None -> every franchise; otherwise that executive's franchises.
    CSS = (HAPPY - NOT HAPPY) / Total incl. OK.
    'Yet to get X%' = extra HAPPY responses a franchise still needs to reach X% (0 = already there):
        ceil((X*Total - 100*(HAPPY - NOT HAPPY)) / (100 - X)).
    The Grand Total of each 'Yet to get' column is the SUM of the franchise rows.
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
    for t in targets:
        g[f"Need{t}"] = [_css_need(r["HAPPY"], r["NOT HAPPY"], r["Total"], t) for _, r in g.iterrows()]
    g = g.reset_index()
    g = g.sort_values(["CSS", "FR"], ascending=[False, True])

    tot = g[["HAPPY", "NOT HAPPY", "OK", "Total"]].sum()
    tot_css = (tot["HAPPY"] - tot["NOT HAPPY"]) / tot["Total"]
    need_tot = {t: int(g[f"Need{t}"].sum()) for t in targets}        # sum of the franchise rows
    main = targets[0]
    tgt = main / 100

    def fmt(v):                     # whole percent, but one decimal when rounding would cross the target line
        p = v * 100
        return f"{p:.1f}%" if (round(p) >= main > p) else f"{p:.0f}%"
    headers = ["FR Name", "HAPPY", "NOT HAPPY", "OK", "Grand Total", "CSS"] + [f"Yet to get {t}%" for t in targets]
    name_w = max(3.6, 0.13 * max(len(n) for n in g["FR"]) + 0.6)
    widths = [name_w, 1.0, 1.3, 0.7, 1.3, 0.9] + [1.9] * len(targets)
    blank = lambda v: "" if v == 0 else int(v)
    rows = [[r["FR"], blank(r["HAPPY"]), blank(r["NOT HAPPY"]), blank(r["OK"]), int(r["Total"]), fmt(r["CSS"])]
            + [int(r[f"Need{t}"]) for t in targets] for _, r in g.iterrows()]
    rows.append(["Grand Total", int(tot["HAPPY"]), int(tot["NOT HAPPY"]), int(tot["OK"]), int(tot["Total"]), fmt(tot_css)]
                + [need_tot[t] for t in targets])
    colours = {5: [_css_colour(v, tgt) for v in g["CSS"].values]}
    total_colours = {5: _css_colour(tot_css, tgt)}
    for k, t in enumerate(targets):
        f = _need_colour_fn(list(g[f"Need{t}"].values) + [need_tot[t]])
        colours[6 + k] = [f(int(v)) for v in g[f"Need{t}"].values]
        total_colours[6 + k] = f(need_tot[t])
    png = _table_png(headers, rows, widths, "", colour_cols=colours, total_row=True, total_colours=total_colours)

    who = f"{executive.title()}: " if executive else ""
    below = int((g["CSS"] * 100 < main).sum())
    yet = ", ".join(f"{need_tot[t]} for {t}%" for t in targets)
    summary = (f"{who}CSS {tot_css*100:.1f}% on {int(tot['Total'])} responses across {len(g)} franchises. "
               f"Below {main}%: {below}. Extra HAPPY still needed (sum of franchises): {yet}.\n"
               f"CSS = (Happy - Not happy) / Total incl. OK. Yet to get X% = extra HAPPY needed per franchise "
               f"(0 = already at target).")
    return png, summary


# ================================================================ Soft closure reports
# Calls soft-closed on the report day: executive-wise, all executives combined, and one
# executive's franchises. Days taken = soft closure date - call book date (both mm/dd/yyyy).
SOFT_EXCLUDE_TYPES = ["COURTESY VISIT", "MANDATORY CALLS", "MANDATORY CALL"]    # not counted, as in the daily report
SOFT_MAX_DAY = 9                                              # days 0..9 shown one by one, more -> "10+"


def _parse_us_dates(series: pd.Series) -> pd.Series:
    """Parse mm/dd/yyyy (optionally with a time). Real Excel dates and ISO text also work."""
    out = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    text = series.astype(str).str.strip()
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M %p",
                "%m/%d/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        todo = out.isna()
        if not todo.any():
            break
        out[todo] = pd.to_datetime(text[todo], format=fmt, errors="coerce")
    return out


def load_soft_closure(source) -> pd.DataFrame:
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    raw = pd.read_excel(source, header=None)
    hdr = next((i for i in range(min(10, len(raw))) if "Soft Closure Date" in raw.iloc[i].astype(str).values), None)
    if hdr is None:
        raise ValueError("no 'Soft Closure Date' column - is this the Soft Closure report?")
    df = raw.iloc[hdr + 1:].copy()
    df.columns = [str(c).strip() for c in raw.iloc[hdr]]
    df = df.dropna(how="all")
    missing = [c for c in ("Call Book Date", "Soft Closure Date", "Branch Exe Name", "Franchise Name", "Call Type")
               if c not in df.columns]
    if missing:
        raise ValueError("missing column(s): " + ", ".join(missing))
    if "Ticket No" in df.columns:
        df = df.drop_duplicates(subset="Ticket No", keep="first")
    df["_cb"] = _parse_us_dates(df["Call Book Date"])
    df["_sc"] = _parse_us_dates(df["Soft Closure Date"])
    bad = df["_cb"].isna() | df["_sc"].isna()
    if len(df) and bad.mean() > 0.2:
        raise ValueError("could not read the dates (expected mm/dd/yyyy)")
    df = df[~bad].copy()
    df["days"] = (df["_sc"].dt.normalize() - df["_cb"].dt.normalize()).dt.days.clip(lower=0)
    df["_day"] = df["_sc"].dt.normalize()
    df["Exe"] = df["Branch Exe Name"].fillna("(no executive)").astype(str).str.strip().replace("", "(no executive)")
    df["FR"] = df["Franchise Name"].fillna("(no franchise)").astype(str).str.strip()
    df["CT"] = df["Call Type"].fillna("(no type)").astype(str).str.strip().str.upper()
    tech = df["Technician Name"] if "Technician Name" in df.columns else pd.Series(index=df.index, dtype=object)
    code = df["TECHNICIAN CODE"] if "TECHNICIAN CODE" in df.columns else pd.Series(index=df.index, dtype=object)
    t = tech.fillna(code).astype(str).str.replace(r"\s+", " ", regex=True).str.strip().str.upper()
    df["Tech"] = t.where(tech.notna() | code.notna(), "(NO TECHNICIAN)").replace({"": "(NO TECHNICIAN)", "NAN": "(NO TECHNICIAN)"})
    return df


_SOFT_CACHE = {"key": None, "df": None}     # last parsed soft closure file, so each franchise pick does not re-read the Excel


def _soft_loaded(source):
    if isinstance(source, pd.DataFrame):
        return source
    key = hash(bytes(source)) if isinstance(source, (bytes, bytearray)) else None
    if key is not None and _SOFT_CACHE["key"] == key:
        return _SOFT_CACHE["df"]
    df = load_soft_closure(source)
    if key is not None:
        _SOFT_CACHE.update(key=key, df=df)
    return df


def _soft_scope(source):
    """Calls soft-closed on the report day, after the standard exclusions. Returns (frame, info)."""
    df = _soft_loaded(source)
    if df.empty:
        raise ValueError("no soft closed calls found in this file")
    day = df["_day"].max()
    that_day = df[df["_day"] == day]
    kept = that_day[~that_day["CT"].isin(SOFT_EXCLUDE_TYPES)]
    excluded_types = {t: int((that_day["CT"] == t).sum()) for t in SOFT_EXCLUDE_TYPES}
    kept = eng.drop_excluded_executives(kept, "Exe")
    info = {"date": day.strftime("%m/%d/%Y"), "excluded_types": excluded_types,
            "days_in_file": int(df["_day"].nunique())}
    return kept, info


def _soft_notes(info) -> str:
    ex = ", ".join(f"{v} {k.title()}" for k, v in info["excluded_types"].items() if v)
    note = f"Excludes {ex}." if ex else ""
    if info["days_in_file"] > 1:
        note += f" File covers {info['days_in_file']} days; showing the latest."
    return note.strip()


def _days_report(df, rowcol, header, order):
    """Row field x days-taken table with Grand Total (high = green) and a Grand Total row."""
    over = bool((df["days"] > SOFT_MAX_DAY).any())
    days = df["days"].clip(upper=SOFT_MAX_DAY + 1)
    ct = pd.crosstab(df[rowcol], days)
    # like the executive's pivot table: only day columns that actually occur
    cols = [c for c in range(SOFT_MAX_DAY + 1) if c in ct.columns] + ([SOFT_MAX_DAY + 1] if over else [])
    ct = ct.reindex(columns=cols, fill_value=0)
    ct["Grand Total"] = ct.sum(axis=1)
    if order == "alpha":
        ct = ct.loc[sorted(ct.index, key=lambda n: str(n).lower())]
    else:
        ct = ct.sort_values("Grand Total", ascending=False)
    total = ct.sum()
    heads = [header] + [str(c) if c <= SOFT_MAX_DAY else f"{SOFT_MAX_DAY + 1}+" for c in cols] + ["Closed Calls"]
    name_w = max(3.6, 0.13 * max(len(str(n)) for n in ct.index) + 0.6)
    widths = [name_w] + [0.6] * len(cols) + [1.3]
    rows = [[n] + ["" if v == 0 else int(v) for v in r.values[:-1]] + [int(r.values[-1])] for n, r in ct.iterrows()]
    rows.append(["Closed Calls"] + [int(v) for v in total.values])
    colours = {len(cols) + 1: eng._excel_3_colour_scale(ct["Grand Total"].values, reverse=True)}
    png = _table_png(heads, rows, widths, "", colour_cols=colours, total_row=True)
    return png, ct


def _same_day(df):
    n = len(df)
    s = int((df["days"] == 0).sum())
    return s, (100 * s / n if n else 0)


def soft_closure_executives(source):
    kept, _ = _soft_scope(source)
    return kept["Exe"].value_counts()


def soft_closure_by_executive(source):
    """Calls soft-closed on the report day, executive-wise; the Grand Total row is all executives combined."""
    kept, info = _soft_scope(source)
    if kept.empty:
        return None, f"No soft closed calls on {info['date']} after the exclusions."
    png, ct = _days_report(kept, "Exe", "Row Labels", "total")
    s, pct = _same_day(kept)
    top = ct.index[0]
    summary = (f"Soft closed on {info['date']}: {len(kept)} calls by {len(ct)} executives (combined total)"
               f" | same-day {s} ({pct:.0f}%)\nMost: {top.title()} {int(ct.loc[top, 'Grand Total'])}. "
               + _soft_notes(info))
    return png, summary.strip()


def soft_closure_combined(source):
    """All executives combined, split by call type."""
    kept, info = _soft_scope(source)
    if kept.empty:
        return None, f"No soft closed calls on {info['date']} after the exclusions."
    kept = kept.assign(CTn=kept["CT"].str.title())
    png, ct = _days_report(kept, "CTn", "Call Type", "total")
    s, pct = _same_day(kept)
    status = ""
    if "Status" in kept.columns:
        status = " | " + ", ".join(f"{k} {v}" for k, v in kept["Status"].value_counts().items())
    summary = (f"All executives combined, soft closed on {info['date']}: {len(kept)} calls"
               f" | same-day {s} ({pct:.0f}%){status}\n" + _soft_notes(info))
    return png, summary.strip()


def soft_closure_franchises(source, executive):
    """One executive's franchises x days taken (franchise names A to Z)."""
    kept, info = _soft_scope(source)
    sub = kept[kept["Exe"].str.lower() == executive.strip().lower()]
    if sub.empty:
        return None, f"No soft closed calls for {executive.title()} on {info['date']} after the exclusions."
    png, ct = _days_report(sub, "FR", "FR Name", "alpha")
    s, pct = _same_day(sub)
    summary = (f"{executive.title()}: {len(sub)} calls soft closed on {info['date']} across {len(ct)} franchises"
               f" | same-day {s} ({pct:.0f}%)\n" + _soft_notes(info))
    return png, summary.strip()


def soft_closure_franchise_counts(source, executive):
    """Franchises of one executive with calls soft closed on the report day (most calls first)."""
    kept, _ = _soft_scope(source)
    sub = kept[kept["Exe"].str.lower() == executive.strip().lower()]
    return sub["FR"].value_counts().sort_values(ascending=False, kind="stable")


def soft_closure_technicians(source, executive, franchise):
    """One franchise's technicians with calls soft closed on the report day: Technician | Closed Calls.
    Courtesy Visit and Mandatory Calls are already excluded by _soft_scope; no days-taken columns."""
    kept, info = _soft_scope(source)
    sub = kept[(kept["Exe"].str.lower() == executive.strip().lower())
               & (kept["FR"].str.lower() == franchise.strip().lower())]
    if sub.empty:
        return None, f"No soft closed calls for {franchise.title()} on {info['date']} after the exclusions."
    ct = sub["Tech"].value_counts().sort_values(ascending=False, kind="stable")
    name_w = max(3.6, 0.13 * max(len(str(n)) for n in ct.index) + 0.6)
    rows = [[n, int(v)] for n, v in ct.items()] + [["Closed Calls", int(ct.sum())]]
    colours = {1: eng._excel_3_colour_scale(ct.values, reverse=True)}
    png = _table_png(["Technician", "Closed Calls"], rows, [name_w, 1.5], "", colour_cols=colours, total_row=True)
    summary = (f"{franchise.title()} ({executive.title()}): {len(sub)} calls soft closed on {info['date']} "
               f"by {len(ct)} technicians\n" + _soft_notes(info))
    return png, summary.strip()


# ============================================ Franchise call lists (one image per franchise)
# For one executive: every franchise gets its own call-list image in the layout the executive makes by hand:
# Franchise Name | Ticket No | Customer Name | Pincode | Call Type | Product | Machine Status | Call Book Date | DAYS OLD
# sorted DAYS OLD high to low (newest booking first within a day). Rows 2+ days old are solid red;
# 0 and 1 day old are red text on white.
CALL_LIST_COLUMNS = ["Franchise Name", "Ticket No", "Customer Name", "Pincode", "Call Type",
                     "Product", "Machine Status", "Call Book Date", "DAYS OLD"]
CALL_LIST_RED_FROM = 2          # DAYS OLD >= this -> whole row red
CALL_LIST_DPI = 130             # image sharpness; 200 was slower and heavier on the free tier
CALL_LIST_PAGE_ROWS = 40        # longer franchises are split into pages of this many rows


def _fmt_book_date(ts, original) -> str:
    """mm/dd/yyyy h:mm:ss AM/PM, as in the portal and the executive's sheet."""
    if pd.isna(ts):
        return "" if pd.isna(original) else str(original)
    h = ts.hour % 12 or 12
    return f"{ts.month:02d}/{ts.day:02d}/{ts.year} {h}:{ts.minute:02d}:{ts.second:02d} {'AM' if ts.hour < 12 else 'PM'}"


def _cell_text(v, blank="") -> str:
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return blank
    s = str(v).strip()
    return s[:-2] if s.endswith(".0") and s[:-2].isdigit() else (s or blank)


def render_call_list(rows: pd.DataFrame) -> bytes:
    """Excel-style call list image: blue header with filter buttons, red rows / red text."""
    from matplotlib.patches import Polygon, Rectangle
    body, is_red = [], []
    for _, r in rows.iterrows():
        body.append([_cell_text(r["_fr"]), _cell_text(r["Ticket No"]), _cell_text(r["Customer Name"]),
                     _cell_text(r["Pincode"]), _cell_text(r["Call Type"]), _cell_text(r["Product"]),
                     _cell_text(r["Machine Status"], "NA"), _fmt_book_date(r["_cb"], r["Call Book Date"]),
                     _cell_text(int(r["DAYS OLD"]))])
        is_red.append(int(r["DAYS OLD"]) >= CALL_LIST_RED_FROM)

    probe = plt.figure(dpi=100)
    renderer = probe.canvas.get_renderer()

    def text_w(s):
        t = probe.text(0, 0, s, fontsize=10, fontweight="bold")
        w = t.get_window_extent(renderer).width / probe.dpi
        t.remove()
        return w
    widths = []
    for c, head in enumerate(CALL_LIST_COLUMNS):
        longest = sorted({row[c] for row in body}, key=len, reverse=True)[:3]
        widths.append(max([text_w(head) + 0.32] + [text_w(x) for x in longest]) + 0.3)
    plt.close(probe)

    row_h = 0.34
    total_w, n_rows = sum(widths), len(body) + 1
    fig = plt.figure(figsize=(total_w, row_h * n_rows), dpi=CALL_LIST_DPI)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, total_w); ax.set_ylim(0, row_h * n_rows); ax.axis("off")
    xs = np.concatenate([[0], np.cumsum(widths)])

    def top(r):                       # y of the bottom edge of row r (row 0 = header)
        return row_h * (n_rows - 1 - r)

    for c, head in enumerate(CALL_LIST_COLUMNS):               # header
        x, w, y = xs[c], widths[c], top(0)
        ax.add_patch(Rectangle((x, y), w, row_h, facecolor="#5B9BD5", edgecolor="#9DC3E6", lw=0.8))
        ax.text(x + (w - 0.2) / 2, y + row_h / 2, head, ha="center", va="center", fontsize=10,
                fontweight="bold", color="white")
        bx, by = x + w - 0.24, y + (row_h - 0.2) / 2           # filter button
        ax.add_patch(Rectangle((bx, by), 0.2, 0.2, facecolor="#F2F2F2", edgecolor="#7F7F7F", lw=0.6))
        cx, cy = bx + 0.1, by + 0.1
        ax.add_patch(Polygon([(cx - 0.045, cy + 0.025), (cx + 0.045, cy + 0.025), (cx, cy - 0.035)],
                             closed=True, facecolor="black", edgecolor="none"))
    for r, row in enumerate(body, start=1):                     # data rows
        red = is_red[r - 1]
        for c, val in enumerate(row):
            x, w, y = xs[c], widths[c], top(r)
            ax.add_patch(Rectangle((x, y), w, row_h, facecolor="#FF0000" if red else "white",
                                   edgecolor="#BFBFBF", lw=0.6))
            ax.text(x + w / 2, y + row_h / 2, val, ha="center", va="center", fontsize=10,
                    fontweight="bold", color="black" if red else "#FF0000")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=CALL_LIST_DPI, facecolor="white", bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)
    return buf.getvalue()


def _call_list_scope(source, executive):
    """One executive's pending calls after the standard filters, with parsed date + franchise columns."""
    df = source.copy() if isinstance(source, pd.DataFrame) else eng.load_report(source)
    d = df[df[eng.COL_EXEC].str.strip().str.lower() == executive.strip().lower()]
    d = d[~d[eng.COL_TYPE].isin(eng.STD_EXCLUDE_TYPES)]
    d = d[~d[eng.COL_REASON].astype(str).str.strip().str.lower().isin([r.lower() for r in eng.STD_EXCLUDE_REASONS])]
    if d.empty:
        return d
    return d.assign(_cb=_parse_us_dates(d["Call Book Date"]),
                    _fr=d["Franchise Name"].fillna("(no franchise)").astype(str).str.strip())


def call_list_franchises(source, executive, min_age=0):
    """Franchises of one executive with pending calls (DAYS OLD >= min_age), A to Z -> Series {name: count}."""
    d = _call_list_scope(source, executive)
    if not d.empty and min_age:
        d = d[d["DAYS OLD"] >= min_age]
    if d.empty:
        return pd.Series(dtype=int)
    c = d["_fr"].value_counts()
    return c.reindex(sorted(c.index, key=str.lower))


def franchise_call_list(source, executive, franchise, min_age=0):
    """Call list image(s) for ONE franchise, or a GROUP of franchises combined (pass a list of names).
    min_age=2 keeps only calls 2+ days old. Returns [(png, caption), ...]."""
    names = [franchise] if isinstance(franchise, str) else list(franchise)
    want = {n.strip().lower() for n in names}
    d = _call_list_scope(source, executive)
    g = d[d["_fr"].str.lower().isin(want)] if not d.empty else d
    if not g.empty and min_age:
        g = g[g["DAYS OLD"] >= min_age]
    if g.empty:
        return []
    shown = sorted(g["_fr"].unique(), key=str.lower)
    label = shown[0] if len(shown) == 1 else " + ".join(shown)
    if len(label) > 120:
        label = f"{len(shown)} franchises"
    g = g.assign(_k=g["_fr"].str.lower()).sort_values(["_k", "DAYS OLD", "_cb"], ascending=[True, False, False])
    red = int((g["DAYS OLD"] >= CALL_LIST_RED_FROM).sum())
    chunks = [g.iloc[i:i + CALL_LIST_PAGE_ROWS] for i in range(0, len(g), CALL_LIST_PAGE_ROWS)]
    note = f" | only {min_age}+ days old" if min_age else ""
    pages = []
    for i, ch in enumerate(chunks, start=1):
        cap = (f"{label}: {len(g)} pending call{'s' if len(g) != 1 else ''}{note} | "
               f"{red} at {CALL_LIST_RED_FROM}+ days (red)")
        if len(chunks) > 1:
            cap += f" | part {i} of {len(chunks)}"
        pages.append((render_call_list(ch), cap))
    return pages


def franchise_call_lists(source, executive):
    """Kept for compatibility: ALL franchises at once (slow). Prefer call_list_franchises + franchise_call_list."""
    pages = []
    names = list(call_list_franchises(source, executive).index)
    for n in names:
        pages += franchise_call_list(source, executive, n)
    return pages, len(names)


# ================================================================ Mandatory appointments
MAND_STATUSES = ["Future Appt", "Missed Appt", "Today Appt", "Visit cancelled", "Visit Done"]


def load_mandatory(source) -> pd.DataFrame:
    """Mandatory Appointment export: 2 title rows, header on row 3."""
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    df = pd.read_excel(source, header=2)
    df.columns = [str(c).strip() for c in df.columns]
    for need in ("BSE Name", "Visit Status", "Ticket"):
        if need not in df.columns:
            raise ValueError(f"column '{need}' not found - is this the Mandatory Appointment report?")
    df = df[df["Ticket"].notna()].copy()                       # drops blank / total rows
    df["BSE Name"] = df["BSE Name"].fillna("(no executive)").astype(str).str.strip()
    df["Visit Status"] = df["Visit Status"].astype(str).str.strip()
    return eng.drop_excluded_executives(df, "BSE Name")


def _mand_table(df, by, label, who=""):
    """Visit-status table grouped by `by` (ASM or franchise), MC % = Visit Done / Grand Total, best first."""
    g = pd.crosstab(df[by], df["Visit Status"])
    for c in MAND_STATUSES:
        if c not in g:
            g[c] = 0
    g = g[MAND_STATUSES + [c for c in g.columns if c not in MAND_STATUSES]]
    g["Total"] = g.sum(axis=1)
    g["MC"] = g["Visit Done"] / g["Total"]
    g = g.sort_values(["MC", "Total"], ascending=[False, False])
    tot = g[MAND_STATUSES + ["Total"]].sum()
    tot_mc = tot["Visit Done"] / tot["Total"]

    headers = [label] + MAND_STATUSES[:3] + ["Visit cancelled", "Visit Done", "Grand Total", "MC %"]
    name_w = max(3.6, 0.13 * max(len(n) for n in g.index) + 0.6)
    widths = [name_w, 1.5, 1.6, 1.6, 1.7, 1.4, 1.6, 1.0]
    blank = lambda v: "" if v == 0 else int(v)
    rows = [[n] + [blank(r[c]) for c in MAND_STATUSES] + [int(r["Total"]), f"{r['MC']*100:.0f}%"]
            for n, r in g.iterrows()]
    rows.append(["Grand Total"] + [int(tot[c]) for c in MAND_STATUSES] + [int(tot["Total"]), f"{tot_mc*100:.0f}%"])
    colours = {7: eng._excel_3_colour_scale(g["MC"].values, reverse=True)}
    png = _table_png(headers, rows, widths, "", colour_cols=colours, total_row=True)
    best, worst = g.index[0], g.index[-1]
    summary = (f"{who}Mandatory appointments: {int(tot['Total'])} calls, {int(tot['Visit Done'])} visits done "
               f"(MC {tot_mc*100:.0f}%). Best: {best.title()} {g.loc[best, 'MC']*100:.0f}%. "
               f"Lowest: {worst.title()} {g.loc[worst, 'MC']*100:.0f}%.\n"
               f"MC % = Visit Done / Grand Total.")
    return png, summary


def mandatory_appointments(source):
    """ASM x visit status. source = bytes / path / parsed DataFrame. Returns (png, summary)."""
    df = source if isinstance(source, pd.DataFrame) else load_mandatory(source)
    if df.empty:
        return None, "No mandatory appointments left after removing the excluded executives."
    return _mand_table(df, "BSE Name", "ASM Name")


def mandatory_executives(df):
    """ASMs in the file with their call counts, most calls first."""
    return df["BSE Name"].value_counts()


def mandatory_franchises(df, executive):
    """One ASM's franchises x visit status. Returns (png, summary)."""
    d = df[df["BSE Name"].str.lower() == executive.strip().lower()].copy()
    if d.empty:
        return None, f"No mandatory appointments found for {executive.title()}."
    d["_fr"] = d["Franchise Name"].fillna("(no franchise)").astype(str).str.strip()
    png, summary = _mand_table(d, "_fr", "Franchise")
    return png, summary.replace("Mandatory appointments:", f"{executive.title()} - mandatory appointments:", 1)
