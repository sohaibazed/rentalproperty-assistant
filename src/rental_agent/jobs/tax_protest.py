"""tax-protest: evidence, forms and dates for a Collin County property-tax protest.

Three kinds of evidence, each computed by Python from data that has a source:
- Similar homes (unequal appraisal): the district's own values for homes of about the same
  size in the same district neighborhood, read from its open data (collin_cad). No model.
- Recent sales: found by the sales-researcher subagent (WebSearch/WebFetch only), in the same
  two-agent setup as research-rent.
- Rental income: the comparable rents research-rent saved and the expenses recorded for the
  property, at a market rate of return the researcher found.

The main agent weighs the three and gives an opinion of value. Python checks that opinion
against the figures and flags anything doubtful. The report also lists what to file, the
documents to gather and the dates. Nothing is filed and nothing is saved to the database.

Collin County only: the forms, addresses and dates are those of the Collin Central Appraisal
District and its Appraisal Review Board (2026 procedures), under Texas Tax Code chapter 41.
"""

import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from rental_agent import collin_cad, db
from rental_agent.config import Settings
from rental_agent.jobs import rent_research as rr
from rental_agent.jobs.lease import long_date, money

MIN_COMPS = 5
SHOWN_COMPS = 20                                    # listed in the report; the median uses all
SIZE_BAND = (Decimal("0.8"), Decimal("1.2"))        # similar homes: living area vs the subject's
SALE_SIZE_BAND = (Decimal("0.7"), Decimal("1.3"))   # the researcher aims for 20%; drop past 30%
PRICE_RATIO_LIMITS = (Decimal("0.4"), Decimal("2.5"))   # sale price vs the district's value
CAP_RATE_LIMITS = (Decimal("0.03"), Decimal("0.12"))    # a believable yearly rate of return
# Schedule E lines that are not costs of running the property.
NOT_OPERATING = {"mortgage_interest", "other_interest", "depreciation"}

OFFICE = "250 Eldorado Pkwy, McKinney, TX 75069"
NOTICE_DAY = (4, 15)         # real-property notices were mailed on April 15 in 2024-2026
RECORDS_APPROVED = (7, 20)   # the board approves the appraisal records by about this day
# Tax Code 23.231(k): the 20% yearly limit on value increases for non-homestead property.
INCREASE_LIMIT_ENDED = date(2026, 12, 31)

FILING = """\
1. **Notice of Protest.** The only required filing, and it is free. File it on the district's
   Online Appeals (eFile) site with the PIN printed on your notice. Or take form CCAD-132 from
   [collinarb.org/forms](https://collinarb.org/forms/) and send it by mail, hand it in, or use
   the drop box at {office}. Fax and email are not accepted.
   - Owner, as the district lists it: {owner}
   - Property: {address} (district property ID {prop_id})
   - Reason: tick **Market & Equity** (boxes 1 and 2). A reason you don't tick can't be argued
     later.
   - Opinion of value (optional): {opinion}
   - Hearing type: in person, by phone, by video, or on a written declaration.
   - If an LLC owns the property, sign with your title and bring proof that you act for it
     (Certificate of Formation or Company Agreement).
2. **Declaration of Evidence** (on the same forms page; no notary needed). Only for a hearing
   by phone, by video or on paper.
3. **Form 50-162, Appointment of Agent.** Only if you hire a paid tax agent.
4. **Form AP-219, Request for Binding Arbitration.** Only to appeal the board's order."""

DOCUMENTS = """\
- The Notice of Appraised Value: it has the eFile PIN, the exact deadline and the QR code for
  an informal review.
- This report, printed to PDF. The board takes PDF files, 20 MB in total for a property.
- Photos and repair estimates for anything that lowers the value: roof, foundation, HVAC,
  dated kitchen or baths.
- Your closing statement, if you bought the house in the last few years.
- The lease and the rent received, to back the income figures.
- A recent appraisal, if you have one."""

# Prompts live in prompts/agent-prompts.md; read at each run so web edits apply.

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sales", "cap_rate", "cap_rate_source_url", "market_notes",
                 "opinion_of_value", "rationale"],
    "properties": {
        "sales": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["address", "sqft", "year_built", "sale_price", "sale_date",
                             "source_url"],
                "properties": {
                    "address": {"type": "string"},
                    "sqft": {"type": ["integer", "null"]},
                    "year_built": {"type": ["integer", "null"]},
                    "sale_price": {"type": "number"},
                    "sale_date": {"type": ["string", "null"]},
                    "source_url": {"type": "string"},
                },
            },
        },
        "cap_rate": {"type": ["number", "null"]},        # a percentage, e.g. 5.5
        "cap_rate_source_url": {"type": ["string", "null"]},
        "market_notes": {"type": "string"},
        "opinion_of_value": {"type": "number"},
        "rationale": {"type": "string"},
    },
}


@dataclass(frozen=True)
class Home:
    """A home as the district records it."""
    prop_id: int
    address: str
    year_built: int | None
    sqft: int
    pool: bool
    value: Decimal                  # the district's market value

    @property
    def per_sqft(self) -> Decimal:
        return self.value / self.sqft


@dataclass(frozen=True)
class Sale:
    address: str
    year_built: int | None
    sqft: int
    price: Decimal
    sold_on: date
    source_url: str

    @property
    def per_sqft(self) -> Decimal:
        return self.price / self.sqft


@dataclass(frozen=True)
class Record:
    """The district's record of the property and of similar homes around it."""
    home: Home
    owner: str
    neighborhood: str               # the district's neighborhood code
    value_year: int                 # the tax year these values are for
    prior_value: Decimal | None
    comps: list[Home]               # same neighborhood and kind, within SIZE_BAND
    notice_date: date | None        # when this tax year's notice was mailed, once it is
    deadline: date | None           # the protest deadline printed on that notice
    source: collin_cad.Dataset
    data_date: date | None


@dataclass(frozen=True)
class Income:
    rent: Decimal                   # monthly: the median comparable rent, else the lease rent
    comps: list[rr.Comp]            # from the saved rent research; empty if there is none
    research_year: int | None
    lease_rent: Decimal | None
    expense_year: int
    expenses: dict[str, Decimal]    # operating costs recorded for the property, by category
    shared_expenses: int            # LLC-wide rows, left out

    @property
    def net(self) -> Decimal:
        """Yearly rent less operating costs."""
        return self.rent * 12 - sum(self.expenses.values(), Decimal(0))


@dataclass(frozen=True)
class Facts:
    property_id: int
    address: str
    description: str                # type, bedrooms and bathrooms, as the owner recorded them
    sqft: int | None                # the owner's figure, to check the district's against
    year: int
    record: Record
    income: Income | None           # None when there is no rent research and no lease

    def as_text(self) -> str:
        record, home = self.record, self.record.home
        lines = [
            f"Address: {self.address}",
            f"Property: {self.description}",
            f"Tax year: {self.year} (value as of January 1, {self.year})",
            (f"District record ({record.value_year} values): {home.sqft:,} sq ft, built "
             f"{home.year_built or 'unknown'}, {'pool' if home.pool else 'no pool'}, market "
             f"value {dollars(home.value)} ({per_sqft(home.per_sqft)})"),
        ]
        if record.comps:
            lines.append(
                f"Similar homes: the {len(record.comps)} homes in district neighborhood "
                f"{record.neighborhood} within 20% of this size have a median of "
                f"{per_sqft(median_per_sqft(record.comps))}, which puts this home at "
                f"{dollars(at_median(record.comps, home.sqft))}")
        if self.income:
            costs = sum(self.income.expenses.values(), Decimal(0))
            lines.append(
                f"Rental income: {money(self.income.rent)}/month ({rent_basis(self.income)}); "
                f"operating expenses recorded for {self.income.expense_year} {dollars(costs)}; "
                f"net income {dollars(self.income.net)} a year")
        return "\n".join(lines) + "\n"


@dataclass
class Protest:
    facts: Facts
    equity_value: Decimal | None    # this home at the similar homes' median value per sq ft
    sales: list[Sale]
    dropped: list[tuple[str, str]]  # (address, reason)
    sales_value: Decimal | None     # this home at the sales' median price per sq ft
    opinion: Decimal
    rationale: str
    market_notes: str
    deadline: date
    deadline_is_official: bool      # from the district's notice data, not the general rule
    hearing: date | None
    cap_rate: Decimal | None = None       # a fraction; kept only if it has a source
    cap_rate_source: str | None = None
    income_value: Decimal | None = None   # net income / cap_rate
    flags: list[str] = field(default_factory=list)


def dollars(amount: Decimal) -> str:
    return f"${amount:,.0f}"


def per_sqft(amount: Decimal) -> str:
    return f"${amount:,.2f}/sq ft"


def rent_basis(income: Income) -> str:
    if income.comps:
        return (f"median of {len(income.comps)} comparable rents from the "
                f"{income.research_year} rent research")
    return "the current lease"


def median_per_sqft(homes: list[Home] | list[Sale]) -> Decimal:
    return statistics.median(h.per_sqft for h in homes)


def at_median(homes: list[Home] | list[Sale], sqft: int) -> Decimal | None:
    """What a home of `sqft` is worth at the median value per square foot of `homes`."""
    return (median_per_sqft(homes) * sqft).quantize(Decimal(1)) if homes else None


# --- Dates --------------------------------------------------------------------------------


def default_year(today: date) -> int:
    """The protest season that is open or comes next. A year's season ends when the board
    approves the records in late July."""
    return today.year if today < date(today.year, 8, 1) else today.year + 1


def filing_deadline(year: int, notice_date: date | None) -> date:
    """Tax Code 41.44: May 15, or 30 days after the notice was mailed if that is later, moved
    to the next business day from a weekend. Holidays are not checked; the notice prints the
    exact date."""
    day = date(year, 5, 15)
    if notice_date:
        day = max(day, notice_date + timedelta(days=30))
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def business_days_before(day: date, count: int) -> date:
    while count:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            count -= 1
    return day


def key_dates(p: Protest) -> list[tuple[str, str]]:
    """(when, what) for the tax year, in order."""
    year, record = p.facts.year, p.facts.record
    rows = []
    if year == INCREASE_LIMIT_ENDED.year + 1:
        rows.append((long_date(INCREASE_LIMIT_ENDED),
                     ("The 20% yearly limit on value increases for property that is not a "
                     f"homestead ended (Tax Code 23.231). The {year} value can rise to full "
                     "market value, unless the Legislature has renewed the limit since.")))
    rows += [
        (long_date(date(year, 1, 1)),
         "Appraisal date. The value is set as of this day, so sales closest to it count most."),
        (long_date(record.notice_date) if record.notice_date
         else f"About {long_date(date(year, *NOTICE_DAY))}",
         ("The district mails the Notice of Appraised Value. Book an informal review with the "
         "QR code on it; appointments run through May 15. A review does not replace the "
         "protest.")),
        (f"**{long_date(p.deadline)}**",
         "Last day to file the protest"
         + ("" if p.deadline_is_official else " (expected; the notice prints the exact date)")
         + ". File online or in person: a mailed protest must be postmarked by this day, and "
           "postmarks now run 1 to 2 days late."),
        ("Mid-May to early July",
         "Hearings for homes. The board sends your hearing date at least 15 days ahead."),
    ]
    if p.hearing:
        rows += [
            (long_date(p.hearing - timedelta(days=14)),
             "From this day the district must give you the evidence it will use. Ask for it."),
            (long_date(p.hearing - timedelta(days=10)),
             "Last day to switch to a phone or video hearing, in writing."),
            (long_date(business_days_before(p.hearing, 3)),
             "Upload your evidence through Online Appeals by this day (PDF, 20 MB in total)."),
            (f"**{long_date(p.hearing)}**",
             (f"Your hearing, at {OFFICE} unless you chose phone or video. Each side gets about "
             "5 minutes. If you miss it, file a good-cause statement within 4 days.")),
        ]
    else:
        rows.append(("Before your hearing",
                     ("14 days before, ask for the district's evidence. 10 days before is the "
                     "last day to switch to phone or video. 3 business days before, upload "
                     "your evidence (PDF, 20 MB in total). Run this job again with the hearing "
                     "date for the exact days.")))
    rows += [
        (f"About {long_date(date(year, *RECORDS_APPROVED))}",
         ("The board approves the appraisal records. A late protest, which needs good cause, is "
         "not possible after this.")),
        ("60 days after you receive the board's order",
         ("Last day to ask for binding arbitration (form AP-219, with a deposit) or to sue in "
         "district court.")),
        (f"Before {long_date(date(year + 1, 2, 1))}",
         (f"The {year} taxes are due. During an appeal you must still pay at least the tax on "
         "the value that is not in dispute.")),
    ]
    return rows


# --- Evidence -----------------------------------------------------------------------------


def clean_sales(raw: list[dict[str, Any]], home: Home, year: int,
                today: date) -> tuple[list[Sale], list[tuple[str, str]]]:
    """Keep sales that have a source, a size near the subject's, a date from the year before
    the appraisal date up to today, and a believable price."""
    earliest = date(year - 1, 1, 1)
    low, high = (home.value * r for r in PRICE_RATIO_LIMITS)
    min_sqft, max_sqft = (home.sqft * b for b in SALE_SIZE_BAND)
    kept: list[Sale] = []
    dropped: list[tuple[str, str]] = []
    seen: set[tuple[str, date]] = set()
    for item in raw:
        address = str(item.get("address") or "unknown").strip()
        url = str(item.get("source_url") or "").strip()
        sqft = item.get("sqft")
        try:
            price = Decimal(str(item.get("sale_price"))).quantize(Decimal(1))
            sold_on = date.fromisoformat(str(item.get("sale_date")))
        except (ArithmeticError, ValueError):
            dropped.append((address, "no usable sale price or date"))
            continue
        if not re.match(r"https?://", url):
            dropped.append((address, "no source URL"))
        elif not sqft:
            dropped.append((address, "no square footage"))
        elif not min_sqft <= sqft <= max_sqft:
            dropped.append((address, f"{sqft:,} sq ft is too far from {home.sqft:,}"))
        elif not earliest <= sold_on <= today:
            dropped.append((address, (f"sold {long_date(sold_on)}, outside "
                                     f"{long_date(earliest)} to today")))
        elif not low <= price <= high:
            dropped.append((address, (f"price {dollars(price)} outside "
                                     f"{dollars(low)}–{dollars(high)}")))
        elif (address.lower(), sold_on) in seen:
            dropped.append((address, "duplicate"))
        else:
            seen.add((address.lower(), sold_on))
            kept.append(Sale(address, item.get("year_built"), int(sqft), price, sold_on, url))
    return kept, dropped


def _use_cap_rate(p: Protest, raw: dict[str, Any]) -> None:
    """Keep the researcher's rate of return only if it has a source and is believable, and
    value the property's net income at it."""
    if raw.get("cap_rate") is None:
        return
    rate = Decimal(str(raw["cap_rate"])) / 100
    url = str(raw.get("cap_rate_source_url") or "").strip()
    if not re.match(r"https?://", url):
        p.flags.append("The market rate of return came without a source, so it is not used.")
    elif not CAP_RATE_LIMITS[0] <= rate <= CAP_RATE_LIMITS[1]:
        p.flags.append(f"A market rate of return of {rate:.1%} is not believable, so it is not "
                       "used.")
    else:
        p.cap_rate, p.cap_rate_source = rate, url
        income = p.facts.income
        if income and income.net > 0:
            p.income_value = (income.net / rate).quantize(Decimal(1))


def analyze(facts: Facts, raw: dict[str, Any], today: date,
            hearing: date | None = None) -> Protest:
    record, home, income = facts.record, facts.record.home, facts.income
    sales, dropped = clean_sales(raw.get("sales", []), home, facts.year, today)
    if not record.comps and not sales:
        raise RuntimeError("No similar homes and no usable sales were found; nothing to "
                           "argue from.")
    p = Protest(
        facts=facts,
        equity_value=at_median(record.comps, home.sqft),
        sales=sales,
        dropped=dropped,
        sales_value=at_median(sales, home.sqft),
        opinion=Decimal(str(raw["opinion_of_value"])).quantize(Decimal(1)),
        rationale=str(raw.get("rationale", "")).strip(),
        market_notes=str(raw.get("market_notes", "")).strip(),
        deadline=record.deadline or filing_deadline(facts.year, record.notice_date),
        deadline_is_official=record.deadline is not None,
        hearing=hearing,
    )
    if p.opinion <= 0:
        raise RuntimeError("Agent returned a non-positive value")

    if record.value_year != facts.year:
        p.flags.append(
            f"The district has not published {facts.year} values yet (it mails notices about "
            f"{long_date(date(facts.year, *NOTICE_DAY))}). This report uses its "
            f"{record.value_year} values as a preview. Run it again when the notice arrives.")
    if today > p.deadline:
        p.flags.append(
            f"The {facts.year} filing deadline has passed. A late protest needs good cause and "
            "must reach the board before it approves the records (about "
            f"{long_date(date(facts.year, *RECORDS_APPROVED))}).")
    _use_cap_rate(p, raw)
    figures = [v for v in (p.equity_value, p.sales_value, p.income_value) if v]
    if p.opinion >= home.value:
        p.flags.append("The evidence found does not support a value below the district's "
                       f"{dollars(home.value)}.")
    elif p.opinion < min(figures):
        p.flags.append("The opinion of value is below every figure computed from the evidence "
                       f"(the lowest is {dollars(min(figures))}).")
    if len(record.comps) < MIN_COMPS:
        p.flags.append(f"Only {len(record.comps)} similar homes in the district's neighborhood "
                       f"{record.neighborhood} (want {MIN_COMPS}+); the similar-homes figure "
                       "is weak.")
    if not sales:
        p.flags.append("No usable sales were found, so the opinion of value has no sales "
                       "behind it. Add sales you know of before relying on it.")
    elif len(sales) < MIN_COMPS:
        p.flags.append(f"Only {len(sales)} usable sales (want {MIN_COMPS}+); treat the sales "
                       "figure with caution.")
    if facts.sqft and abs(facts.sqft - home.sqft) * 20 > home.sqft:      # more than 5% apart
        p.flags.append(f"The district lists {home.sqft:,} sq ft; your records say "
                       f"{facts.sqft:,}. If the district is wrong, also tick \"Property "
                       "description is incorrect\" on the protest.")
    if income:
        if not income.comps:
            p.flags.append("No saved rent research, so the income figures use the lease rent. "
                           "Run research-rent first for a market rent.")
        if today.year <= income.expense_year:
            p.flags.append(f"{income.expense_year} is not over, so its recorded expenses are "
                           "incomplete and the net income is overstated.")
        elif "taxes" not in income.expenses:
            p.flags.append(f"No property tax is recorded for {income.expense_year}, so the net "
                           "income is overstated.")
        if income.shared_expenses:
            p.flags.append(f"{income.shared_expenses} LLC-wide expense(s) in "
                           f"{income.expense_year} are not assigned to this property and are "
                           "left out of the net income.")
    return p


# --- Report -------------------------------------------------------------------------------


def _equity_section(p: Protest) -> list[str]:
    record, home = p.facts.record, p.facts.record.home
    lines = ["## Evidence 1: similar homes (unequal appraisal)", ""]
    if not record.comps:
        return lines + [(f"The district's data has no other homes within 20% of this home's "
                        f"size in its neighborhood {record.neighborhood}."), ""]
    lower = sum(c.per_sqft < home.per_sqft for c in record.comps)
    shown = sorted(record.comps, key=lambda c: abs(c.sqft - home.sqft))[:SHOWN_COMPS]
    lines += [
        (f"The district values this home at **{per_sqft(home.per_sqft)}**. The "
         f"{len(record.comps)} other homes in its neighborhood {record.neighborhood} within 20% "
         f"of this home's size have a median of **{per_sqft(median_per_sqft(record.comps))}**, "
         f"and {lower} of them are valued lower per square foot than this home. At the median "
         f"this home would be **{dollars(p.equity_value)}**."),
        "",
        ("The comparison adjusts for size only. Source: "
         f"[{record.source.name}]({record.source.url})"
         + (f", data as of {long_date(record.data_date)}" if record.data_date else "")
         + ". Each property ID can be checked on the district's property search."),
        "",
    ]
    if len(shown) < len(record.comps):
        lines += [f"The {len(shown)} closest in size of {len(record.comps)}:", ""]
    lines += ["| Property ID | Address | Built | Sq ft | Pool | District value | Per sq ft |",
              "|---|---|---|---|---|---|---|"]
    for c in [home] + sorted(shown, key=lambda c: c.per_sqft):
        mark = "**" if c is home else ""
        lines.append(f"| {mark}{c.prop_id}{mark} | {mark}{c.address}"
                     f"{' (this home)' if c is home else ''}{mark} | {c.year_built or '-'} | "
                     f"{c.sqft:,} | {'yes' if c.pool else 'no'} | {dollars(c.value)} | "
                     f"${c.per_sqft:,.2f} |")
    return lines + [""]


def _sales_section(p: Protest) -> list[str]:
    home = p.facts.record.home
    lines = ["## Evidence 2: recent sales", ""]
    if p.sales:
        lines += [
            (f"{len(p.sales)} sales since {long_date(date(p.facts.year - 1, 1, 1))} have a "
             f"median of **{per_sqft(median_per_sqft(p.sales))}**. At that price this home "
             f"({home.sqft:,} sq ft) would be **{dollars(p.sales_value)}**. Texas does not "
             "publish sale prices, so open each source and save a copy of the page as proof."),
            "",
            "| Address | Built | Sq ft | Sold | Price | Per sq ft | Source |",
            "|---|---|---|---|---|---|---|",
        ]
        for s in sorted(p.sales, key=lambda s: s.per_sqft):
            lines.append(f"| {s.address} | {s.year_built or '-'} | {s.sqft:,} | "
                         f"{s.sold_on.isoformat()} | {dollars(s.price)} | ${s.per_sqft:,.2f} | "
                         f"[link]({s.source_url}) |")
    else:
        lines.append("No usable sales were found.")
    if p.dropped:
        lines += ["", "Dropped:", ""] + [f"- {a}: {why}" for a, why in p.dropped]
    return lines + [""]


def _income_section(p: Protest) -> list[str]:
    income, home = p.facts.income, p.facts.record.home
    lines = ["## Evidence 3: rental income", ""]
    if income is None:
        return lines + ["No rent research and no lease are recorded for this property.", ""]
    costs = sum(income.expenses.values(), Decimal(0))
    detail = ", ".join(f"{name.replace('_', ' ')} {dollars(amount)}"
                       for name, amount in sorted(income.expenses.items()))
    lines += [
        "Review boards give this little weight for a single-family house. Use it as support.",
        "",
        f"- **{'Market rent' if income.comps else 'Rent'}:** {money(income.rent)}/month "
        f"({rent_basis(income)})"
        + (f". The current lease is {money(income.lease_rent)}" if income.comps
           and income.lease_rent else ""),
        f"- **Operating expenses recorded for {income.expense_year}:** {dollars(costs)}"
        + (f" ({detail})" if detail else ""),
        f"- **Net income:** {dollars(income.net)} a year",
        f"- **Return at the district's value:** {income.net / home.value:.1%}",
    ]
    if p.income_value:
        lines.append(f"- **At a market rate of return of {p.cap_rate:.1%}** "
                     f"([source]({p.cap_rate_source})) the income supports "
                     f"**{dollars(p.income_value)}**")
    if income.comps:
        lines += ["", *rr.comps_table(income.comps)]
    return lines + [""]


def render_report(p: Protest, today: date) -> str:
    f, record, home = p.facts, p.facts.record, p.facts.record.home
    days = (p.deadline - today).days
    status = "passed" if days < 0 else "today" if days == 0 else f"{days} days left"
    if not p.deadline_is_official:
        status += "; expected, the notice prints the exact date"
    lines = [
        f"# Property tax protest {f.year}: {f.address}",
        "",
        (f"_Generated {long_date(today)}. Information to help you file, not legal or tax "
         "advice. Nothing has been filed._"),
        "",
        "## Summary",
        "",
        f"- **District's market value ({record.value_year}):** {dollars(home.value)}"
        + (f"; the year before, {dollars(record.prior_value)}" if record.prior_value else ""),
    ]
    if p.opinion < home.value:
        cut = home.value - p.opinion
        lines.append(f"- **Opinion of value:** {dollars(p.opinion)}, which is {dollars(cut)} "
                     f"({cut / home.value:.1%}) below the district")
    else:
        lines.append(f"- **Opinion of value:** {dollars(p.opinion)}. The evidence does not "
                     "support a value below the district's.")
    lines.append(f"- **File by:** {long_date(p.deadline)} ({status})")
    for label, value, basis in (
        ("Similar homes", p.equity_value, f"{len(record.comps)} homes in its neighborhood"),
        ("Recent sales", p.sales_value, f"{len(p.sales)} sales"),
        ("Rental income", p.income_value, "net income at a market rate of return"),
    ):
        if value:
            lines.append(f"- **{label}:** {dollars(value)} ({basis})")
    lines += ["", p.rationale, ""]
    if p.flags:
        lines += ["## Check before using", ""] + [f"- {x}" for x in p.flags] + [""]
    lines += ["## Dates", "", "| When | What |", "|---|---|"]
    lines += [f"| {when} | {what} |" for when, what in key_dates(p)]
    lines += [
        "",
        "## What to file",
        "",
        FILING.format(office=OFFICE, owner=record.owner, address=home.address,
                      prop_id=home.prop_id, opinion=dollars(p.opinion)),
        "",
        "## Documents to gather",
        "",
        DOCUMENTS,
        "",
        *_equity_section(p),
        *_sales_section(p),
        *_income_section(p),
    ]
    if p.market_notes:
        lines += ["## Market notes", "", p.market_notes, ""]
    lines += ["## Property facts used", "", "```", f.as_text().rstrip(), "```", ""]
    return "\n".join(lines)


# --- Loading and running ------------------------------------------------------------------


def _day(text: str | None) -> date | None:
    return date.fromisoformat(text[:10]) if text else None


def _home(row: collin_cad.Row) -> Home | None:
    """None when the district has no value or no living area for the row."""
    value = row.get("currvalmarket") or row.get("noticevalmarket")
    area = row.get("imprvmainarea")
    if not value or not area or int(Decimal(area)) <= 0:
        return None
    built = row.get("imprvyearbuilt")
    return Home(int(row["propid"]), str(row.get("situsconcatshort") or "").strip(),
                int(built) if built else None, int(Decimal(area)),
                bool(row.get("imprvpoolflag")), Decimal(value))


def load_record(street_address: str, city: str, year: int, cad_id: int | None) -> Record:
    """The district's record of the property and of similar homes in its neighborhood. Uses
    `year`'s values once the district has published them, else last year's as a preview."""
    sets = collin_cad.datasets()
    for name in (str(year), "Preliminary", str(year - 1)):
        dataset = sets.get(collin_cad.DATA_PREFIX + name)
        row = collin_cad.subject(dataset, street_address, city, cad_id) if dataset else None
        home = _home(row) if row else None
        if home and int(row["propyear"]) in (year, year - 1):
            break
    else:
        raise ValueError(f"The district's open data has no {year} or {year - 1} value for "
                         f"{street_address}. Check the address, or pass the district "
                         "property ID.")
    similar = []
    if row.get("nbhdcode"):
        low, high = (int(home.sqft * b) for b in SIZE_BAND)
        similar = [h for r in collin_cad.neighborhood(dataset, row, low, high)
                   if (h := _home(r)) and h.prop_id != home.prop_id]
    notices = sets.get(collin_cad.NOTICES)
    notice = collin_cad.notice(notices, home.prop_id, year) if notices else None
    prior = row.get("prevvalmarket")
    return Record(
        home=home,
        owner=str(row.get("ownername") or "not listed").strip(),
        neighborhood=str(row.get("nbhdcode") or "not listed"),
        value_year=int(row["propyear"]),
        prior_value=Decimal(prior) if prior else None,
        comps=similar,
        notice_date=_day(notice.get("noticedate")) if notice else None,
        deadline=_day(notice.get("protdeadlinedate")) if notice else None,
        source=dataset,
        data_date=_day(row.get("datadate")),
    )


def load_income(conn, property_id: int, year: int) -> Income | None:
    """Market rent from the saved rent research (else the lease rent), and the operating
    costs recorded for the property in the year before the appraisal date."""
    saved = db.saved_rent_comps(conn, property_id, year)
    lease = db.latest_lease(conn, property_id)
    comps = [rr.Comp(**{**c, "rent": Decimal(str(c["rent"]))}) for c in saved[1]] if saved else []
    if not comps and lease is None:
        return None
    operating = [e for e in db.expenses_for_year(conn, year - 1)
                 if e.category not in NOT_OPERATING]
    by_category: dict[str, Decimal] = defaultdict(Decimal)
    for e in operating:
        if e.property_id == property_id:
            by_category[e.category] += e.amount
    return Income(
        rent=rr.quantile([c.rent for c in comps], 0.5) if comps else lease.monthly_rent,
        comps=comps,
        research_year=saved[0] if saved else None,
        lease_rent=lease.monthly_rent if lease else None,
        expense_year=year - 1,
        expenses=dict(by_category),
        shared_expenses=sum(e.property_id is None for e in operating),
    )


def load_facts(conn, property_id: int, year: int, cad_id: int | None) -> Facts:
    prop = db.property_detail(conn, property_id)
    size = db.property_size(conn, property_id)
    if prop is None or size is None:
        raise ValueError(f"No property with id {property_id}")
    if "collin" not in (prop.county or "").lower():
        raise ValueError("tax-protest covers Collin County only; this property's county is "
                         f"{prop.county or 'not set'}")
    if not prop.city:
        raise ValueError("Fill in properties.city before running tax-protest")
    address = f"{prop.address}{', ' + prop.unit if prop.unit else ''}, {prop.city}, " \
              f"{prop.state} {prop.zip or ''}".strip()
    parts = [size.property_type,
             f"{float(size.beds):g} bedrooms" if size.beds else None,
             f"{float(size.baths):g} bathrooms" if size.baths else None]
    return Facts(
        property_id=property_id,
        address=address,
        description=", ".join(part for part in parts if part) or "house",
        sqft=size.sqft,
        year=year,
        record=load_record(prop.address, prop.city, year, cad_id),
        income=load_income(conn, property_id, year),
    )


async def ask_agents(facts: Facts, settings: Settings) -> dict[str, Any]:
    options = rr.agent_options(settings, system="protest-opinion",
                               researcher="sales-researcher", schema=OUTPUT_SCHEMA)
    return await rr.ask(facts.as_text(), options, settings, "tax-protest")


async def run(
    settings: Settings, *, property_id: int, year: int | None, cad_id: int | None,
    hearing_date: date | None,
) -> tuple[Protest, Path]:
    today = date.today()
    with db.connect(settings.db_url) as conn:
        facts = load_facts(conn, property_id, year or default_year(today), cad_id)

    protest = analyze(facts, await ask_agents(facts, settings), today, hearing_date)

    report = (settings.artifacts_dir / str(facts.year) / "tax-protest"
              / f"{rr.report_slug(facts.address)}.md")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render_report(protest, today))
    return protest, report
