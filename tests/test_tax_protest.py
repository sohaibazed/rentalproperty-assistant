import asyncio
from datetime import date
from decimal import Decimal as D

import pytest
from claude_agent_sdk import ResultMessage

from rental_agent import collin_cad, runner
from rental_agent.config import Settings
from rental_agent.jobs import rent_research as rr
from rental_agent.jobs import tax_protest as tp

TODAY = date(2027, 4, 20)
SOURCE = collin_cad.Dataset("abcd-1234", "Collin CAD Appraisal Data - 2027")
HOME = tp.Home(101, "12 OAK ST", 1995, 1800, False, D(450000))          # $250/sq ft


def home(prop_id, value, sqft=1800):
    return tp.Home(prop_id, f"{prop_id} ELM ST", 1996, sqft, False, D(value))


# $220 to $240/sq ft: median $230, which puts the subject at $414,000.
SIMILAR = [home(200 + i, v) for i, v in enumerate((396000, 405000, 414000, 423000, 432000))]


def record(comps=SIMILAR, value_year=2027, deadline=date(2027, 5, 17)):
    return tp.Record(home=HOME, owner="OAK ST LLC", neighborhood="N0617", value_year=value_year,
                     prior_value=D(430000), comps=comps,
                     notice_date=deadline and date(2027, 4, 15), deadline=deadline,
                     source=SOURCE, data_date=date(2027, 4, 19))


def income(comps=(2500, 2600, 2700), shared=0):
    """$2,600 x 12 less $10,200 of costs: $21,000 a year."""
    rents = [rr.Comp(f"{r} Elm St", 3, 2, 1800, D(r), "2026-10-01", "listed",
                     "https://example.com/r") for r in comps]
    return tp.Income(rent=D(2600), comps=rents, research_year=2027 if comps else None,
                     lease_rent=D(2500), expense_year=2026,
                     expenses={"taxes": D(7000), "insurance": D(2000), "repairs": D(1200)},
                     shared_expenses=shared)


def facts(rec=None, inc="default", sqft=1800):
    return tp.Facts(1, "12 Oak St, Plano, TX 75024",
                    "single-family house, 3 bedrooms, 2 bathrooms", sqft, 2027,
                    rec or record(), income() if inc == "default" else inc)


def sale(price, url="https://example.com/s", address=None, sqft=1800, sold="2026-11-01"):
    return {"address": address or f"{price} Pine St", "sqft": sqft, "year_built": 1996,
            "sale_price": price, "sale_date": sold, "source_url": url}


# $225 to $245/sq ft: median $235, which puts the subject at $423,000.
SALES = [sale(p) for p in (405000, 414000, 423000, 432000, 441000)]


def raw(sales=SALES, opinion=415000, cap=5.0, cap_url="https://example.com/cap"):
    return {"sales": sales, "cap_rate": cap, "cap_rate_source_url": cap_url,
            "market_notes": "Prices are flat (https://example.com/trend).",
            "opinion_of_value": opinion, "rationale": "Similar homes sit near $230/sq ft."}


def test_default_year_is_the_next_protest_season():
    assert tp.default_year(date(2026, 11, 17)) == 2027
    assert tp.default_year(date(2027, 4, 20)) == 2027
    assert tp.default_year(date(2027, 8, 1)) == 2028      # 2027's records are approved


def test_filing_deadline_follows_the_statute():
    assert tp.filing_deadline(2026, date(2026, 4, 15)) == date(2026, 5, 15)
    assert tp.filing_deadline(2027, None) == date(2027, 5, 17)     # May 15 is a Saturday
    assert tp.filing_deadline(2027, date(2027, 5, 3)) == date(2027, 6, 2)   # mailed late


def test_business_days_skip_the_weekend():
    assert tp.business_days_before(date(2027, 6, 14), 3) == date(2027, 6, 9)   # a Monday


def test_clean_sales_drops_bad_rows():
    kept, dropped = tp.clean_sales([
        sale(420000), sale(430000),
        sale(425000, url=""),                          # no source
        sale(425000, sqft=None),                       # no size
        sale(600000, sqft=2600),                       # much bigger house
        sale(425000, sold="2025-06-01"),               # before the year leading to Jan 1
        sale(2000000),                                 # outside 0.4x-2.5x of $450,000
        sale(420000),                                  # duplicate of the first
        {**sale(425000), "sale_price": "undisclosed"},
        sale(425000, sold=None),
    ], HOME, 2027, TODAY)
    assert [s.price for s in kept] == [D(420000), D(430000)]
    assert [why.split()[0] for _, why in dropped] == [
        "no", "no", "2,600", "sold", "price", "duplicate", "no", "no"]


def test_analyze_computes_every_figure_itself():
    p = tp.analyze(facts(), raw(), TODAY)
    assert p.equity_value == D(414000)                  # median $230/sq ft x 1,800
    assert p.sales_value == D(423000)                   # median $235/sq ft x 1,800
    assert p.income_value == D(420000)                  # $21,000 / 5%
    assert (p.opinion, p.deadline, p.deadline_is_official) == (D(415000), date(2027, 5, 17), True)
    assert p.flags == []


def test_analyze_flags_doubtful_results():
    rec = record(comps=SIMILAR[:2], value_year=2026, deadline=None)
    p = tp.analyze(facts(rec, income(comps=(), shared=2), sqft=2000),
                   raw(sales=SALES[:2], opinion=460000, cap_url=None), date(2026, 11, 17))
    assert p.deadline == date(2027, 5, 17) and not p.deadline_is_official
    assert p.cap_rate is None and p.income_value is None
    for text in ("has not published 2027 values", "uses its 2026 values as a preview",
                 "does not support a value below the district's $450,000",
                 "Only 2 similar homes", "Only 2 usable sales",
                 "district lists 1,800 sq ft; your records say 2,000",
                 "No saved rent research", "2026 is not over", "2 LLC-wide expense(s)",
                 "came without a source"):
        assert any(text in f for f in p.flags), text


def test_analyze_flags_an_opinion_below_all_the_evidence_and_a_passed_deadline():
    p = tp.analyze(facts(), raw(opinion=300000, cap=40), date(2027, 6, 1))
    assert any("below every figure" in f and "$414,000" in f for f in p.flags)
    assert any("filing deadline has passed" in f for f in p.flags)
    assert any("40.0% is not believable" in f for f in p.flags)


def test_analyze_needs_some_evidence():
    with pytest.raises(RuntimeError, match="nothing to argue from"):
        tp.analyze(facts(record(comps=[])), raw(sales=[sale(425000, url="")]), TODAY)


def test_report_contents():
    md = tp.render_report(tp.analyze(facts(), raw(), TODAY, hearing=date(2027, 6, 14)), TODAY)
    for text in (
        "# Property tax protest 2027: 12 Oak St, Plano, TX 75024",
        "**District's market value (2027):** $450,000; the year before, $430,000",
        "**Opinion of value:** $415,000, which is $35,000 (7.8%) below the district",
        "**File by:** May 17, 2027 (27 days left)",
        "| December 31, 2026 | The 20% yearly limit",
        "| **May 17, 2027** | Last day to file the protest. File online",
        "| May 31, 2027 | From this day the district must give you the evidence",
        "| June 4, 2027 | Last day to switch to a phone or video hearing",
        "| June 9, 2027 | Upload your evidence",
        "Owner, as the district lists it: OAK ST LLC",
        "Property: 12 OAK ST (district property ID 101)",
        "Opinion of value (optional): $415,000",
        "median of **$230.00/sq ft**, and 5 of them are valued lower",
        ("[Collin CAD Appraisal Data - 2027](https://data.texas.gov/d/abcd-1234), data as of "
        "April 19, 2027"),
        "| **101** | **12 OAK ST (this home)** | 1995 | 1,800 | no | $450,000 | $250.00 |",
        "5 sales since January 1, 2026 have a median of **$235.00/sq ft**",
        ("**Market rent:** $2,600.00/month (median of 3 comparable rents from the 2027 rent "
        "research). The current lease is $2,500.00"),
        ("**Operating expenses recorded for 2026:** $10,200 (insurance $2,000, repairs $1,200, "
        "taxes $7,000)"),
        "**Return at the district's value:** 4.7%",
        "the income supports **$420,000**",
    ):
        assert text in md, text
    assert md.count("[link](https://example.com/s)") == 5
    assert md.count("[link](https://example.com/r)") == 3
    assert "Check before using" not in md


def test_report_without_a_lower_value_or_a_hearing_date():
    md = tp.render_report(tp.analyze(facts(record(deadline=None), None),
                                     raw(opinion=455000, cap=None), TODAY), TODAY)
    assert "**Opinion of value:** $455,000. The evidence does not support a value below" in md
    assert "(27 days left; expected, the notice prints the exact date)" in md
    assert "| About April 15, 2027 | The district mails" in md
    assert "| Before your hearing | 14 days before" in md
    assert "No rent research and no lease are recorded" in md


def row(year, value="450000", propid=101, **extra):
    return {"propyear": str(year), "propid": str(propid), "propcategorycode": "A",
            "nbhdcode": "N0617", "situsconcatshort": "12 OAK ST", "imprvyearbuilt": "1995",
            "imprvmainarea": "1800", "imprvpoolflag": False, "currvalmarket": value,
            "prevvalmarket": "430000", "datadate": "2027-04-19T00:48:07.477",
            "ownername": "OAK ST LLC", **extra}


@pytest.fixture
def district(monkeypatch):
    """Fake the district's open data: `rows` maps a data set's short name to its subject row."""
    def serve(rows, notice=None):
        names = [collin_cad.DATA_PREFIX + n for n in ("2026", "Preliminary")] + [collin_cad.NOTICES]
        monkeypatch.setattr(collin_cad, "datasets",
                            lambda: {n: collin_cad.Dataset(n[-4:], n) for n in names})
        monkeypatch.setattr(collin_cad, "subject", lambda ds, street, city, cad_id:
                            rows.get(ds.name.removeprefix(collin_cad.DATA_PREFIX)))
        asked = []

        def neighborhood(ds, home, low, high):
            asked.append((ds.name, low, high))
            return [home, row(home["propyear"], "414000", 201),
                    row(home["propyear"], None, 202)]           # 202 has no value yet
        monkeypatch.setattr(collin_cad, "neighborhood", neighborhood)
        monkeypatch.setattr(collin_cad, "notice", lambda ds, prop_id, year: notice)
        return asked
    return serve


def test_load_record_uses_this_years_values_once_published(district):
    asked = district({"Preliminary": row(2027), "2026": row(2026, "430000")},
                     notice={"noticedate": "2027-04-15T00:00:00.000",
                             "protdeadlinedate": "2027-05-17T00:00:00.000"})
    r = tp.load_record("12 Oak St", "Plano", 2027, None)
    assert (r.value_year, r.home, r.owner) == (2027, HOME, "OAK ST LLC")
    assert asked == [(collin_cad.DATA_PREFIX + "Preliminary", 1440, 2160)]
    assert [c.prop_id for c in r.comps] == [201]       # not the subject, not the unvalued home
    assert (r.notice_date, r.deadline) == (date(2027, 4, 15), date(2027, 5, 17))
    assert (r.prior_value, r.data_date) == (D(430000), date(2027, 4, 19))


def test_load_record_previews_with_last_years_values(district):
    district({"Preliminary": row(2027, None), "2026": row(2026, "430000")})
    r = tp.load_record("12 Oak St", "Plano", 2027, None)
    assert (r.value_year, r.home.value, r.deadline) == (2026, D(430000), None)
    assert r.source.name == collin_cad.DATA_PREFIX + "2026"


def test_load_record_without_a_district_record(district):
    district({})
    with pytest.raises(ValueError, match="no 2027 or 2026 value for 12 Oak St"):
        tp.load_record("12 Oak St", "Plano", 2027, None)


def test_agents_get_the_protest_prompts_and_no_file_shell_or_db_tools(monkeypatch, tmp_path):
    seen = {}

    async def fake_query(prompt, options):
        seen.update(prompt=prompt, options=options)
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                            num_turns=3, session_id="s", structured_output={"ok": True})

    monkeypatch.setattr(rr, "query", fake_query)
    settings = Settings("m", "db", tmp_path, tmp_path, tmp_path)
    assert asyncio.run(tp.ask_agents(facts(), settings)) == {"ok": True}
    opts = seen["options"]
    assert set(opts.tools) == {"Agent", "WebSearch", "WebFetch"}
    assert opts.agents["sales-researcher"].tools == ["WebSearch", "WebFetch"]
    assert "Collin County" in opts.system_prompt
    assert opts.output_format["schema"] is tp.OUTPUT_SCHEMA
    for text in (("District record (2027 values): 1,800 sq ft, built 1995, no pool, market value "
                 "$450,000 ($250.00/sq ft)"),
                 "median of $230.00/sq ft, which puts this home at $414,000",
                 "net income $21,000 a year"):
        assert text in seen["prompt"], text


def test_job_options_are_parsed():
    params = runner.parse_params("tax-protest", {"property_id": "1", "hearing_date": "2027-06-14"})
    assert params == {"property_id": 1, "year": None, "cad_id": None,
                      "hearing_date": date(2027, 6, 14)}
