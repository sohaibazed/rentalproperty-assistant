"""Collin Central Appraisal District's public appraisal records, read from its open data.

The district's property search refuses automated requests. It publishes the same records on
data.texas.gov: one "Collin CAD Appraisal Data" set per certified year, a "Preliminary" set
for the year in progress, and the notices it mailed. This module finds those sets by name,
so a new year needs no code change. Read-only; needs no account or key.
"""

import json
import re
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

import certifi

CATALOG = "https://api.us.socrata.com/api/catalog/v1"
DATA_PREFIX = "Collin CAD Appraisal Data - "       # followed by a year, or "Preliminary"
NOTICES = "Collin CAD Appraisal Notices"
TIMEOUT_SECONDS = 60
MAX_ROWS = 5000

# Only what a protest needs. The owner's name is read for the subject property alone.
FIELDS = ("propyear, propid, propcategorycode, nbhdcode, situsbldgnum, situsstreetname, "
          "situsconcatshort, imprvyearbuilt, imprvmainarea, imprvpoolflag, currvalmarket, "
          "noticevalmarket, prevvalmarket, datadate")

# Python installed by uv on a Mac has no certificate authorities of its own, so use certifi's.
_TLS = ssl.create_default_context(cafile=certifi.where())

Row = dict[str, Any]


@dataclass(frozen=True)
class Dataset:
    id: str
    name: str

    @property
    def url(self) -> str:
        return f"https://data.texas.gov/d/{self.id}"


def _get(url: str, params: dict[str, str]) -> Any:
    request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}",
                                     headers={"User-Agent": "rental-agent"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS,
                                    context=_TLS) as response:
            return json.load(response)
    except (OSError, ValueError) as exc:    # network or HTTP error, or a reply that isn't JSON
        raise RuntimeError(f"Could not read Collin CAD open data from {url}: {exc}") from exc


def _rows(dataset: Dataset, where: str, fields: str = FIELDS) -> list[Row]:
    return _get(f"https://data.texas.gov/resource/{dataset.id}.json",
                {"$select": fields, "$where": where, "$limit": str(MAX_ROWS)})


def _quote(text: str) -> str:
    """A text value for a $where clause."""
    return "'" + text.replace("'", "''") + "'"


def _normal(address: str) -> str:
    """'8705 Empire Blvd.' and '8705 EMPIRE  BLVD' compare equal."""
    return " ".join(re.sub(r"[^A-Z0-9 ]", "", address.upper()).split())


def datasets() -> dict[str, Dataset]:
    """The district's official data sets, by name."""
    found = _get(CATALOG, {"domains": "data.texas.gov", "q": "Collin CAD Appraisal",
                           "only": "datasets", "provenance": "official", "limit": "100"})
    sets = [Dataset(r["resource"]["id"], r["resource"]["name"]) for r in found["results"]]
    return {s.name: s for s in sets}


def subject(dataset: Dataset, street_address: str, city: str, cad_id: int | None) -> Row | None:
    """The district's row for one property: by its district property ID if given, else by
    street address. None if the set has no such property. Raises ValueError if the address
    matches more than one."""
    fields = FIELDS + ", ownername"
    if cad_id is not None:
        found = _rows(dataset, f"propid = {int(cad_id)}", fields)
    else:
        target = _normal(street_address)
        number = target.split()[0] if target else ""
        same_number = _rows(dataset, f"situsbldgnum = {_quote(number)} "
                                     f"AND situscity = {_quote(city.upper())}", fields)
        found = [r for r in same_number if _normal(r.get("situsconcatshort", "")) == target]
        if not found:   # "Boulevard" here, "BLVD" there: match the street name as whole words
            found = [r for r in same_number
                     if f" {r.get('situsstreetname', '')} " in f" {target} "]
    if len(found) > 1:
        ids = ", ".join(str(r["propid"]) for r in found)
        raise ValueError(f"{len(found)} district records match {street_address} "
                         f"(property IDs {ids}); pass the district property ID")
    return found[0] if found else None


def neighborhood(dataset: Dataset, home: Row, min_sqft: int, max_sqft: int) -> list[Row]:
    """Homes of the same kind in the same district neighborhood as `home`, within a size
    range. Includes `home` itself."""
    return _rows(dataset, f"nbhdcode = {_quote(home['nbhdcode'])} "
                          f"AND propcategorycode = {_quote(home['propcategorycode'])} "
                          f"AND imprvmainarea >= {int(min_sqft)} "
                          f"AND imprvmainarea <= {int(max_sqft)}")


def notice(dataset: Dataset, prop_id: int, year: int) -> Row | None:
    """When the district mailed `year`'s notice for a property and the protest deadline
    printed on it. None until that notice is mailed."""
    found = _rows(dataset, f"propid = {int(prop_id)} AND noticeyear = {int(year)}",
                  "noticedate, protdeadlinedate")
    return found[0] if found else None
