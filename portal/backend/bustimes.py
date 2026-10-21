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

# Short in-memory cache: the UI fetches detail+timetable for the preview and
# the import refetches seconds later. bustimes.org can take 10s+ per file, so
# without this every import pays the download twice with no feedback.
_CACHE_TTL_S = 300
_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, fn):
    import time as _time
    now = _time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL_S:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    # bounded: drop the oldest entries past 200
    if len(_cache) > 200:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[: len(_cache) - 200]:
            del _cache[k]
    return val


def cache_clear() -> None:
    """Forget cached bustimes responses (tests)."""
    _cache.clear()


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
    return _cached(f"detail:{service_id}", lambda: _http_get_json(url))


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
    return _cached(f"timetable:{service_id}", lambda: parse_timetable_csv(_http_get_text(url)))


# Position words bustimes puts after the comma or in trailing parens that
# describe where the stop is rather than name it
# ("Tutbury, adj St Christopher Church", "Shobnall Street (opp)").
# Abbreviations count in any case; full words only when lowercase, so a
# street actually called "By ..." is never mangled.
_STOP_ABBR = {"adj", "opp", "nr", "n/r", "o/s", "os", "cnr", "jct", "jnc"}
_STOP_WORDS = {"adjacent", "opposite", "outside", "near", "by", "in", "at", "on",
              "before", "after", "beyond", "behind", "beside", "for", "towards"}
_STOP_TWO_WORD = {"next to"}


def _split_qualifier(tail: str) -> str | None:
    """If `tail` starts with a position qualifier, return the remainder
    ("" when it is a bare qualifier like "(opp)"). Else None."""
    words = tail.split()
    if not words:
        return None
    low = words[0].lower().rstrip(".")
    if low in _STOP_ABBR:
        return tail[len(words[0]):].strip()
    if len(words) >= 2:
        two = (words[0] + " " + words[1]).lower()
        if two in _STOP_TWO_WORD and (words[0] + " " + words[1]) == two:
            return tail[len(words[0]) + 1 + len(words[1]):].strip()
    if words[0] == words[0].lower() and low in _STOP_WORDS:
        return tail[len(words[0]):].strip()
    return None


def _strip_trailing_qualifier_paren(s: str) -> str:
    """Drop a trailing '(opp)' / '(adj)' style parenthetical.

    "Shobnall Street (opp)" -> "Shobnall Street", but informative ones like
    "Lichfield Bus Station (Stand 2)" are kept. Repeats for stacked parens.
    """
    while s.endswith(")"):
        open_i = s.rfind("(")
        if open_i <= 0:
            break
        inner = s[open_i + 1:-1].strip()
        if _split_qualifier(inner) is None:
            break
        s = s[:open_i].rstrip()
        if not s:
            break
    return s


def spoken_stop_name(full_name: str) -> str:
    """Short spoken form of a timetable stop for audio clips.

    Strips the "Place, qualifier" prefix ("Tutbury, adj St Christopher
    Church" -> "St Christopher Church") and ensures terminal punctuation
    ("St Christopher Church" -> "St Christopher Church."), which gives the
    TTS sentence-final prosody instead of a clipped fragment. Names without
    a comma are kept whole. The stop record itself keeps the full name —
    only the clip name and spoken audio use this form.
    """
    s = (full_name or "").strip()
    if "," in s:
        head, _, tail = s.partition(",")
        head, tail = head.strip(), tail.strip()
        if tail:
            rest = _split_qualifier(tail)
            if rest is None:
                s = tail  # no qualifier: keep everything after the comma
            else:
                s = rest or head  # bare qualifier ("Foo, opp"): fall back to the head
            if not s:
                s = head or tail
    s = _strip_trailing_qualifier_paren(s.strip())
    # "PH" (public house) would be read out letter by letter — say "pub".
    # Whole word only, so "Phoenix" is untouched.
    s = re.sub(r"\bPH\b", "pub", s)
    if s and s[-1] not in ".!?":
        s += "."
    return s


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
