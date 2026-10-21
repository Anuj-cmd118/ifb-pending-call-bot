"""
report_engine.py
Turns the "Pending call" Excel from the IFB portal into the colour-coded
executive x days-old pivot image.  No WhatsApp code in here, so it can be
tested on its own:

    python report_engine.py "Pending_call.xlsx" --min-days 3 --exclude-courtesy
"""
import argparse
import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# Column names exactly as they appear in the portal export
COL_EXEC = "Service Executive Name"
COL_DAYS = "DAYS OLD"
COL_TYPE = "Call Type"
COL_STATUS = "Status"

COL_REASON = "Pending Reason"

# IFB standard filters: these are ALWAYS ignored in the daily report
STD_EXCLUDE_TYPES = ["COURTESY VISIT", "DEALER DEFECTIVE REQUEST"]
STD_EXCLUDE_REASONS = ["Request soft closure verification",
                       "Cancellation Request verification"]

HEADER_ROW = 2   # rows 0-1 are the title and date-range lines


# ----------------------------------------------------------------- loading
def load_report(source) -> pd.DataFrame:
    """source = file path or bytes. Finds the real header row automatically."""
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    raw = pd.read_excel(source, header=None)
    # locate the header row by looking for 'DAYS OLD' (more robust than a fixed row)
    hdr = next((i for i in range(min(10, len(raw)))
                if COL_DAYS in raw.iloc[i].astype(str).values), HEADER_ROW)
    df = raw.iloc[hdr + 1:].copy()
    df.columns = [str(c).strip() for c in raw.iloc[hdr]]
    df = df.dropna(how="all")
    missing = [c for c in (COL_EXEC, COL_DAYS) if c not in df.columns]
    if missing:
        raise ValueError(f"This doesn't look like the pending call report - "
                         f"missing column(s): {', '.join(missing)}")
    df[COL_DAYS] = pd.to_numeric(df[COL_DAYS], errors="coerce")
    df = df.dropna(subset=[COL_DAYS])
    df[COL_DAYS] = df[COL_DAYS].astype(int)
    df[COL_EXEC] = df[COL_EXEC].fillna("UNASSIGNED").astype(str).str.strip()
    return df


def date_range_text(source) -> str:
    """Pulls 'Date Range : ...' line from the top of the sheet, if present."""
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    top = pd.read_excel(source, header=None, nrows=3)
    for v in top.iloc[:, 0].astype(str):
        if v.lower().startswith("date range"):
            return v
    return ""


def call_types(df) -> list:
    return sorted(df[COL_TYPE].dropna().unique()) if COL_TYPE in df else []


# ------------------------------------------------------------------- logic
def build_pivot(df, min_days=3, call_type_filter=None, status_filter=None,
                max_days=None, cap_days=30, exclude_types=None, exclude_reasons=None):
    """
    min_days: keep calls with DAYS OLD  >  min_days   ("more than 3 days")
    max_days: optional upper limit (inclusive)
    Returns pivot: rows = executive, columns = day values, plus 'Grand Total',
    sorted by Grand Total descending, and a 'Grand Total' row at the bottom.
    """
    d = df[df[COL_DAYS] > min_days]
    if max_days is not None:
        d = d[d[COL_DAYS] <= max_days]
    if call_type_filter and COL_TYPE in d:
        d = d[d[COL_TYPE].isin(call_type_filter)]
    if status_filter and COL_STATUS in d:
        d = d[d[COL_STATUS].isin(status_filter)]
    if exclude_types and COL_TYPE in d:
        d = d[~d[COL_TYPE].isin(exclude_types)]
    if exclude_reasons and COL_REASON in d:
        # blank pending reason is kept; only the listed reasons are dropped
        d = d[~d[COL_REASON].astype(str).str.strip().str.lower()
              .isin([r.lower() for r in exclude_reasons])]
    if d.empty:
        return None
    days = d[COL_DAYS].where(d[COL_DAYS] <= cap_days, cap_days + 1) if cap_days else d[COL_DAYS]
    pv = pd.crosstab(d[COL_EXEC], days)
    pv.columns.name = None
    pv.index.name = None
    if cap_days and (cap_days + 1) in pv.columns:      # tail bucket e.g. "30+"
        pv = pv.rename(columns={cap_days + 1: f"{cap_days}+"})
    pv["Grand Total"] = pv.sum(axis=1)
    pv = pv.sort_values("Grand Total", ascending=False)
    pv.loc["Grand Total"] = pv.sum()
    return pv.astype(int)


# --------------------------------------------------------------- rendering
def _excel_3_colour_scale(values):
    """Mimics Excel's default Green-Yellow-Red scale (min=green, 50th pct=yellow, max=red)."""
    green, yellow, red = (99, 190, 123), (255, 235, 132), (248, 105, 107)
    lo, mid, hi = np.min(values), np.percentile(values, 50), np.max(values)

    def mix(a, b, t):
        return tuple((a[i] + (b[i] - a[i]) * t) / 255 for i in range(3))

    out = []
    for v in values:
        if hi == lo:
            out.append(mix(green, green, 0))
        elif v <= mid:
            out.append(mix(green, yellow, 0 if mid == lo else (v - lo) / (mid - lo)))
        else:
            out.append(mix(yellow, red, (v - mid) / (hi - mid)))
    return out


def render_image(pv: pd.DataFrame, title: str = "") -> bytes:
    """Draws the pivot like the Excel screenshot and returns PNG bytes."""
    body = pv.iloc[:-1]
    cols = ["Row Labels"] + [str(c) for c in pv.columns[:-1]] + ["Grand Total"]
    n_rows, n_cols = len(pv) + 1, len(cols)

    name_w = max(3.6, 0.13 * max(len(s) for s in pv.index) + 0.6)
    fig_w = name_w + 0.55 * (n_cols - 2) + 1.3
    fig_h = 0.42 * n_rows + (0.55 if title else 0.15)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=200)
    ax.axis("off")

    widths = [name_w] + [0.55] * (n_cols - 2) + [1.3]
    total_w = sum(widths)
    x_edges = np.cumsum([0] + widths) / total_w
    row_h = 1.0 / (n_rows + (1.2 if title else 0))
    header_bg, total_bg, border = "#DCE6F1", "#DCE6F1", "#B8B8B8"

    def cell(r, c, text, bg="white", bold=False, ha="center"):
        y0 = 1 - (r + 1) * row_h - (1.2 * row_h if title else 0)
        ax.add_patch(plt.Rectangle((x_edges[c], y0), x_edges[c + 1] - x_edges[c], row_h,
                                   facecolor=bg, edgecolor=border, lw=0.6,
                                   transform=ax.transAxes))
        tx = x_edges[c] + (0.5 * (x_edges[c + 1] - x_edges[c]) if ha == "center" else 0.012)
        ax.text(tx, y0 + row_h / 2, text, ha=ha, va="center", fontsize=10,
                fontweight="bold" if bold else "normal", transform=ax.transAxes)

    if title:
        ax.text(0.0, 1 - 0.55 * row_h, title, fontsize=9, color="#444444",
                va="center", transform=ax.transAxes)

    for c, h in enumerate(cols):
        cell(0, c, h, header_bg, True)

    totals = body["Grand Total"].values
    colours = _excel_3_colour_scale(totals)
    for r, (name, row) in enumerate(body.iterrows(), start=1):
        cell(r, 0, name, ha="center")
        for c, v in enumerate(row.values[:-1], start=1):
            cell(r, c, "" if v == 0 else str(v))
        cell(r, n_cols - 1, str(row.values[-1]), colours[r - 1])

    last = n_rows - 1
    cell(last, 0, "Grand Total", total_bg, True)
    for c, v in enumerate(pv.loc["Grand Total"].values, start=1):
        cell(last, c, str(v), total_bg, True)

    ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.08, facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def make_report(source, min_days=3, standard_filters=True, call_type_filter=None,
                status_filter=None, max_days=None):
    """One-call helper used by the bot. Returns (png_bytes | None, summary_text).
    standard_filters=True applies the IFB exclusions (courtesy visit, dealer
    defective, soft-closure / cancellation verification)."""
    df = load_report(source)
    ex_t = STD_EXCLUDE_TYPES if standard_filters else None
    ex_r = STD_EXCLUDE_REASONS if standard_filters else None
    pv = build_pivot(df, min_days, call_type_filter, status_filter, max_days,
                     exclude_types=ex_t, exclude_reasons=ex_r)
    if pv is None:
        return None, f"No calls found that are more than {min_days} days old with those filters."
    age = f"more than {min_days} days old"
    title = f"Pending calls - {age}" + ("" if standard_filters else " (no filters)")
    png = render_image(pv, title)
    summary = (f"{int(pv.loc['Grand Total', 'Grand Total'])} pending calls "
               f"({age}) across {len(pv) - 1} executives.")
    return png, summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx")
    ap.add_argument("--min-days", type=int, default=3)
    ap.add_argument("--no-filters", action="store_true", help="skip the IFB standard exclusions")
    ap.add_argument("--out", default="report.png")
    a = ap.parse_args()
    png, msg = make_report(a.xlsx, a.min_days, standard_filters=not a.no_filters)
    print(msg)
    if png:
        open(a.out, "wb").write(png)
