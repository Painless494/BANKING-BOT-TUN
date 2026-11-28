"""The alliance economy report: what the ALLIANCE earned and spent in a period. Members' own money is left out on purpose.

Revenue     = tax collected every turn (the very records the per-turn tax summary adds up)
              + alliance donations (#ignore) ONLY when the report is told to include them.
Expenditure = money that left the alliance's own treasury: grants, bulk payments, raws, ECON payments, anything staff sent
              from alliance funds, and money that left the bank outside TUN Bank and was reviewed as alliance money.
Not counted = members' deposits and members' withdrawals (their own money passing through), loan repayments, and moves between
              our own banks (main <-> offshore). They are listed as information so the picture can be checked.
Needs review = money that left the bank but nobody has yet said whether it was a member's withdrawal or alliance money. It is
              NEVER guessed: it is shown separately and not counted until staff decide it with /bank review.

Cash is exact. Resources are valued at current market prices, the same way every other TUN Bank report values them.
"""
from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import io
import json
import logging

from . import alerts as A
from . import fmt
from . import ledger as L
from . import money as M
from .config import cfg_bool, cfg_get
from .util import utcnow
from .valuation import value_amounts

log = logging.getLogger("tunbank.econreport")

REPORT_DELAY = dt.timedelta(hours=1)            # an automatic report waits this long after the period ends, so the last turn is in

CHOICES = {
    "7d": "Last 7 days", "30d": "Last 30 days",
    "this_week": "This week so far (Mon-Sun)", "last_week": "Last full week (Mon-Sun)",
    "this_month": "This month so far", "last_month": "Last full month",
    "custom": "Custom dates",
}


class ReportError(ValueError):
    """The message is safe to show to the person."""


@dataclasses.dataclass(frozen=True)
class Period:
    start: dt.datetime            # inclusive, UTC
    end: dt.datetime              # exclusive, UTC
    label: str
    partial: bool = False         # the period has not finished yet

    @property
    def days(self) -> float:
        return (self.end - self.start).total_seconds() / 86400


def _midnight(d: dt.datetime) -> dt.datetime:
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _fmt_range(start: dt.datetime, end: dt.datetime) -> str:
    last = end - dt.timedelta(seconds=1)
    if start.year == last.year and start.month == last.month:
        return f"{start:%d}–{last:%d %b %Y}" if start.date() != last.date() else f"{start:%d %b %Y}"
    return f"{start:%d %b %Y} – {last:%d %b %Y}"


def week_of(d: dt.datetime) -> Period:
    start = _midnight(d) - dt.timedelta(days=d.weekday())
    return Period(start, start + dt.timedelta(days=7), f"Week {_fmt_range(start, start + dt.timedelta(days=7))}")


def month_of(d: dt.datetime) -> Period:
    start = _midnight(d).replace(day=1)
    end = (start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1))
    return Period(start, end, f"{start:%B %Y}")


def parse_date(text: str, what: str) -> dt.datetime:
    try:
        return dt.datetime.strptime(text.strip(), "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        raise ReportError(f"{what} must look like 2026-10-31 (year-month-day).") from None


def period_for(kind: str, now: dt.datetime | None = None, start: str = "", end: str = "") -> Period:
    now = now or utcnow()
    if kind == "7d" or kind == "30d":
        n = int(kind[:-1])
        s = _midnight(now) - dt.timedelta(days=n - 1)
        e = _midnight(now) + dt.timedelta(days=1)
        return Period(s, e, f"Last {n} days ({_fmt_range(s, e)})", partial=True)
    if kind == "this_week":
        p = week_of(now)
        return dataclasses.replace(p, label=p.label + " (so far)", partial=True)
    if kind == "last_week":
        return week_of(week_of(now).start - dt.timedelta(days=1))
    if kind == "this_month":
        p = month_of(now)
        return dataclasses.replace(p, label=p.label + " (so far)", partial=True)
    if kind == "last_month":
        return month_of(month_of(now).start - dt.timedelta(days=1))
    if kind == "custom":
        if not start.strip():
            raise ReportError("For custom dates give a start date, e.g. 2026-10-01 (the end date is optional and defaults to today).")
        s = parse_date(start, "The start date")
        e_day = parse_date(end, "The end date") if end.strip() else _midnight(now)
        if e_day < s:
            raise ReportError("The end date is before the start date.")
        e = e_day + dt.timedelta(days=1)
        if (e - s).days > 400:
            raise ReportError("A custom report can cover at most 400 days.")
        return Period(s, e, _fmt_range(s, e), partial=e > now)
    raise ReportError("Unknown period.")


def previous_of(p: Period) -> Period:
    """The period right before this one: the previous calendar month for a month, else the same number of days."""
    if p.start.day == 1 and month_of(p.start).end == p.end:
        prev = month_of(p.start - dt.timedelta(days=1))
        return dataclasses.replace(prev, partial=False)
    length = p.end - p.start
    return Period(p.start - length, p.start, _fmt_range(p.start - length, p.start))


# ---------------------------------------------------------------------------------------------------- computing
@dataclasses.dataclass
class Bucket:
    amounts: dict = dataclasses.field(default_factory=dict)
    count: int = 0

    def add(self, amounts: dict):
        self.amounts = M.add(self.amounts, amounts)
        self.count += 1


@dataclasses.dataclass
class Report:
    period: Period
    include_donations: bool
    tax: Bucket
    donations: Bucket                                      # computed even when excluded, so the report can say what it left out
    expenditure: dict                                      # category -> Bucket
    member_deposits: Bucket
    member_withdrawals: Bucket
    loan_repayments: Bucket
    internal_moves: Bucket
    needs_review: Bucket
    rows: list                                             # every line, for the detail export
    first_data_at: str | None = None


def _ts(expr_date: str, expr_fallback: str) -> str:
    return f"replace(substr(coalesce({expr_date},{expr_fallback}),1,19),'T',' ')"


def _bounds(p: Period) -> tuple[str, str]:
    return p.start.strftime("%Y-%m-%d %H:%M:%S"), p.end.strftime("%Y-%m-%d %H:%M:%S")


def _tx_category(conn, tx_id: int, reason: str | None) -> str:
    from .disburse import LABEL
    row = conn.execute("SELECT b.kind FROM disbursement_items i JOIN disbursement_batches b ON b.id=i.batch_id WHERE i.tx_id=?", (tx_id,)).fetchone()
    if row:
        return LABEL.get(row["kind"], row["kind"].replace("_", " ").title())
    if conn.execute("SELECT 1 FROM bulk_items WHERE tx_id=?", (tx_id,)).fetchone():
        return "Bulk payments"
    if conn.execute("SELECT 1 FROM grants WHERE tx_id=?", (tx_id,)).fetchone():
        return "Grants"
    low = (reason or "").lower()
    if low.startswith("loan") or " loan" in low:
        return "Loans issued (money lent)"
    return "Other payments by staff"


def compute(conn, period: Period, include_donations: bool, own_bank_ids=()) -> Report:
    lo, hi = _bounds(period)
    rep = Report(period, include_donations, Bucket(), Bucket(), {}, Bucket(), Bucket(), Bucket(), Bucket(), Bucket(), [])
    first = conn.execute("SELECT MIN(first_seen_at) FROM pnw_records WHERE status!='BASELINE'").fetchone()[0]
    rep.first_data_at = first

    # ---- revenue: tax (exactly what the per-turn tax summary adds up)
    when = _ts("record_date", "recorded_at")
    for r in conn.execute(f"SELECT id, nation_id, amounts_json, {when} AS ts FROM tax_records WHERE {when}>=? AND {when}<?", (lo, hi)):
        amounts = json.loads(r["amounts_json"])
        rep.tax.add(amounts)
        rep.rows.append(("REVENUE", "Tax", r["ts"], r["nation_id"], amounts, f"tax record #{r['id']}"))

    pw = _ts("p.record_date", "p.first_seen_at")
    base = (f"FROM pnw_records p LEFT JOIN transactions t ON t.id=p.tx_id "
            f"WHERE {pw}>=? AND {pw}<? AND p.status NOT IN ('BASELINE','DISMISSED')")
    # ---- inbound: donations (revenue only if asked), members' deposits and loan repayments (never revenue)
    for r in conn.execute(f"SELECT p.id, p.sender_id, p.classification, p.amounts_json, {pw} AS ts {base} AND p.direction='IN' "
                          "AND p.classification IN ('ALLIANCE_DONATION','MEMBER_DEPOSIT','LOAN_REPAYMENT')", (lo, hi)):
        amounts = json.loads(r["amounts_json"])
        kind = r["classification"]
        if kind == "ALLIANCE_DONATION":
            rep.donations.add(amounts)
            if include_donations:
                rep.rows.append(("REVENUE", "Donations", r["ts"], r["sender_id"], amounts, f"PnW record #{r['id']}"))
            else:
                rep.rows.append(("NOT COUNTED", "Donations (switched off)", r["ts"], r["sender_id"], amounts, f"PnW record #{r['id']}"))
        elif kind == "MEMBER_DEPOSIT":
            rep.member_deposits.add(amounts)
            rep.rows.append(("NOT COUNTED", "Member deposit", r["ts"], r["sender_id"], amounts, f"PnW record #{r['id']}"))
        else:
            rep.loan_repayments.add(amounts)
            rep.rows.append(("NOT COUNTED", "Loan repayment", r["ts"], r["sender_id"], amounts, f"PnW record #{r['id']}"))

    # ---- outbound: sort every record that left one of our banks
    external = {r[0] for r in conn.execute("SELECT pnw_record_id FROM external_withdrawals")}
    offshore_moves = {r[0] for r in conn.execute("SELECT pnw_record_id FROM offshore_transfers WHERE pnw_record_id IS NOT NULL")}
    own = {int(i) for i in own_bank_ids if i}
    for r in conn.execute(f"SELECT p.id, p.receiver_id, p.receiver_type, p.classification, p.status, p.tx_id, p.amounts_json, "
                          f"t.funding_source, t.reason AS tx_reason, {pw} AS ts {base} AND p.direction='OUT'", (lo, hi)):
        amounts = json.loads(r["amounts_json"])
        ref = f"PnW record #{r['id']}" + (f" · payment #{r['tx_id']}" if r["tx_id"] else "")
        who = r["receiver_id"]
        if r["id"] in offshore_moves or (r["receiver_type"] == 2 and who in own):
            rep.internal_moves.add(amounts)                     # our own money moving between our own banks: neither revenue nor spending
            rep.rows.append(("NOT COUNTED", "Move between our own banks", r["ts"], who, amounts, ref))
        elif r["status"] == "AWAITING_REVIEW":
            rep.needs_review.add(amounts)
            rep.rows.append(("NEEDS REVIEW", "Not yet classified", r["ts"], who, amounts, ref))
        elif r["tx_id"]:
            if r["funding_source"] == "ALLIANCE":
                cat = _tx_category(conn, r["tx_id"], r["tx_reason"])
                rep.expenditure.setdefault(cat, Bucket()).add(amounts)
                rep.rows.append(("EXPENDITURE", cat, r["ts"], who, amounts, ref))
            else:
                rep.member_withdrawals.add(amounts)
                rep.rows.append(("NOT COUNTED", "Member withdrawal", r["ts"], who, amounts, ref))
        elif r["id"] in external:
            rep.member_withdrawals.add(amounts)
            rep.rows.append(("NOT COUNTED", "Member withdrawal (made outside TUN Bank)", r["ts"], who, amounts, ref))
        else:
            cat = "Sent outside TUN Bank (reviewed as alliance money)"
            rep.expenditure.setdefault(cat, Bucket()).add(amounts)
            rep.rows.append(("EXPENDITURE", cat, r["ts"], who, amounts, ref))
    return rep


# ---------------------------------------------------------------------------------------------------- numbers and layout
def value_of(amounts: dict, snap):
    return value_amounts(amounts, snap)


def totals(rep: Report, snap) -> dict:
    """Cents for revenue / expenditure / net, and whether every resource had a price."""
    revenue = M.add(rep.tax.amounts, rep.donations.amounts if rep.include_donations else {})
    spent: dict = {}
    for b in rep.expenditure.values():
        spent = M.add(spent, b.amounts)
    rv, ev = value_of(revenue, snap), value_of(spent, snap)
    complete = rv.complete and ev.complete
    r, e = rv.total_cents or 0, ev.total_cents or 0
    return {"revenue": r, "expenditure": e, "net": r - e, "complete": complete, "revenue_amounts": revenue, "spent_amounts": spent}


def signed(cents: int) -> str:
    return ("+" if cents > 0 else "−" if cents < 0 else "") + fmt.dollars(abs(cents))


def line(label: str, amounts: dict, snap, count: int | None = None) -> str:
    v = value_of(amounts, snap).total_cents
    extra = f" · {count} payment{'s' if count != 1 else ''}" if count is not None else ""
    return f"{label} **{fmt.dollars(v)}**{extra}"


def build_card(rep: Report, snap, previous: Report | None = None, previous_snap=None) -> A.Card:
    t = totals(rep, snap)
    net = t["net"]
    color = A.GREEN if net > 0 else A.RED if net < 0 else A.BLUE
    c = A.Card(f"📊 Economy report · {rep.period.label}",
               "Alliance money only. **Members' deposits and withdrawals are not counted.** Resources are valued at current market prices.",
               color, kind="ECON_REPORT")
    rev = [line("Tax collected", rep.tax.amounts, snap, None) + f" · {rep.tax.count} records"]
    if rep.include_donations:
        rev.append(line("Donations", rep.donations.amounts, snap, None) + f" · {rep.donations.count}")
    else:
        rev.append(f"_Donations not included_ ({rep.donations.count} received, {fmt.dollars(value_of(rep.donations.amounts, snap).total_cents)})"
                   if rep.donations.count else "_Donations not included_")
    c.add(f"💰 Revenue · {fmt.dollars(t['revenue'])}", "\n".join(rev))
    if rep.expenditure:
        order = sorted(rep.expenditure.items(), key=lambda kv: -(value_of(kv[1].amounts, snap).total_cents or 0))
        exp = [line(name, b.amounts, snap, b.count) for name, b in order]
    else:
        exp = ["_Nothing left the alliance treasury._"]
    c.add(f"💸 Expenditure · {fmt.dollars(t['expenditure'])}", "\n".join(exp)[:1000])
    word = "surplus" if net > 0 else "deficit" if net < 0 else "break-even"
    c.add("📈 Net result", f"**{signed(net)}** ({word})")
    if previous is not None:
        pt = totals(previous, previous_snap or snap)
        d = net - pt["net"]
        c.add(f"Before that · {previous.period.label}",
              f"Revenue {fmt.dollars(pt['revenue'])} · Expenditure {fmt.dollars(pt['expenditure'])} · Net {signed(pt['net'])}\n"
              f"Net changed by {signed(d)}")
    memo = [f"Member deposits {fmt.dollars(value_of(rep.member_deposits.amounts, snap).total_cents)}",
            f"Member withdrawals {fmt.dollars(value_of(rep.member_withdrawals.amounts, snap).total_cents)}"]
    if rep.loan_repayments.count:
        memo.append(f"Loan repayments {fmt.dollars(value_of(rep.loan_repayments.amounts, snap).total_cents)}")
    if rep.internal_moves.count:
        memo.append(f"Moved between our own banks {fmt.dollars(value_of(rep.internal_moves.amounts, snap).total_cents)}")
    c.add("Not counted (for checking)", " · ".join(memo))
    if rep.needs_review.count:
        c.add("⚠️ Needs review — NOT in the totals",
              f"{rep.needs_review.count} outgoing payment(s) worth {fmt.dollars(value_of(rep.needs_review.amounts, snap).total_cents)} "
              "have not been classified as a member withdrawal or alliance money. Use `/bank review`, then run the report again.")
    notes = []
    if not t["complete"]:
        notes.append("Some resources had no price, so their value is left out of the totals.")
    if rep.first_data_at and rep.period.start.strftime("%Y-%m-%dT%H:%M:%SZ") < rep.first_data_at:
        notes.append(f"TUN Bank has records only since {rep.first_data_at[:10]}: earlier days in this period may be incomplete.")
    if rep.period.partial:
        notes.append("This period is not finished yet.")
    c.footer = " ".join(notes) or "Detail: run with detail:True · TUN Bank"
    return c


def detail_csv(rep: Report, snap) -> bytes:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["section", "category", "time_utc", "nation_id", "value_usd", *M.RESOURCES, "reference"])
    for section, cat, ts, nid, amounts, ref in sorted(rep.rows, key=lambda r: (r[0], r[1], r[2])):
        v = value_of(amounts, snap).total_cents
        w.writerow([section, cat, ts, nid or "", f"{(v or 0) / 100:.2f}", *[M.fmt_units(r, amounts[r]) if amounts.get(r) else "" for r in M.RESOURCES], ref])
    return out.getvalue().encode("utf-8-sig")


# ---------------------------------------------------------------------------------------------------- running it
def own_banks(settings) -> list:
    ids = [getattr(settings, "alliance_id", None)]
    off = getattr(settings, "offshore", None)
    if off is not None:
        ids.append(getattr(off, "alliance_id", None))
    return [i for i in ids if i]


async def generate(svc, period: Period, include_donations: bool | None = None, with_previous: bool = True):
    """-> (card, report, snapshot). `include_donations` None means: use the saved setting."""
    import asyncio
    snap = await svc.prices.get()
    banks = own_banks(svc.settings)

    def work():
        with svc.db.read() as conn:
            inc = cfg_bool(conn, "econ_report_donations") if include_donations is None else include_donations
            rep = compute(conn, period, inc, banks)
            prev = compute(conn, previous_of(period), inc, banks) if with_previous else None
        return rep, prev
    rep, prev = await asyncio.to_thread(work)
    return build_card(rep, snap, prev), rep, snap


# ---------------------------------------------------------------------------------------------------- automatic reports
def due_periods(now: dt.datetime) -> dict:
    """The most recent FINISHED week and month that are old enough to report (REPORT_DELAY after they ended)."""
    out = {}
    wk = week_of(week_of(now - REPORT_DELAY).start - dt.timedelta(days=1))
    if now >= wk.end + REPORT_DELAY:
        out["weekly"] = (wk, f"{wk.start:%G-W%V}")
    mo = month_of(month_of(now - REPORT_DELAY).start - dt.timedelta(days=1))
    if now >= mo.end + REPORT_DELAY:
        out["monthly"] = (mo, f"{mo.start:%Y-%m}")
    return out


async def send_due_reports(svc, now: dt.datetime | None = None) -> list:
    """Post the weekly / monthly report once per finished period. Safe to call every few minutes; at most once per period.

    Switching a schedule on never posts an old report: the first run only remembers the latest finished period, and the first
    report is the next one to finish. Nothing is posted (and nothing remembered) while no channel is configured."""
    now = now or utcnow()
    posted = []
    with svc.db.read() as conn:
        enabled = {"weekly": cfg_bool(conn, "econ_report_weekly"), "monthly": cfg_bool(conn, "econ_report_monthly")}
        channel = cfg_get(conn, "econ_report_channel_id") or cfg_get(conn, "econ_log_channel_id")
    for kind, (period, key) in due_periods(now).items():
        state = f"econ_report_last_{kind}"
        with svc.db.read() as conn:
            last = L.get_state(conn, state)
        if not enabled[kind]:
            if last:                                               # switched off: forget, so switching on later never posts an old report
                with svc.db.tx() as conn:
                    L.set_state(conn, state, "")
            continue
        if last == key:
            continue
        if not last:                                           # just switched on: start from the NEXT finished period
            with svc.db.tx() as conn:
                L.set_state(conn, state, key)
            continue
        if not str(channel).isdigit():
            continue                                               # nowhere to post: try again once a channel exists
        with svc.db.tx() as conn:                                  # remember first: a report is never posted twice
            L.set_state(conn, state, key)
            L.audit(conn, "system", "ECON_REPORT_POSTED", f"{kind}:{key}", {"channel": str(channel)})
        try:
            card, _, _ = await generate(svc, period)
            card.title = f"📊 {kind.title()} economy report · {period.label}"
            await svc.alerts.econ(card, channel_id=str(channel))
            posted.append((kind, key))
        except Exception:  # noqa: BLE001
            log.exception("automatic %s economy report failed; run /economy report to produce it by hand", kind)
    return posted
