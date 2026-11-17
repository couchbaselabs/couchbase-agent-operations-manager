"""
ROI metrics export (Settings -> Export ROI Metrics).

Turns what the two caches already record into one report an operator can
hand to whoever asks "what is this saving us?":

  * LLM Cache     - tokens and dollars not spent because an answer was served
                    from Couchbase instead of a provider, next to what was
                    spent on the misses. Dollars come from the per-model
                    prices in app/llm_cache.py (list-price estimates, or the
                    prices an admin entered for a custom provider) - the
                    same figures the LLM Cache dashboard shows.
  * Context Cache - source fetches avoided and the fetch time they would
                    have taken. A context fetch has no token price, so it is
                    only expressed in dollars when the operator supplies a
                    cost per avoided fetch (a warehouse query, a metered API
                    call); otherwise the dollar columns stay empty.

Everything is reduced from the same GROUP BY rows the dashboards use
(CouchbaseStore.llm_dashboard_aggregate_since /
context_dashboard_aggregate_since) plus the two all-time counter documents,
so an export can never disagree with the pages it summarizes. Nothing here
talks to Couchbase; app/main.py does the reads.
"""
import calendar
import csv
import io
from datetime import datetime

LLM_HIT_OUTCOMES = ("hit_exact", "hit_semantic")

CSV_COLUMNS = [
    "section", "scope", "period_start", "period_end",
    "provider", "model", "agent", "namespace",
    "requests", "hits", "misses", "hit_rate_pct",
    "tokens_saved", "tokens_spent",
    "cost_saved_usd", "cost_spent_usd", "savings_rate_pct",
    "latency_saved_ms", "latency_saved_hours",
    "platform_cost_usd", "net_savings_usd", "roi_pct", "projected_annual_savings_usd",
    # Twelve-month estimate rows only.
    "llm_cost_saved_usd", "context_cost_saved_usd", "cumulative_savings_usd", "cumulative_net_savings_usd",
]


def _pct(part: float, whole: float) -> float:
    return round((part / whole) * 100, 1) if whole else 0.0


def _hours(ms: int) -> float:
    return round(ms / 3_600_000, 2)


def _money(value: float) -> float:
    return round(float(value or 0.0), 4)


def _llm_bucket() -> dict:
    return {
        "requests": 0, "hits": 0, "exact_hits": 0, "semantic_hits": 0, "misses": 0,
        "bypasses": 0, "errors": 0, "tokens_saved": 0, "tokens_spent": 0,
        "cost_saved_usd": 0.0, "cost_spent_usd": 0.0, "bypass_cost_usd": 0.0, "latency_saved_ms": 0,
    }


def _llm_add(bucket: dict, row: dict):
    outcome = row.get("outcome")
    n = int(row.get("n") or 0)
    bucket["requests"] += n
    if outcome in LLM_HIT_OUTCOMES:
        bucket["hits"] += n
        bucket["exact_hits" if outcome == "hit_exact" else "semantic_hits"] += n
        bucket["tokens_saved"] += int(row.get("tokens_saved") or 0)
        bucket["cost_saved_usd"] += float(row.get("cost_saved_usd") or 0.0)
        bucket["latency_saved_ms"] += max(0, int(row.get("latency_saved_ms") or 0))
    elif outcome == "miss":
        bucket["misses"] += n
        bucket["tokens_spent"] += int(row.get("total_tokens") or 0)
        bucket["cost_spent_usd"] += float(row.get("cost_usd") or 0.0)
    elif outcome == "bypass":
        # A bypass called the provider without the cache being allowed to
        # help. Reported on its own so cost_spent_usd stays what the LLM
        # Cache dashboard calls it: spend on cacheable misses.
        bucket["bypasses"] += n
        bucket["bypass_cost_usd"] += float(row.get("cost_usd") or 0.0)
    elif outcome == "error":
        bucket["errors"] += n


def _llm_finish(bucket: dict) -> dict:
    out = dict(bucket)
    out["hit_rate_pct"] = _pct(out["hits"], out["hits"] + out["misses"])
    out["cost_saved_usd"] = _money(out["cost_saved_usd"])
    out["cost_spent_usd"] = _money(out["cost_spent_usd"])
    out["bypass_cost_usd"] = _money(out["bypass_cost_usd"])
    # Share of what the same traffic would have cost with no cache.
    out["savings_rate_pct"] = _pct(out["cost_saved_usd"], out["cost_saved_usd"] + out["cost_spent_usd"])
    out["latency_saved_hours"] = _hours(out["latency_saved_ms"])
    return out


def _ctx_bucket() -> dict:
    return {"lookups": 0, "hits": 0, "misses": 0, "writes": 0, "latency_saved_ms": 0}


def _ctx_add(bucket: dict, row: dict):
    outcome = row.get("outcome")
    n = int(row.get("n") or 0)
    if outcome == "hit":
        bucket["lookups"] += n
        bucket["hits"] += n
        bucket["latency_saved_ms"] += max(0, int(row.get("latency_saved_ms") or 0))
    elif outcome == "miss":
        bucket["lookups"] += n
        bucket["misses"] += n
    elif outcome == "write":
        bucket["writes"] += n


def _ctx_finish(bucket: dict, cost_per_fetch: float | None) -> dict:
    out = dict(bucket)
    out["hit_rate_pct"] = _pct(out["hits"], out["lookups"])
    out["latency_saved_hours"] = _hours(out["latency_saved_ms"])
    out["source_fetches_avoided"] = out["hits"]
    out["cost_saved_usd"] = _money(out["hits"] * cost_per_fetch) if cost_per_fetch else None
    return out


def _add_months(start: datetime, months: int) -> datetime:
    """Same day-of-month `months` later, clamped to the month's last day."""
    index = start.month - 1 + months
    year, month = start.year + index // 12, index % 12 + 1
    return start.replace(year=year, month=month, day=min(start.day, calendar.monthrange(year, month)[1]))


def twelve_month_estimate(
    *, until: str, window_days: int, days_with_activity: int,
    llm_window: dict, context_window: dict, platform_cost: float | None,
) -> dict:
    """What the next twelve months look like if usage stays where it is.

    The window's totals are turned into a per-day run rate and applied to
    each of the twelve months that start at the report time (so a
    31-day month earns more than a 28-day one, and the year is 365 or 366
    days as the calendar has it). Platform cost is extended the same way.
    It is a straight-line estimate: no growth, no seasonality, and a hit
    rate that stays where the window left it."""
    try:
        start = datetime.strptime(until, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        start = datetime.utcnow()
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    days = max(1, int(window_days))

    daily = {
        "llm_cost_saved_usd": llm_window["cost_saved_usd"] / days,
        "context_cost_saved_usd": (context_window["cost_saved_usd"] or 0.0) / days,
        "llm_cost_spent_usd": llm_window["cost_spent_usd"] / days,
        "platform_cost_usd": (platform_cost or 0.0) / days,
        "tokens_saved": llm_window["tokens_saved"] / days,
        "llm_requests": llm_window["requests"] / days,
        "context_fetches_avoided": context_window["hits"] / days,
        "latency_saved_ms": (llm_window["latency_saved_ms"] + context_window["latency_saved_ms"]) / days,
    }
    daily["cost_saved_usd"] = daily["llm_cost_saved_usd"] + daily["context_cost_saved_usd"]
    has_context_cost = context_window["cost_saved_usd"] is not None

    def period(n_days: int) -> dict:
        saved = daily["cost_saved_usd"] * n_days
        cost = daily["platform_cost_usd"] * n_days
        return {
            "days": n_days,
            "llm_cost_saved_usd": round(daily["llm_cost_saved_usd"] * n_days, 2),
            "context_cost_saved_usd": round(daily["context_cost_saved_usd"] * n_days, 2) if has_context_cost else None,
            "cost_saved_usd": round(saved, 2),
            "llm_cost_spent_usd": round(daily["llm_cost_spent_usd"] * n_days, 2),
            "tokens_saved": round(daily["tokens_saved"] * n_days),
            "context_fetches_avoided": round(daily["context_fetches_avoided"] * n_days),
            "latency_saved_hours": _hours(round(daily["latency_saved_ms"] * n_days)),
            "platform_cost_usd": round(cost, 2) if platform_cost else None,
            "net_savings_usd": round(saved - cost, 2) if platform_cost else None,
            "roi_pct": round((saved - cost) / cost * 100, 1) if platform_cost and cost else None,
        }

    months, cumulative_days = [], 0
    for i in range(12):
        m_start, m_end = _add_months(start, i), _add_months(start, i + 1)
        cumulative_days += (m_end - m_start).days
        to_date = period(cumulative_days)
        months.append(dict(
            period((m_end - m_start).days),
            month=i + 1,
            period_start=m_start.strftime("%Y-%m-%dT00:00:00Z"),
            period_end=m_end.strftime("%Y-%m-%dT00:00:00Z"),
            cumulative_savings_usd=to_date["cost_saved_usd"],
            cumulative_net_savings_usd=to_date["net_savings_usd"],
        ))

    return {
        "basis": {
            "method": "Run rate: each figure in the reporting window divided by the window's days, applied "
                      "to the calendar days of the next twelve months. Assumes usage, hit rate, model "
                      "prices and platform cost stay as they were in the window - no growth or seasonality.",
            "window_days": days,
            # Fewer active days than the window (a new deployment, a quiet
            # weekend) means the run rate understates a normal day.
            "days_with_activity": days_with_activity,
            "daily_cost_saved_usd": round(daily["cost_saved_usd"], 4),
            "daily_llm_cost_saved_usd": round(daily["llm_cost_saved_usd"], 4),
            "daily_context_cost_saved_usd": round(daily["context_cost_saved_usd"], 4) if has_context_cost else None,
            "daily_platform_cost_usd": round(daily["platform_cost_usd"], 4) if platform_cost else None,
            "daily_tokens_saved": round(daily["tokens_saved"]),
            "daily_llm_requests": round(daily["llm_requests"]),
            "daily_context_fetches_avoided": round(daily["context_fetches_avoided"]),
        },
        "period_start": months[0]["period_start"],
        "period_end": months[-1]["period_end"],
        "total": period(cumulative_days),
        "months": months,
    }


def build_report(
    *,
    llm_rows: list[dict],
    context_rows: list[dict],
    llm_lifetime: dict | None,
    context_lifetime: dict | None,
    window_days: int,
    since: str,
    until: str,
    retention_days: int,
    provider_labels: dict[str, str] | None = None,
    context_cost_per_fetch_usd: float | None = None,
    platform_cost_usd: float | None = None,
) -> dict:
    """The whole report as one JSON-ready dict. `llm_rows` / `context_rows`
    are the dashboards' hourly GROUP BY rows for the window; the lifetime
    dicts are the all-time counter documents."""
    provider_labels = provider_labels or {}
    cost_per_fetch = float(context_cost_per_fetch_usd) if context_cost_per_fetch_usd else None
    platform_cost = float(platform_cost_usd) if platform_cost_usd else None

    # ---- LLM cache ---------------------------------------------------------
    llm_total = _llm_bucket()
    llm_by_model: dict[tuple, dict] = {}
    llm_by_day: dict[str, dict] = {}
    for r in llm_rows or []:
        _llm_add(llm_total, r)
        key = (r.get("provider") or "unknown", r.get("model") or "unknown")
        _llm_add(llm_by_model.setdefault(key, _llm_bucket()), r)
        day = (r.get("hour_key") or "")[:10]
        if day:
            _llm_add(llm_by_day.setdefault(day, _llm_bucket()), r)

    lifetime_by_model = (llm_lifetime or {}).get("by_model") or {}
    by_model = []
    for (provider, model), bucket in llm_by_model.items():
        row = _llm_finish(bucket)
        life = lifetime_by_model.get(f"{provider}::{model}") or {}
        row.update(
            provider=provider, provider_label=provider_labels.get(provider, provider), model=model,
            lifetime_cost_saved_usd=_money(life.get("cost_saved_usd_total")),
            lifetime_cost_spent_usd=_money(life.get("cost_spent_usd_total")),
        )
        by_model.append(row)
    by_model.sort(key=lambda r: r["cost_saved_usd"], reverse=True)

    life_saved = _money((llm_lifetime or {}).get("cost_saved_usd_total"))
    life_spent = _money(sum(float(m.get("cost_spent_usd_total") or 0.0) for m in lifetime_by_model.values()))
    llm = {
        "window": _llm_finish(llm_total),
        "lifetime": {
            "tokens_saved": int((llm_lifetime or {}).get("tokens_saved_total") or 0),
            "cost_saved_usd": life_saved,
            "cost_spent_usd": life_spent,
            "savings_rate_pct": _pct(life_saved, life_saved + life_spent),
        },
        "by_model": by_model,
        "daily": [dict(_llm_finish(b), date=d) for d, b in sorted(llm_by_day.items())],
    }

    # ---- Context cache -----------------------------------------------------
    ctx_total = _ctx_bucket()
    ctx_by_agent: dict[tuple, dict] = {}
    ctx_by_day: dict[str, dict] = {}
    for r in context_rows or []:
        _ctx_add(ctx_total, r)
        key = (r.get("subject") or "unknown", r.get("namespace") or "default")
        _ctx_add(ctx_by_agent.setdefault(key, _ctx_bucket()), r)
        day = (r.get("hour_key") or "")[:10]
        if day:
            _ctx_add(ctx_by_day.setdefault(day, _ctx_bucket()), r)

    by_agent = [
        dict(_ctx_finish(b, cost_per_fetch), agent=agent, namespace=namespace)
        for (agent, namespace), b in ctx_by_agent.items()
    ]
    by_agent.sort(key=lambda r: r["latency_saved_ms"], reverse=True)

    life_hits = int((context_lifetime or {}).get("hits_total") or 0)
    life_ms = int((context_lifetime or {}).get("latency_saved_ms_total") or 0)
    life_lookups = int((context_lifetime or {}).get("lookups_total") or 0)
    context = {
        "window": _ctx_finish(ctx_total, cost_per_fetch),
        "lifetime": {
            "lookups": life_lookups,
            "hits": life_hits,
            "hit_rate_pct": _pct(life_hits, life_lookups),
            "source_fetches_avoided": life_hits,
            "latency_saved_ms": life_ms,
            "latency_saved_hours": _hours(life_ms),
            "cost_saved_usd": _money(life_hits * cost_per_fetch) if cost_per_fetch else None,
        },
        "by_agent": by_agent,
        "daily": [dict(_ctx_finish(b, cost_per_fetch), date=d) for d, b in sorted(ctx_by_day.items())],
    }

    # ---- Combined ----------------------------------------------------------
    estimate = twelve_month_estimate(
        until=until, window_days=window_days,
        days_with_activity=len(set(llm_by_day) | set(ctx_by_day)),
        llm_window=llm["window"], context_window=context["window"], platform_cost=platform_cost,
    )
    window_saved = llm["window"]["cost_saved_usd"] + (context["window"]["cost_saved_usd"] or 0.0)
    lifetime_saved = llm["lifetime"]["cost_saved_usd"] + (context["lifetime"]["cost_saved_usd"] or 0.0)
    combined = {
        "cost_saved_usd": _money(window_saved),
        "lifetime_cost_saved_usd": _money(lifetime_saved),
        "latency_saved_ms": llm["window"]["latency_saved_ms"] + context["window"]["latency_saved_ms"],
        "latency_saved_hours": _hours(llm["window"]["latency_saved_ms"] + context["window"]["latency_saved_ms"]),
        # The twelve-month estimate's total - see twelve_month_estimate.
        "projected_annual_savings_usd": estimate["total"]["cost_saved_usd"],
        "platform_cost_usd": platform_cost,
        "net_savings_usd": _money(window_saved - platform_cost) if platform_cost else None,
        "roi_pct": round((window_saved - platform_cost) / platform_cost * 100, 1) if platform_cost else None,
    }

    return {
        "report": "aom_roi_metrics",
        "generated_at": until,
        "window": {"days": window_days, "since": since, "until": until, "retention_days": retention_days},
        "assumptions": {
            "llm_pricing": "Per-model list-price estimates (or the prices entered for a custom provider) "
                           "in USD per 1M tokens, applied when each request was recorded. Not billing data.",
            "context_cost_per_fetch_usd": cost_per_fetch,
            "platform_cost_usd": platform_cost,
            "roi_formula": "(savings in the window - platform cost for the window) / platform cost",
        },
        "combined": combined,
        "twelve_month_estimate": estimate,
        "llm": llm,
        "context": context,
    }


def to_csv(report: dict) -> str:
    """The report as one flat table: one row per total / model / agent /
    day / projected month, with a `section` and `scope` column to filter or pivot on. Columns
    that do not apply to a row are left empty rather than zero."""
    since, until = report["window"]["since"], report["window"]["until"]
    rows: list[dict] = []

    def add(section, scope, data, **extra):
        row = {"section": section, "scope": scope, "period_start": since, "period_end": until}
        row.update({k: v for k, v in data.items() if k in CSV_COLUMNS and v is not None})
        row.update(extra)  # a row's own period (a day, a projected month) wins over the window's
        rows.append(row)

    def ctx(data):
        # Context rows count lookups, not requests.
        return dict(data, requests=data.get("lookups"))

    combined = report["combined"]
    add("combined", "window_total", combined)
    add("combined", "lifetime_total", {"cost_saved_usd": combined["lifetime_cost_saved_usd"]},
        period_start="", period_end=until)

    estimate = report["twelve_month_estimate"]
    add("twelve_month_estimate", "total", estimate["total"],
        period_start=estimate["period_start"], period_end=estimate["period_end"])
    for m in estimate["months"]:
        add("twelve_month_estimate", "monthly", m)

    llm = report["llm"]
    add("llm_cache", "window_total", llm["window"])
    add("llm_cache", "lifetime_total", llm["lifetime"], period_start="", period_end=until)
    for m in llm["by_model"]:
        add("llm_cache", "by_model", m)
    for d in llm["daily"]:
        add("llm_cache", "daily", d, period_start=f"{d['date']}T00:00:00Z", period_end=f"{d['date']}T23:59:59Z")

    context = report["context"]
    add("context_cache", "window_total", ctx(context["window"]))
    add("context_cache", "lifetime_total", ctx(context["lifetime"]), period_start="", period_end=until)
    for a in context["by_agent"]:
        add("context_cache", "by_agent", ctx(a))
    for d in context["daily"]:
        add("context_cache", "daily", ctx(d),
            period_start=f"{d['date']}T00:00:00Z", period_end=f"{d['date']}T23:59:59Z")

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _csv_safe(row.get(k, "")) for k in CSV_COLUMNS})
    return buf.getvalue()


def _csv_safe(value):
    """Agent and namespace names are caller-supplied; keep a spreadsheet
    from evaluating one that starts like a formula."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


# ---------------------------------------------------------------------------
# Branded Excel workbook
# ---------------------------------------------------------------------------
# The CSV above is the machine-friendly export; this is the one that gets
# forwarded. Same report, laid out as a Couchbase-branded workbook: a red
# title band on every sheet, dark table headers, banded rows and real number
# formats. The palette is the dashboard's own (ui/src/theme/theme.css), which
# follows the Couchbase mark - change it here and every sheet follows.
BRAND = {
    "name": "Couchbase Labs Agent Operations Manager",
    "red": "EA2328",        # Couchbase red - title band, tab colour, total rules
    "red_dark": "C81E22",
    "ink": "1B1E26",        # table headers, body text
    "muted": "5B6272",
    "band": "F7F8FA",       # alternate rows
    "tint": "FDECEC",       # headline figures
    "rule": "E2E5EB",
    "white": "FFFFFF",
    "font": "Arial",
}
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_USD = '"$"#,##0.00'
_INT = "#,##0"
_PCT = '0.0"%"'
_HRS = '#,##0.00" h"'


def to_xlsx(report: dict) -> bytes:
    """The report as a Couchbase-branded .xlsx: Summary, 12-Month Estimate,
    LLM Cache, Context Cache and Assumptions sheets."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    def fill(color):
        return PatternFill("solid", start_color=color, end_color=color)

    def font(color=BRAND["ink"], size=10, bold=False, italic=False):
        return Font(name=BRAND["font"], size=size, bold=bold, italic=italic, color=color)

    rule = Side(style="thin", color=BRAND["rule"])
    total_rule = Side(style="medium", color=BRAND["red"])
    window = report["window"]
    subtitle = (
        f"ROI Metrics Report   |   {window['since'][:10]} to {window['until'][:10]} "
        f"({window['days']}-day window)   |   generated {report['generated_at'].replace('T', ' ').replace('Z', ' UTC')}"
    )

    wb = Workbook()
    wb.remove(wb.active)

    def sheet(title: str, widths: list[int]):
        ws = wb.create_sheet(title)
        ws.sheet_view.showGridLines = False
        ws.sheet_properties.tabColor = BRAND["red"]
        for i, width in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = width
        last = len(widths)
        for row, height, color in ((1, 34, BRAND["red"]), (2, 20, BRAND["ink"])):
            ws.row_dimensions[row].height = height
            for col in range(1, last + 1):
                ws.cell(row=row, column=col).fill = fill(color)
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last)
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last)
        head = ws.cell(row=1, column=1, value=f"{BRAND['name']}  -  {title}")
        head.font = font(BRAND["white"], 16, bold=True)
        head.alignment = Alignment(vertical="center", indent=1)
        sub = ws.cell(row=2, column=1, value=subtitle)
        sub.font = font(BRAND["white"], 9)
        sub.alignment = Alignment(vertical="center", indent=1)
        ws.page_setup.orientation = "landscape"
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.print_title_rows = "1:2"
        ws.oddFooter.left.text = BRAND["name"]
        ws.oddFooter.right.text = "Page &P of &N"
        return ws

    def put(ws, row, col, value, fmt=None, **style):
        cell = ws.cell(row=row, column=col, value=value)
        if isinstance(value, str):
            # Agent, namespace and model names are caller-supplied: keep
            # one that starts with "=" a string, never a formula.
            cell.data_type = "s"
        cell.font = style.get("font") or font()
        cell.alignment = Alignment(
            horizontal="right" if isinstance(value, (int, float)) else "left",
            vertical="center", wrap_text=style.get("wrap", False),
        )
        if fmt and isinstance(value, (int, float)):
            cell.number_format = fmt
        return cell

    def section(ws, row, title):
        cell = put(ws, row, 1, title, font=font(BRAND["red_dark"], 12, bold=True))
        ws.row_dimensions[row].height = 22
        return cell.row + 1

    def table(ws, row, columns, rows, total=None):
        """columns: [(header, key, number format)]. Returns the next free row."""
        for c, (header, _key, fmt) in enumerate(columns, start=1):
            cell = put(ws, row, c, header, font=font(BRAND["white"], 10, bold=True), wrap=True)
            cell.fill = fill(BRAND["ink"])
            # Headers sit over their column's content: text left, numbers right.
            cell.alignment = Alignment(horizontal="right" if fmt else "left", vertical="center", wrap_text=True)
        ws.row_dimensions[row].height = 30
        first = row + 1
        for r, data in enumerate(rows):
            for c, (_header, key, fmt) in enumerate(columns, start=1):
                cell = put(ws, first + r, c, data.get(key), fmt)
                cell.border = Border(bottom=rule)
                if r % 2:
                    cell.fill = fill(BRAND["band"])
        row = first + len(rows)
        if not rows:
            put(ws, row, 1, "No activity in this window.", font=font(BRAND["muted"], italic=True))
            row += 1
        if total is not None:
            for c, (_header, key, fmt) in enumerate(columns, start=1):
                cell = put(ws, row, c, total.get(key), fmt, font=font(bold=True))
                cell.border = Border(top=total_rule)
            row += 1
        return row + 1

    def facts(ws, row, pairs):
        """Label / value pairs down columns A:B."""
        for label, value, fmt in pairs:
            put(ws, row, 1, label, font=font(BRAND["muted"])).border = Border(bottom=rule)
            cell = put(ws, row, 2, "-" if value is None else value, fmt, font=font(bold=True))
            cell.border = Border(bottom=rule)
            cell.alignment = Alignment(horizontal="right", vertical="center")
            row += 1
        return row + 1

    combined, estimate = report["combined"], report["twelve_month_estimate"]
    llm, context = report["llm"], report["context"]
    has_cost = estimate["total"]["platform_cost_usd"] is not None

    # ---- Summary -----------------------------------------------------------
    ws = sheet("Summary", [44, 22, 4, 44, 22])
    headline = [
        ("Savings in the window", combined["cost_saved_usd"], _USD),
        ("All-time savings", combined["lifetime_cost_saved_usd"], _USD),
        ("Twelve-month savings estimate", estimate["total"]["cost_saved_usd"], _USD),
        ("Twelve-month ROI estimate" if has_cost else "Time saved in the window",
         estimate["total"]["roi_pct"] if has_cost else combined["latency_saved_hours"],
         _PCT if has_cost else _HRS),
    ]
    for i, (label, value, fmt) in enumerate(headline):
        col = 1 if i % 2 == 0 else 4
        r = 4 + (i // 2) * 3
        for rr in (r, r + 1):
            for cc in (col, col + 1):
                ws.cell(row=rr, column=cc).fill = fill(BRAND["tint"])
        ws.merge_cells(start_row=r, start_column=col, end_row=r, end_column=col + 1)
        ws.merge_cells(start_row=r + 1, start_column=col, end_row=r + 1, end_column=col + 1)
        put(ws, r, col, label, font=font(BRAND["muted"], 9, bold=True)).alignment = Alignment(indent=1, vertical="center")
        big = put(ws, r + 1, col, value, fmt, font=font(BRAND["red_dark"], 20, bold=True))
        big.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws.row_dimensions[r + 1].height = 34

    row = section(ws, 11, "LLM Cache - reporting window")
    w = llm["window"]
    row_ctx = row  # the Context Cache block sits beside this one
    left_end = facts(ws, row, [
        ("Requests", w["requests"], _INT),
        ("Cache hits (exact + semantic)", w["hits"], _INT),
        ("Hit rate", w["hit_rate_pct"], _PCT),
        ("Tokens saved", w["tokens_saved"], _INT),
        ("Cost saved", w["cost_saved_usd"], _USD),
        ("Cost spent on misses", w["cost_spent_usd"], _USD),
        ("Share of spend avoided", w["savings_rate_pct"], _PCT),
        ("Model latency saved", w["latency_saved_hours"], _HRS),
        ("All-time cost saved", llm["lifetime"]["cost_saved_usd"], _USD),
        ("All-time tokens saved", llm["lifetime"]["tokens_saved"], _INT),
    ])
    c = context["window"]
    put(ws, 11, 4, "Context Cache - reporting window", font=font(BRAND["red_dark"], 12, bold=True))
    r = row_ctx
    for label, value, fmt in [
        ("Lookups", c["lookups"], _INT),
        ("Cache hits", c["hits"], _INT),
        ("Hit rate", c["hit_rate_pct"], _PCT),
        ("Source fetches avoided", c["source_fetches_avoided"], _INT),
        ("Fetch time saved", c["latency_saved_hours"], _HRS),
        ("Estimated cost saved", c["cost_saved_usd"], _USD),
        ("All-time fetches avoided", context["lifetime"]["source_fetches_avoided"], _INT),
        ("All-time fetch time saved", context["lifetime"]["latency_saved_hours"], _HRS),
        ("All-time estimated cost saved", context["lifetime"]["cost_saved_usd"], _USD),
    ]:
        put(ws, r, 4, label, font=font(BRAND["muted"])).border = Border(bottom=rule)
        cell = put(ws, r, 5, "-" if value is None else value, fmt, font=font(bold=True))
        cell.border = Border(bottom=rule)
        cell.alignment = Alignment(horizontal="right", vertical="center")
        r += 1
    row = max(left_end, r + 1)
    if has_cost:
        row = section(ws, row, "Return on investment")
        facts(ws, row, [
            ("Platform cost for the window", combined["platform_cost_usd"], _USD),
            ("Net savings for the window", combined["net_savings_usd"], _USD),
            ("ROI for the window", combined["roi_pct"], _PCT),
            ("Twelve-month platform cost", estimate["total"]["platform_cost_usd"], _USD),
            ("Twelve-month net savings", estimate["total"]["net_savings_usd"], _USD),
            ("Twelve-month ROI", estimate["total"]["roi_pct"], _PCT),
        ])
    ws.freeze_panes = "A3"

    # ---- 12-Month Estimate -------------------------------------------------
    columns = [
        ("Month", "label", None), ("Days", "days", _INT),
        ("LLM savings", "llm_cost_saved_usd", _USD), ("Context savings", "context_cost_saved_usd", _USD),
        ("Total savings", "cost_saved_usd", _USD), ("Cumulative savings", "cumulative_savings_usd", _USD),
    ]
    if has_cost:
        columns += [
            ("Platform cost", "platform_cost_usd", _USD), ("Net savings", "net_savings_usd", _USD),
            ("Cumulative net", "cumulative_net_savings_usd", _USD), ("ROI", "roi_pct", _PCT),
        ]
    columns += [("Tokens saved", "tokens_saved", _INT), ("Context fetches avoided", "context_fetches_avoided", _INT),
                ("Hours saved", "latency_saved_hours", _HRS)]
    ws = sheet("12-Month Estimate", [30] + [17] * (len(columns) - 1))
    row = section(ws, 4, "Twelve-month estimate at current usage")
    months = [dict(m, label=f"Month {m['month']}  (from {m['period_start'][:10]})") for m in estimate["months"]]
    row = table(ws, row, columns, months, total=dict(estimate["total"], label="Twelve months"))
    note = put(ws, row, 1, estimate["basis"]["method"], font=font(BRAND["muted"], 9, italic=True), wrap=True)
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=len(columns))
    ws.row_dimensions[row].height = 30
    note.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "B6"

    # ---- LLM Cache ---------------------------------------------------------
    llm_cols = [
        ("Requests", "requests", _INT), ("Hits", "hits", _INT), ("Misses", "misses", _INT),
        ("Hit rate", "hit_rate_pct", _PCT), ("Tokens saved", "tokens_saved", _INT),
        ("Tokens spent", "tokens_spent", _INT), ("Cost saved", "cost_saved_usd", _USD),
        ("Cost spent", "cost_spent_usd", _USD), ("Spend avoided", "savings_rate_pct", _PCT),
        ("Hours saved", "latency_saved_hours", _HRS),
    ]
    ws = sheet("LLM Cache", [22, 40] + [15] * len(llm_cols))
    row = section(ws, 4, "Savings by provider and model")
    row = table(ws, row, [("Provider", "provider_label", None), ("Model", "model", None)] + llm_cols,
                llm["by_model"], total=dict(llm["window"], provider_label="Window total"))
    row = section(ws, row, "Day by day")
    table(ws, row, [("Date (UTC)", "date", None), ("", "_", None)] + llm_cols, llm["daily"])
    ws.freeze_panes = "A3"

    # ---- Context Cache -----------------------------------------------------
    ctx_cols = [
        ("Lookups", "lookups", _INT), ("Hits", "hits", _INT), ("Misses", "misses", _INT),
        ("Writes", "writes", _INT), ("Hit rate", "hit_rate_pct", _PCT),
        ("Fetches avoided", "source_fetches_avoided", _INT), ("Hours saved", "latency_saved_hours", _HRS),
        ("Est. cost saved", "cost_saved_usd", _USD),
    ]
    ws = sheet("Context Cache", [40, 24] + [16] * len(ctx_cols))
    row = section(ws, 4, "Savings by agent and namespace")
    row = table(ws, row, [("Agent", "agent", None), ("Namespace", "namespace", None)] + ctx_cols,
                context["by_agent"], total=dict(context["window"], agent="Window total"))
    row = section(ws, row, "Day by day")
    table(ws, row, [("Date (UTC)", "date", None), ("", "_", None)] + ctx_cols, context["daily"])
    ws.freeze_panes = "A3"

    # ---- Assumptions -------------------------------------------------------
    ws = sheet("Assumptions", [38, 110])
    a, basis = report["assumptions"], estimate["basis"]
    row = section(ws, 4, "How these figures were produced")
    for label, value, fmt in [
        ("Reporting window", f"{window['since']} to {window['until']} ({window['days']} days)", None),
        ("Cache event retention", f"{window['retention_days']} days - the longest window a report can cover", None),
        ("LLM pricing", a["llm_pricing"], None),
        ("Cost of one context fetch (USD)", a["context_cost_per_fetch_usd"]
         if a["context_cost_per_fetch_usd"] is not None
         else "Not provided - Context Cache savings are reported as fetches and time only", _USD),
        ("Platform cost for the window (USD)", a["platform_cost_usd"]
         if a["platform_cost_usd"] is not None else "Not provided - net savings and ROI are not calculated", _USD),
        ("ROI formula", a["roi_formula"], None),
        ("Twelve-month estimate", basis["method"], None),
        ("Days with cache activity in the window", f"{basis['days_with_activity']} of {basis['window_days']}", None),
        ("Savings per day used for the estimate (USD)", basis["daily_cost_saved_usd"], _USD),
        ("Bypassed calls in the window (USD)", llm["window"]["bypass_cost_usd"], _USD),
    ]:
        put(ws, row, 1, label, font=font(BRAND["muted"], bold=True)).border = Border(bottom=rule)
        cell = put(ws, row, 2, value, fmt, wrap=True)
        cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
        cell.border = Border(bottom=rule)
        ws.cell(row=row, column=1).alignment = Alignment(vertical="top", wrap_text=True)
        if isinstance(value, str) and len(value) > 100:
            ws.row_dimensions[row].height = 15 * (len(value) // 100 + 1)
        row += 1
    ws.freeze_panes = "A3"

    wb.properties.title = f"{BRAND['name']} - ROI Metrics Report"
    wb.properties.creator = BRAND["name"]
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
