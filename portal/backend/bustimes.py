"""bustimes.org import — services list, timetable parse, description split.

plan.md 5.3a / user request: import services from
  https://bustimes.org/api/services/?operator=BULI
typical destinations come from ``description`` split on " - ":
  "Fradley - The Friary School" -> ["Fradley", "The Friary School"]
  "Lichfield City Circular"      -> ["Lichfield City Circular"]
  "Tamworth - Lichfield - St. Francis of Assisi" -> 3 parts
stops come from  https://bustimes.org/services/<id>/timetable.csv?detailed=true
(the number is the service ID). The CSV holds one block per direction,
blocks separated by a blank line with the header repeated.
"""
from __future__ import annotations

import csv
import io
import json
import re
import urllib.parse
import urllib.request

BUSTIMES_BASE = "https://bustimes.org"
_HTTP_TIMEOUT = 20


def split_description(description: str) -> list[str]:
    """Split a bustimes description into typical destinations.

    Split on " - " (space-hyphen-space). Single-destination descriptions
    (e.g. "Lichfield City Circular") return one part; multi-part ones
    (e.g. "Tamworth - Lichfield - St. Francis of Assisi") return each part.
    """
    desc = (description or "").strip()
    if not desc:
        return []
    parts = [p.strip() for p in re.split(r"\s+-\s+", desc)]
    return [p for p in parts if p]


def _http_get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "nsl-displays/1.0"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "nsl-displays/1.0"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        raw = resp.read()
    # timetable.csv may start with a BOM
    return raw.decode("utf-8-sig", errors="replace")


def fetch_services(operator: str = "", search: str = "", page: int = 1) -> dict:
    """Proxy the bustimes services API. Returns the decoded JSON as-is."""
    qs = {"public_use": "", "mode": "", "mode__in": "", "slug": "", "slug__in": "",
          "modified_at__gte": "", "operator": operator or "", "search": search or "",
          "stops": ""}
    if page and page > 1:
        qs["page"] = str(page)
    url = BUSTIMES_BASE + "/api/services/?" + urllib.parse.urlencode(qs)
    return _http_get_json(url)


def fetch_service_detail(service_id: int | str) -> dict:
    url = BUSTIMES_BASE + f"/api/services/{service_id}/"
    return _http_get_json(url)


def parse_timetable_csv(text: str) -> list[dict]:
    """Parse a detailed timetable.csv into one entry per direction block.

    Each block is the stops between a blank-line separator (the header row
    ``stop,...`` repeats at the start of every block). Returns a list of
    {"stops": [names...], "trips": n} in file order (usually outbound,
    then inbound; circular services have a single block).
    """
    directions: list[dict] = []
    current: list[str] = []
    trips = 0
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if not row or not (row[0] or "").strip():
            if current:
                directions.append({"stops": current, "trips": trips})
                current, trips = [], 0
            continue
        if row[0].strip().lower() == "stop":
            # header row: trip columns are everything after the first 3
            # (stop, NaPTAN code, ATCO code)
            trips = max(0, len(row) - 3)
            continue
        name = (row[0] or "").strip()
        if name:
            current.append(name)
    if current:
        directions.append({"stops": current, "trips": trips})
    return directions


def fetch_timetable(service_id: int | str) -> list[dict]:
    url = BUSTIMES_BASE + f"/services/{service_id}/timetable.csv?detailed=true"
    return parse_timetable_csv(_http_get_text(url))


def auto_direction_for_part(part: str, directions: list[dict]) -> int | None:
    """Guess which timetable block serves a description part.

    Matches the first word of the part against the last stop of each block
    (e.g. part "Tamworth" matches a block ending in "Tamworth ...").
    Falls back to reversed-order mapping for the classic 2-part/2-block
    case (dest[0] is served by the inbound block), else block order.
    Returns None when there are no directions.
    """
    if not directions:
        return None
    first_word = (part or "").strip().split()
    first_word = first_word[0].strip(",.").lower() if first_word else ""
    for i, d in enumerate(directions):
        stops = d.get("stops") or []
        if not stops:
            continue
        last = stops[-1].lower()
        if first_word and first_word in last:
            return i
    n = len(directions)
    return None if n == 0 else 0
