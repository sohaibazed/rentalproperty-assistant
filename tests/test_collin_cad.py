"""Reading the district's open data, with the HTTP call faked."""

import urllib.request

import pytest

from rental_agent import collin_cad

SET = collin_cad.Dataset("abcd-1234", "Collin CAD Appraisal Data - 2027")
OAK = {"propid": "1", "situsconcatshort": "12 OAK ST", "situsstreetname": "OAK"}
OAK_HILL = {"propid": "2", "situsconcatshort": "12 OAK HILL DR", "situsstreetname": "OAK HILL"}


@pytest.fixture
def fetched(monkeypatch):
    """Make every request return `rows`; record what was asked."""
    calls = []

    def serve(rows):
        def fake_get(url, params):
            calls.append((url, params))
            return rows
        monkeypatch.setattr(collin_cad, "_get", fake_get)
        return calls
    return serve


def test_subject_is_found_by_street_address(fetched):
    calls = fetched([OAK, OAK_HILL])
    assert collin_cad.subject(SET, "12 Oak St.", "Plano", None) == OAK
    url, params = calls[0]
    assert url == "https://data.texas.gov/resource/abcd-1234.json"
    assert params["$where"] == "situsbldgnum = '12' AND situscity = 'PLANO'"
    assert "ownername" in params["$select"]


def test_subject_matches_a_street_suffix_spelled_differently(fetched):
    fetched([OAK, OAK_HILL])
    assert collin_cad.subject(SET, "12 Oak Street", "Plano", None) == OAK


def test_subject_by_district_property_id(fetched):
    calls = fetched([OAK])
    assert collin_cad.subject(SET, "anything", "Plano", 101) == OAK
    assert calls[0][1]["$where"] == "propid = 101"


def test_subject_missing_or_ambiguous(fetched):
    fetched([])
    assert collin_cad.subject(SET, "12 Oak St", "Plano", None) is None
    fetched([OAK, {**OAK, "propid": "3"}])        # e.g. two units at one address
    with pytest.raises(ValueError, match="property IDs 1, 3.*pass the district property ID"):
        collin_cad.subject(SET, "12 Oak St", "Plano", None)


def test_neighborhood_asks_for_the_same_kind_and_size_and_quotes_text(fetched):
    calls = fetched([OAK])
    home = {"nbhdcode": "N'0617", "propcategorycode": "A"}
    assert collin_cad.neighborhood(SET, home, 1440, 2160) == [OAK]
    assert calls[0][1]["$where"] == ("nbhdcode = 'N''0617' AND propcategorycode = 'A' "
                                     "AND imprvmainarea >= 1440 AND imprvmainarea <= 2160")
    assert "ownername" not in calls[0][1]["$select"]     # neighbors' names are never read


def test_notice_for_a_year(fetched):
    calls = fetched([{"noticedate": "2027-04-15T00:00:00.000"}])
    assert collin_cad.notice(SET, 101, 2027) == {"noticedate": "2027-04-15T00:00:00.000"}
    assert calls[0][1]["$where"] == "propid = 101 AND noticeyear = 2027"
    fetched([])
    assert collin_cad.notice(SET, 101, 2027) is None


def test_datasets_are_listed_by_name(fetched):
    calls = fetched({"results": [{"resource": {"id": "abcd-1234", "name": SET.name}}]})
    assert collin_cad.datasets() == {SET.name: SET}
    assert calls[0][1]["provenance"] == "official"       # only the district's own sets
    assert SET.url == "https://data.texas.gov/d/abcd-1234"


def test_a_failed_request_is_a_clear_error(monkeypatch):
    def refuse(request, **options):
        raise OSError("connection refused")
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(RuntimeError, match="Could not read Collin CAD open data.*refused"):
        collin_cad.datasets()
