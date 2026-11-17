import asyncio
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest
from claude_agent_sdk import ResultMessage

from rental_agent.config import Settings
from rental_agent.jobs import rent_research as rr

FACTS = rr.Facts(1, "12 Oak St, Plano, TX 75024", "single-family house", D(3), D(2), 1800,
                 D(2500), date(2026, 12, 31), 2027, [(2026, D(2500))])


def comp(rent, url="https://example.com/x", address=None, sqft=1800):
    return {"address": address or f"{rent} Elm St", "beds": 3, "baths": 2, "sqft": sqft,
            "rent": rent, "date": "2026-10-01", "status": "listed", "source_url": url}


def raw(comps, rec=2600, low=2500, high=2700):
    return {"comps": comps, "market_notes": "Rents up ~3% (https://example.com/trend).",
            "recommended_rent": rec, "range_low": low, "range_high": high,
            "rationale": "Comps cluster near $2,600."}


def test_clean_comps_drops_bad_rows():
    kept, dropped = rr.clean_comps([
        comp(2550), comp(2650),
        comp(2600, url=""),                       # no source
        comp(9000),                               # outside 0.4x-2.5x of $2,500
        comp(2550),                               # duplicate of first
        {**comp(2700), "rent": "call for price"}, # not a number
    ], D(2500))
    assert [c.rent for c in kept] == [D(2550), D(2650)]
    assert [why.split()[0] for _, why in dropped] == ["no", "rent", "duplicate", "rent"]


def test_analyze_computes_stats_itself():
    r = rr.analyze(FACTS, raw([comp(x) for x in (2400, 2500, 2600, 2700, 2800)]))
    assert (r.median, r.p25, r.p75) == (D(2600), D(2500), D(2700))
    assert r.per_sqft_estimate == D(2600)    # all comps 1800 sq ft, same as subject
    assert r.recommended == D(2600)
    assert r.flags == []


def test_analyze_flags_doubtful_results():
    r = rr.analyze(FACTS, raw([comp(2400), comp(2500)], rec=3000, low=2500, high=2700))
    assert any("Only 2 usable comps" in f for f in r.flags)
    assert any("outside the comps' range" in f for f in r.flags)
    assert any("own range" in f for f in r.flags)


def test_analyze_needs_at_least_one_comp():
    with pytest.raises(RuntimeError, match="No usable comps"):
        rr.analyze(FACTS, raw([comp(2600, url="")]))


def test_report_contents():
    r = rr.analyze(FACTS, raw([comp(x) for x in (2400, 2500, 2600, 2700, 2800)]))
    md = rr.render_report(r, date(2026, 11, 16))
    assert "**Recommended rent:** $2,600.00/month" in md
    assert "change $100.00 (+4.0%)" in md
    assert "25.0 months to earn back one vacant month" in md
    assert "November 16, 2026" in md
    assert md.count("[link](https://example.com/x)") == 5


def test_agents_have_no_file_shell_or_db_tools(tmp_path):
    opts = rr.agent_options(Settings("m", "db", tmp_path, tmp_path, tmp_path,
                                               claude_env={"ANTHROPIC_API_KEY": "k"}))
    assert set(opts.tools) == {"Agent", "WebSearch", "WebFetch"}
    assert opts.agents["comps-researcher"].tools == ["WebSearch", "WebFetch"]
    assert opts.setting_sources == [] and opts.mcp_servers in ({}, None)
    assert opts.output_format["schema"] is rr.OUTPUT_SCHEMA
    assert opts.env == {"ANTHROPIC_API_KEY": "k"}   # credentials reach the Claude process


def test_ask_agents_returns_structured_output(monkeypatch, tmp_path):
    async def fake_query(prompt, options):
        assert "Bedrooms: 3" in prompt
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                            num_turns=3, session_id="s", structured_output={"ok": True})

    monkeypatch.setattr(rr, "query", fake_query)
    settings = Settings("m", "db", tmp_path, tmp_path, tmp_path)
    assert asyncio.run(rr.ask_agents(FACTS, settings)) == {"ok": True}


def test_ask_agents_raises_on_error(monkeypatch, tmp_path):
    async def fake_query(prompt, options):
        yield ResultMessage(subtype="error_max_turns", duration_ms=1, duration_api_ms=1,
                            is_error=True, num_turns=10, session_id="s")

    monkeypatch.setattr(rr, "query", fake_query)
    settings = Settings("m", "db", Path(tmp_path), tmp_path, tmp_path)
    with pytest.raises(RuntimeError, match="error_max_turns"):
        asyncio.run(rr.ask_agents(FACTS, settings))
