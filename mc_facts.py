"""
MikeCast — current-world facts that override the writers' stale training data.

LLM knowledge stops at a training cutoff, so a model can know today's date and
still call the sitting president "former President Trump". Two defenses live here:

  1. prompt_block() — injected into every writer prompt (crew agents + legacy
     mc_generate/mc_critic) stating who currently holds key offices and who held
     them last, plus a rule against titles the articles don't use.
  2. fix_stale_titles() — a deterministic post-generation pass over the HTML and
     both scripts that rewrites known-stale titles ("former President Trump" ->
     "President Trump", "President Biden" -> "former President Biden").

Where the officeholders come from, in order:
  a. Wikidata, live, once per run (SPARQL over "position held" statements). Each
     office is sanity-checked: real humans only, non-deprecated, exactly one
     holder with no end date, start date not in the future.
  b. The last good Wikidata result, saved to S3 (FACTS_S3_KEY) on every success —
     covers a Wikidata outage without falling back to hand-maintained data.
  c. FALLBACK below — hand-maintained, last resort.
Any office that falls through to (b) or (c), any Wikidata/FALLBACK disagreement,
and a FALLBACK older than FALLBACK_MAX_AGE_DAYS all log at WARNING
("Officeholder facts: ...") so drift gets noticed.

To track another office: add an Office (its Wikidata QID is the "position held"
item) and a FALLBACK entry.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache

from mc_config import TODAY_DISPLAY

logger = logging.getLogger(__name__)

WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
FACTS_S3_KEY = "facts/officeholders.json"
FALLBACK_REVIEWED = date(2026, 10, 6)
FALLBACK_MAX_AGE_DAYS = 90
S3_CACHE_MAX_AGE_DAYS = 14


@dataclass(frozen=True)
class Office:
    qid: str
    titles: tuple[str, ...]   # spoken titles, canonical first ("Fed Chair", "Chair", ...)
    role: str                 # long form for the prompt
    fix_former: bool = True   # add "former" to the predecessor's title? (no for popes — they die in office)

    @property
    def title(self) -> str:
        return self.titles[0]


OFFICES: tuple[Office, ...] = (
    Office("Q11696", ("President",), "President of the United States"),
    Office("Q11699", ("Vice President",), "Vice President of the United States"),
    Office("Q2347975", ("Governor", "Gov."), "Governor of New York"),
    Office("Q785304", ("Mayor",), "Mayor of New York City"),
    Office("Q2666591", ("Fed Chair", "Federal Reserve Chair", "Chair"), "Chair of the Federal Reserve"),
    Office("Q19546", ("Pope",), "Pope", fix_former=False),
)


@dataclass(frozen=True)
class Holder:
    name: str     # "Donald Trump", "Leo XIV"
    since: str    # ISO date the term started (current) or ended (former)

    @property
    def surname(self) -> str:
        toks = [t for t in self.name.split() if t.rstrip(".,") not in {"Jr", "Sr", "II", "III", "IV"}]
        # Regnal names ("Leo XIV") are matched whole.
        if len(toks) == 2 and re.fullmatch(r"[IVXLC]+", toks[1]):
            return self.name
        return toks[-1] if toks else self.name


# qid -> (current holder, previous holder). Reviewed against Wikidata on FALLBACK_REVIEWED.
FALLBACK: dict[str, tuple[Holder, Holder | None]] = {
    "Q11696":   (Holder("Donald Trump", "2025-01-20"), Holder("Joe Biden", "2025-01-20")),
    "Q11699":   (Holder("JD Vance", "2025-01-20"), Holder("Kamala Harris", "2025-01-20")),
    "Q2347975": (Holder("Kathy Hochul", "2021-08-24"), Holder("Andrew Cuomo", "2021-08-23")),
    "Q785304":  (Holder("Zohran Mamdani", "2026-01-01"), Holder("Eric Adams", "2025-12-31")),
    "Q2666591": (Holder("Kevin Warsh", "2026-05-22"), Holder("Jerome Powell", "2026-05-15")),
    "Q19546":   (Holder("Leo XIV", "2025-05-08"), Holder("Pope Francis", "2025-04-21")),
}


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _sparql_query() -> str:
    values = " ".join(f"wd:{o.qid}" for o in OFFICES)
    return f"""
SELECT ?office ?personLabel ?start ?end WHERE {{
  VALUES ?office {{ {values} }}
  ?person p:P39 ?st . ?st ps:P39 ?office ; wikibase:rank ?rank .
  ?person wdt:P31 wd:Q5 .
  ?st pq:P580 ?start . OPTIONAL {{ ?st pq:P582 ?end }}
  FILTER(?rank != wikibase:DeprecatedRank)
  FILTER(!BOUND(?end) || ?end > "2000-01-01"^^xsd:dateTime)
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,mul". }}
}}"""


def _fetch_wikidata_rows() -> list[dict]:
    from mc_utils import _safe_request
    resp = _safe_request(
        WIKIDATA_SPARQL_URL,
        params={"query": _sparql_query(), "format": "json"},
        # Wikidata's policy requires a descriptive UA with contact info.
        headers={"User-Agent": "MikeCast/1.0 (https://mikecast.io; daily news briefing)"},
        timeout=20,
    )
    if resp is None:
        raise RuntimeError("no response")
    resp.raise_for_status()
    return [
        {
            "office": b["office"]["value"].rsplit("/", 1)[-1],
            "name": b["personLabel"]["value"],
            "start": b["start"]["value"][:10],
            "end": b.get("end", {}).get("value", "")[:10] or None,
        }
        for b in resp.json()["results"]["bindings"]
    ]


def _resolve(rows: list[dict], today: str) -> dict[str, tuple[Holder, Holder | None]]:
    """Pick (current, previous) per office from Wikidata rows; omit offices that fail sanity checks."""
    out: dict[str, tuple[Holder, Holder | None]] = {}
    for o in OFFICES:
        mine = [r for r in rows if r["office"] == o.qid and not re.fullmatch(r"Q\d+", r["name"])]
        current = [r for r in mine if r["start"] <= today and (r["end"] is None or r["end"] > today)]
        # Collapse duplicate statements for the same person (re-elections are separate P39s).
        current_names = {r["name"] for r in current}
        if len(current_names) != 1:
            logger.warning("Officeholder facts: Wikidata has %d current holders for %s (%s) — not using it",
                           len(current_names), o.role, sorted(current_names) or "none")
            continue
        cur_name = current_names.pop()
        cur_start = min(r["start"] for r in current)
        ended = [r for r in mine if r["end"] and r["end"] <= today and r["name"] != cur_name]
        prev = max(ended, key=lambda r: r["end"], default=None)
        out[o.qid] = (
            Holder(cur_name, cur_start),
            Holder(prev["name"], prev["end"]) if prev else None,
        )
    return out


def _to_json(holders: dict[str, tuple[Holder, Holder | None]]) -> dict:
    return {qid: {"current": vars(c), "previous": vars(p) if p else None} for qid, (c, p) in holders.items()}


def _from_json(data: dict) -> dict[str, tuple[Holder, Holder | None]]:
    return {
        qid: (Holder(**v["current"]), Holder(**v["previous"]) if v.get("previous") else None)
        for qid, v in data.items()
    }


def _load_s3_cache() -> tuple[dict[str, tuple[Holder, Holder | None]], str] | None:
    from mc_config import S3_BUCKET
    if not S3_BUCKET:
        return None
    from mc_utils import s3_load_json
    data = s3_load_json(S3_BUCKET, FACTS_S3_KEY)
    if not data:
        return None
    return _from_json(data["holders"]), data["fetched_at"]


def _save_s3_cache(holders: dict[str, tuple[Holder, Holder | None]]) -> None:
    from mc_config import S3_BUCKET
    if not S3_BUCKET:
        return
    from mc_utils import s3_save_json
    s3_save_json(S3_BUCKET, FACTS_S3_KEY, {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "holders": _to_json(holders),
    }, indent=2)


@lru_cache(maxsize=1)
def get_officeholders() -> dict[str, tuple[Holder, Holder | None]]:
    """(current, previous) holder per office QID — Wikidata, else S3 cache, else FALLBACK. Never raises."""
    today = date.today()
    resolved: dict[str, tuple[Holder, Holder | None]] = {}

    if os.environ.get("MIKECAST_FACTS_OFFLINE") != "1":
        try:
            resolved = _resolve(_fetch_wikidata_rows(), today.isoformat())
            logger.info("Officeholder facts: %d/%d offices from Wikidata", len(resolved), len(OFFICES))
            if len(resolved) == len(OFFICES):
                try:
                    _save_s3_cache(resolved)
                except Exception as exc:
                    logger.warning("Officeholder facts: could not save S3 cache (non-fatal): %s", exc)
        except Exception as exc:
            logger.warning("Officeholder facts: Wikidata lookup failed (%s) — using fallbacks", exc)

    missing = [o for o in OFFICES if o.qid not in resolved]
    if missing:
        try:
            cached = _load_s3_cache()
        except Exception as exc:
            logger.warning("Officeholder facts: could not read S3 cache (non-fatal): %s", exc)
            cached = None
        if cached:
            holders, fetched_at = cached
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)).days
            if age > S3_CACHE_MAX_AGE_DAYS:
                logger.warning("Officeholder facts: S3 cache is %d days old — Wikidata has been failing", age)
            for o in missing:
                if o.qid in holders:
                    resolved[o.qid] = holders[o.qid]
                    logger.warning("Officeholder facts: %s from S3 cache (%s)", o.role, fetched_at[:10])
        for o in OFFICES:
            if o.qid not in resolved:
                resolved[o.qid] = FALLBACK[o.qid]
                logger.warning("Officeholder facts: %s from hardcoded FALLBACK", o.role)

    fallback_age = (today - FALLBACK_REVIEWED).days
    for o in OFFICES:
        cur, fb_cur = resolved[o.qid][0].name, FALLBACK[o.qid][0].name
        if cur != fb_cur:
            logger.warning(
                "Officeholder facts: live data says %s is %s but mc_facts.FALLBACK says %s — "
                "update FALLBACK (and FALLBACK_REVIEWED)", cur, o.role, fb_cur)
    if fallback_age > FALLBACK_MAX_AGE_DAYS:
        logger.warning(
            "Officeholder facts: mc_facts.FALLBACK last reviewed %s (%d days ago) — re-check it against "
            "Wikidata and bump FALLBACK_REVIEWED", FALLBACK_REVIEWED, fallback_age)
    return resolved


# ---------------------------------------------------------------------------
# Prompt text
# ---------------------------------------------------------------------------

def _long_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%B} {d.day}, {d.year}"


TITLE_RULE = (
    "TITLE RULE: Give people the title the provided articles give them. For anyone not listed "
    "in CURRENT FACTS, do not rely on memory for their current title (president, CEO, coach, "
    "minister, chair, etc.) — use the article's wording, or just their name. Never add "
    "'former', 'ex-', or '-elect' to someone's title unless an article does."
)


def facts_lines() -> str:
    """Bullet list of current/previous officeholders (no instructions) — also used as fact-checker context."""
    holders = get_officeholders()
    current, former = [], []
    for o in OFFICES:
        cur, prev = holders[o.qid]
        current.append(
            f"- {cur.name} is the current {o.role} (since {_long_date(cur.since)}). "
            f"Say \"{o.title} {cur.surname}\" — never \"former\".")
        if prev and o.fix_former:
            former.append(
                f"- {prev.name} is the former {o.role} (left office {_long_date(prev.since)}). "
                f"If mentioned, say \"former {o.title} {prev.surname}\".")
        elif prev:
            former.append(f"- {prev.name} was the previous {o.role} (until {_long_date(prev.since)}).")
    return "\n".join(current + former)


def prompt_block() -> str:
    return (
        f"CURRENT FACTS (today is {TODAY_DISPLAY}). Your training data predates today and is out "
        "of date on who holds office. These facts override anything you remember:\n"
        + facts_lines()
        + f"\n\n{TITLE_RULE}"
    )


# ---------------------------------------------------------------------------
# Post-generation fixer
# ---------------------------------------------------------------------------

_GIVEN = r"(?:[A-Z][A-Za-z.'\-]*\s+){0,2}"   # optional given names / initials: "Donald J. "


def _at_sentence_start(text: str, pos: int) -> bool:
    before = text[:pos].rstrip(" \t")
    return not before or before[-1] in '.!?:\n"“]>'


def _inside_longer_title(m: re.Match, titles: tuple[str, ...]) -> bool:
    """True when the matched title is the tail of a longer one ("Chair" in "Fed Chair").
    The regex already tried the longer title at its own start and a qualifier there
    ("former Fed Chair Powell") rejected it — matching the tail would bypass that."""
    title, before = m.group(1), m.string[:m.start()]
    return any(t != title and t.endswith(" " + title) and before.endswith(t[: -len(title)])
               for t in titles)


def fix_stale_titles(text: str) -> tuple[str, list[str]]:
    """Rewrite known-stale titles. Returns (fixed_text, human-readable change list)."""
    if not text:
        return text, []
    changes: list[str] = []
    holders = get_officeholders()

    def _cap(new: str, m: re.Match) -> str:
        return new[0].upper() + new[1:] if _at_sentence_start(m.string, m.start()) else new

    for o in OFFICES:
        cur, prev = holders[o.qid]
        titles = "|".join(re.escape(t) for t in o.titles)
        # Writers sometimes lowercase a title after "former"; accept that there only.
        titles_any_case = "|".join(re.escape(v) for t in o.titles for v in (t, t.lower()))

        # Current holder: drop "former"/"ex-"/"-elect".
        cur_name = rf"{_GIVEN}{re.escape(cur.surname)}\b"
        for pat in (
            re.compile(rf"\b(?:[Ff]ormer|[Ee]x-)\s*((?:U\.S\.\s+)?(?:{titles_any_case}))\s+({cur_name})"),
            re.compile(rf"\b({titles})-elect\s+({cur_name})"),
        ):
            def _sub_current(m: re.Match) -> str:
                new = _cap(f"{m.group(1)} {m.group(2)}", m)
                changes.append(f"{m.group(0)!r} -> {new!r}")
                return new
            text = pat.sub(_sub_current, text)

        if not prev:
            continue
        prev_surname = re.escape(prev.surname)
        # Previous holder titled as if still in office. Lookbehinds are fixed-width,
        # hence one per qualifier; "Vice " keeps "Vice President X" / "Vice Chair X"
        # (different offices) from matching.
        pat = re.compile(
            r"(?<![Ff]ormer )(?<!ex-)(?<!then-)(?<!Then-)(?<!Vice )(?<!U\.S\. )(?<!late )"
            rf"\b({titles})\s+({_GIVEN}{prev_surname})\b"
        )
        if o.fix_former:
            def _sub_former(m: re.Match, o: Office = o) -> str:
                if _inside_longer_title(m, o.titles):
                    return m.group(0)
                new = _cap(f"former {m.group(1)} {m.group(2)}", m)
                changes.append(f"{m.group(0)!r} -> {new!r}")
                return new
            text = pat.sub(_sub_former, text)
        else:
            changes.extend(f"FLAGGED (not changed): {m.group(0)!r}" for m in pat.finditer(text)
                           if not _inside_longer_title(m, o.titles))

    return text, changes


def fix_stale_titles_in_outputs(**outputs: str) -> dict[str, str]:
    """Run fix_stale_titles over each named output, logging every change. Never raises."""
    fixed: dict[str, str] = {}
    for label, text in outputs.items():
        try:
            new_text, changes = fix_stale_titles(text)
        except Exception as exc:  # a regex bug must never kill the daily run
            logger.warning("Stale-title check failed on %s (non-fatal): %s", label, exc)
            fixed[label] = text
            continue
        for c in changes:
            logger.warning("Stale title in %s: %s", label, c)
        fixed[label] = new_text
    return fixed
