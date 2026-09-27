"""Electricity usage: importing NB Power usage files, meter readings, summaries and reports.

Nothing here talks to the NB Power meter. Usage comes from a file the home owner
downloads from their NB Power account, or from readings they type in from the
meter's display. Reports are files the home owner chooses to download or email.
"""
from __future__ import annotations

import csv
import html
import io
import os
import smtplib
from datetime import date, datetime, timedelta
from email.message import EmailMessage

HDD_BASE = 18.0  # °C, standard base for heating degree-days in Canada

DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d-%b-%Y", "%b %d, %Y", "%d %b %Y")


def _parse_day(value: str):
    value = value.strip().strip('"')
    if not value:
        return None
    # Accept "2026-09-01T13:00", "2026-09-01 13:00:00", etc.
    head = value.replace("T", " ").split(" ")[0] if value[:4].isdigit() else value
    for fmt in DATE_FORMATS:
        for candidate in (head, value):
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                pass
    return None


def _parse_number(value: str):
    cleaned = value.strip().strip('"').replace(",", "").replace("kWh", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_usage_csv(text: str):
    """Read a usage export and total it per day.

    Looks for a header row with a date/time column and a kWh/usage/consumption column,
    so it copes with hourly or daily exports and with different column orders.
    Returns ({"YYYY-MM-DD": kWh}, rows_skipped).
    """
    rows = list(csv.reader(io.StringIO(text)))
    date_col = kwh_col = None
    start = 0
    for i, row in enumerate(rows[:30]):
        lowered = [c.strip().lower() for c in row]
        d = next((j for j, c in enumerate(lowered) if "date" in c or c in ("day", "time", "period")), None)
        k = next((j for j, c in enumerate(lowered)
                  if "kwh" in c or "usage" in c or "consumption" in c or "energy" in c), None)
        if d is not None and k is not None and d != k:
            date_col, kwh_col, start = d, k, i + 1
            break
    if date_col is None:
        # No header: assume "date, kWh".
        date_col, kwh_col, start = 0, 1, 0

    totals, skipped = {}, 0
    for row in rows[start:]:
        if len(row) <= max(date_col, kwh_col):
            if any(c.strip() for c in row):
                skipped += 1
            continue
        day = _parse_day(row[date_col])
        kwh = _parse_number(row[kwh_col])
        if day is None or kwh is None or kwh < 0:
            skipped += 1
            continue
        key = day.isoformat()
        totals[key] = round(totals.get(key, 0.0) + kwh, 3)
    return totals, skipped


def spread_reading(previous: dict, current_kwh: float, current_ts: float, tz):
    """Split the kWh between two meter readings evenly over the days they span."""
    used = current_kwh - previous["kwh"]
    if used < 0:
        raise ValueError("This reading is lower than the last one. Check the number on the meter.")
    start = datetime.fromtimestamp(previous["ts"], tz).date()
    end = datetime.fromtimestamp(current_ts, tz).date()
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    if len(days) > 1:
        days = days[1:]  # the first day was already counted by the previous reading
    share = used / len(days)
    return {d.isoformat(): round(share, 3) for d in days}


def month_bounds(month: str):
    """'2026-09' -> (date(2026,9,1), date(2026,9,30))."""
    first = datetime.strptime(month, "%Y-%m").date()
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, nxt - timedelta(days=1)


def summarize(usage_rows: list, daily_means: dict, rate_cents):
    days = []
    for row in usage_rows:
        mean = daily_means.get(row["day"])
        hdd = None if mean is None else round(max(0.0, HDD_BASE - mean), 1)
        days.append({"day": row["day"], "kwh": round(row["kwh"], 2), "mean_temp": mean,
                     "hdd": hdd, "source": row["source"]})
    total = round(sum(d["kwh"] for d in days), 1)
    hdd_days = [d for d in days if d["hdd"] is not None]
    hdd_total = round(sum(d["hdd"] for d in hdd_days), 1)
    kwh_on_hdd_days = sum(d["kwh"] for d in hdd_days)
    return {
        "days": days,
        "total_kwh": total,
        "avg_kwh_per_day": round(total / len(days), 1) if days else None,
        "hdd_total": hdd_total if hdd_days else None,
        "kwh_per_hdd": round(kwh_on_hdd_days / hdd_total, 2) if hdd_total else None,
        "rate_cents_per_kwh": rate_cents,
        "estimated_cost": round(total * rate_cents / 100, 2) if rate_cents else None,
    }


# ---- reports -------------------------------------------------------------------------

REPORT_PARTS = ("usage", "schedule", "weather")


def report_csv(summary: dict, period_label: str, include: set, schedule: list) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Smart Heating Controller — household energy report"])
    w.writerow(["Period", period_label])
    w.writerow(["Note", "Usage from the home owner's NB Power data. The meter was not modified."])
    w.writerow([])
    if "usage" in include:
        w.writerow(["Total kWh", summary["total_kwh"]])
        w.writerow(["Average kWh per day", summary["avg_kwh_per_day"]])
        if summary["estimated_cost"] is not None:
            w.writerow(["Estimated cost (CAD, energy charge only)", summary["estimated_cost"]])
        if "weather" in include and summary["kwh_per_hdd"] is not None:
            w.writerow(["Heating degree-days (base 18 °C)", summary["hdd_total"]])
            w.writerow(["kWh per degree-day", summary["kwh_per_hdd"]])
        w.writerow([])
        header = ["Date", "kWh"] + (["Mean outdoor °C", "Degree-days"] if "weather" in include else [])
        w.writerow(header)
        for d in summary["days"]:
            row = [d["day"], d["kwh"]]
            if "weather" in include:
                row += ["" if d["mean_temp"] is None else d["mean_temp"], "" if d["hdd"] is None else d["hdd"]]
            w.writerow(row)
        w.writerow([])
    if "schedule" in include:
        w.writerow(["Heating schedule"])
        w.writerow(["Period", "Starts", "Target °C", "Days"])
        for p in schedule:
            w.writerow([p["name"], p["start"], p["temp"], day_names(p["days"])])
    return out.getvalue()


def day_names(days: list) -> str:
    if days == [0, 1, 2, 3, 4, 5, 6]:
        return "Every day"
    if days == [0, 1, 2, 3, 4]:
        return "Weekdays"
    if days == [5, 6]:
        return "Weekends"
    return ", ".join("Mon Tue Wed Thu Fri Sat Sun".split()[d] for d in days)


def report_html(summary: dict, period_label: str, include: set, schedule: list, location: str,
                auto_print: bool = False) -> str:
    e = html.escape
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        f"<title>Energy report · {e(period_label)}</title>",
        "<style>body{font-family:system-ui,sans-serif;color:#16181B;max-width:760px;margin:32px auto;padding:0 16px}"
        "h1{font-size:24px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}"
        "table{border-collapse:collapse;width:100%;font-size:14px}"
        "th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #ddd}"
        ".muted{color:#555;font-size:13px}.kpis{display:flex;gap:28px;flex-wrap:wrap}"
        ".kpi b{display:block;font-size:22px}@media print{body{margin:0}}</style></head><body>",
        "<h1>Household heating &amp; energy report</h1>",
        f"<div class='muted'>{e(location)} · {e(period_label)} · generated {e(date.today().isoformat())}</div>",
        "<p class='muted'>Usage comes from the home owner's own NB Power usage data. The electricity "
        "meter was not modified. Figures marked “estimated” are calculations, not billing amounts.</p>",
    ]
    if "usage" in include:
        parts.append("<h2>Electricity use</h2><div class='kpis'>")
        parts.append(f"<div class='kpi'>Total<b>{summary['total_kwh']} kWh</b></div>")
        if summary["avg_kwh_per_day"] is not None:
            parts.append(f"<div class='kpi'>Per day<b>{summary['avg_kwh_per_day']} kWh</b></div>")
        if summary["estimated_cost"] is not None:
            parts.append(f"<div class='kpi'>Estimated energy cost<b>${summary['estimated_cost']:.2f}</b></div>")
        if "weather" in include and summary["kwh_per_hdd"] is not None:
            parts.append(f"<div class='kpi'>kWh per degree-day<b>{summary['kwh_per_hdd']}</b></div>")
        parts.append("</div><table><tr><th>Date</th><th>kWh</th>")
        if "weather" in include:
            parts.append("<th>Mean outdoor °C</th><th>Degree-days</th>")
        parts.append("</tr>")
        for d in summary["days"]:
            parts.append(f"<tr><td>{e(d['day'])}</td><td>{d['kwh']}</td>")
            if "weather" in include:
                mt = "—" if d["mean_temp"] is None else f"{d['mean_temp']:.1f}"
                hd = "—" if d["hdd"] is None else f"{d['hdd']}"
                parts.append(f"<td>{mt}</td><td>{hd}</td>")
            parts.append("</tr>")
        parts.append("</table>")
    if "schedule" in include:
        parts.append("<h2>Heating schedule</h2><table><tr><th>Period</th><th>Starts</th>"
                     "<th>Target</th><th>Days</th></tr>")
        for p in schedule:
            parts.append(f"<tr><td>{e(p['name'])}</td><td>{e(p['start'])}</td>"
                         f"<td>{p['temp']} °C</td><td>{e(day_names(p['days']))}</td></tr>")
        parts.append("</table>")
    if auto_print:
        parts.append("<script>window.addEventListener('load',function(){window.print()})</script>")
    parts.append("</body></html>")
    return "".join(parts)


def email_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM"))


def send_report_email(to: str, subject: str, body: str, attachments: list) -> None:
    """attachments: [(filename, mimetype, text)]"""
    msg = EmailMessage()
    msg["From"] = os.environ["SMTP_FROM"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    for filename, mimetype, text in attachments:
        maintype, subtype = mimetype.split("/")
        msg.add_attachment(text.encode("utf-8"), maintype=maintype, subtype=subtype, filename=filename)
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.starttls()
        if os.environ.get("SMTP_USER"):
            smtp.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        smtp.send_message(msg)
