"""Deterministic answer verification (G14, G15, G16; gaps F-11, F-12, F-13).

Rules:
1  every cited evidence ID was returned by a tool in this session
2  every device or employee ID in the answer (structured values AND free text) came from a tool
3  every number in the answer is labelled once (date, measurement, count or plain value) and checked
   by the rule for that label:
     date         2026-05-22, May 2026      must appear in a tool result
     measurement  16 GB, 2.6%, 30 days      must match a tool value or threshold
     count        5 devices                 must be a whole number in the evidence the claim cites
     plain value  cycle count 993           must match a tool value or threshold
   Nothing is inferred from field names except the few that can only mean "how many results".
4  thresholds the cited tool results used are stated in definitions_used
5  no factual claim without evidence; "no matches" must cite a zero-result receipt
6  completeness: a claim built on a device list names every device or states the count
7  no trend from too few snapshots: when the history a claim cites has fewer snapshots than
   min_points_for_trend, the answer may state the recorded values and that a trend cannot be
   assessed, but not a trend or the absence of one; a question about a trend that rests only on
   such a history must be answered as "insufficient_data"
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fleet_copilot.agent.models import FinalAnswer
from fleet_copilot.session import whole_numbers

if TYPE_CHECKING:
    from fleet_copilot.toolkit import Toolkit

DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:T[\d:]+Z?)?\b")
EVID_RE = re.compile(r"\b(?:INS-[\w-]+|Q-\d+-\d+|PRP-\d+|[A-Za-z0-9-]+@[\dT:-]+(?:[#$][\w-]+)?)")
DEVICE_RE = re.compile(r"\b(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{7,}\b")
EMP_RE = re.compile(r"\bemp-[a-z0-9-]+\b")
NUM_RE = re.compile(r"(?<![\w.])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\w.])|(?<![\w.])-?\d+(?:\.\d+)?(?![\w])")
ALWAYS_OK_NUMBERS = {0.0, 1.0}
# Field names that can only mean "how many results". Anything else (cycle_count, fail_count) is a plain value.
COUNT_KEY_RE = re.compile(r"^(count|total|n|total_count|result_count|(num|number|count|total)_(of_)?(devices|laptops|machines|results|matches)"
                          r"|(device|laptop|machine|result|match)e?s?_count)$")
COUNT_TEXT_RE = re.compile(r"\b(\d+)\s+(?:of\s+(?:the\s+|your\s+)?\d+\s+)?(?:\w+\s+)?(?:devices?|laptops?|machines?|computers?|endpoints?)\b", re.I)
# A number followed by a unit is a measurement ("16 GB device" is one device, not sixteen).
UNIT_RE = re.compile(r"\s?-?\s?(?:%|percent\b|(?:[KMGT]i?B|mAh|Wh)\b|(?:days?|hours?|hrs?|minutes?|mins?|seconds?|secs?|weeks?|months?|years?)\b)", re.I)
MONTHS = {m: i for i, names in enumerate((("January", "Jan"), ("February", "Feb"), ("March", "Mar"), ("April", "Apr"), ("May",),
                                          ("June", "Jun"), ("July", "Jul"), ("August", "Aug"), ("September", "Sep", "Sept"),
                                          ("October", "Oct"), ("November", "Nov"), ("December", "Dec")), 1) for m in names}
# Written dates. Month names are matched with their capital letter only ("may" is a verb).
#   one day        May 22 · May 22nd · 22 May · 22nd of May · May 22, 2026 · May 2026
#   several days   June 10-12 · June 10 to 12 · June 10 and 11 · June 10, 11 and 12 · 10-12 June 2026
# Every day written is a date of its own and must appear in a tool result.
MONTH_RE = re.compile(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b\.?")
_NOUNS = (r"devices?|laptops?|machines?|computers?|endpoints?|checks?|snapshots?|points?|failures?|fails?|passes|times|items?"
          r"|apps?|applications?|results?|records?|entries|changes?|cycles?|proposals?|tickets?|employees?|users?|volumes?")
# A day is one or two digits, not part of a longer number, a measurement ("May 16 GB") or a count ("May 12 devices").
_DAY = (r"(\d{1,2})(?:st|nd|rd|th)?(?![\d\w]|[.:/]\d|\s?(?:%|percent\b|(?:[KMGT]i?B|mAh|Wh)\b)"
        r"|\s+(?:days?|hours?|hrs?|minutes?|mins?|seconds?|secs?|weeks?|months?|years?|" + _NOUNS + r")\b)")
_SEP = r"(\s*,\s*and\s+|\s+and\s+|\s*&\s*|\s*[-–—]\s*|\s+(?:to|through|thru|until|till)\s+|\s*,\s*)"
DAY_AFTER_RE = re.compile(r"\s+" + _DAY)
NEXT_DAY_RE = re.compile(_SEP + _DAY)
DAYS_BEFORE_RE = re.compile(r"(?<![\w.,/:@#$-])\d{1,2}(?:st|nd|rd|th)?(?:" + _SEP + r"\d{1,2}(?:st|nd|rd|th)?)*\s+(?:of\s+)?\Z")
DAY_IN_LIST_RE = re.compile(_SEP + r"?(\d{1,2})(?:st|nd|rd|th)?")
YEAR_AFTER_RE = re.compile(r",?\s+((?:19|20)\d{2})\b(?![.,]\d)")
LIST_END_RE = re.compile(r"\s*(?:\Z|[.;:)\]])")


def _is_comma(sep: str) -> bool:
    return sep.strip() == ","


def written_dates(text: str) -> tuple[list[tuple[str, str]], str]:
    """Written dates in the text, and the text with them blanked out.

    Each date is (as written, the text that must appear in a tool result): "2026-05-22" with a year,
    "-05-22" without one, "2026-05-" for a month and year."""
    dates: list[tuple[str, str]] = []
    spans: list[tuple[int, int]] = []
    for m in MONTH_RE.finditer(text):
        month = MONTHS[m.group(1)]
        start, end = m.start(), m.end()
        days: list[int] = []
        before = DAYS_BEFORE_RE.search(text, max(0, start - 60), start)
        if before:                                           # "10-12 June", "22nd of May"
            items = [(sep or "", int(d)) for sep, d in DAY_IN_LIST_RE.findall(before.group())]
            keep = len(items) - 1                            # the day next to the month always counts
            if not all(_is_comma(sep) for sep, _ in items[1:]):  # "3, 10 June" keeps only the 10 ...
                while keep > 0 and items[keep - 1][1] < items[keep][1]:
                    keep -= 1                                # ... "10, 11 and 12 June" keeps all three
            days = [d for _, d in items[keep:]]
            lead = DAY_IN_LIST_RE.finditer(before.group())
            start = before.start() + [x.start(2) for x in lead][keep]
        else:                                                # "June 10", "June 10, 11 and 12"
            first = DAY_AFTER_RE.match(text, end)
            if first:
                items = [("", int(first.group(1)))]
                ends = [first.end()]
                while (nxt := NEXT_DAY_RE.match(text, ends[-1])) and int(nxt.group(2)) > items[-1][1]:
                    items.append((nxt.group(1), int(nxt.group(2))))
                    ends.append(nxt.end())
                # a last day joined by a bare comma is a day only if the list ends there: "May 3, 5 checks failed"
                while len(items) > 1 and _is_comma(items[-1][0]) and not (
                        LIST_END_RE.match(text, ends[-1]) or YEAR_AFTER_RE.match(text, ends[-1])):
                    items.pop()
                    ends.pop()
                days, end = [d for _, d in items], ends[-1]
        days = [d for d in days if 1 <= d <= 31]
        year = YEAR_AFTER_RE.match(text, end)
        if year:
            end = year.end()
        if not days and not year:
            continue                                         # a month name alone states no number
        shown = text[start:end].strip()
        if not days:
            dates.append((shown, f"{year.group(1)}-{month:02d}-"))
        for d in days:
            dates.append((shown, f"{year.group(1)}-{month:02d}-{d:02d}" if year else f"-{month:02d}-{d:02d}"))
        spans.append((start, end))
    for a, b in spans:
        text = text[:a] + " " * (b - a) + text[b:]
    return dates, text


# A bare year ("during 2026"). Not a year: part of a larger number, a measurement, or a device count.
YEAR_RE = re.compile(r"(?<![\w.,-])((?:19|20)\d{2})(?![\w%-]|[.,]\d|\s?(?:[KMGT]i?B|mAh|Wh)\b"
                     r"|\s+(?:\w+\s+)?(?:devices?|laptops?|machines?|computers?|endpoints?)\b)")


# Words that state a trend (or the absence of one). Past-tense facts ("fell from 50% to 45%") are not in the list.
TREND_RE = re.compile(r"\b(?:trend\w*|rising|falling|declining|increasing|decreasing|growing|shrinking|worsening|improving"
                      r"|deteriorating|degrading|stable|steady)\b", re.I)
# A sentence that says a trend cannot be assessed may use those words.
CANNOT_TELL_RE = re.compile(r"insufficient|not enough|too few|fewer than|less than|below the|cannot|can't|can not|could not|couldn't"
                            r"|unable|minimum|at least|required|needs?\b|needed", re.I)
SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+")
# Lookups that carry no finding of their own; citing them beside a short history does not make the answer mixed.
LOOKUP_TOOLS = {"resolve_entities", "get_definitions"}


def trend_statements(text: str) -> list[str]:
    """Sentences that state a trend without saying it cannot be assessed."""
    return [s.strip() for s in SENTENCE_RE.split(text) if TREND_RE.search(s) and not CANNOT_TELL_RE.search(s)]


@dataclass
class Verification:
    ok: bool
    errors: list[str] = field(default_factory=list)
    failed_claims: list[int] = field(default_factory=list)

    def feedback(self) -> str:
        return "Verification failed:\n- " + "\n- ".join(self.errors)


def _blank(m: re.Match) -> str:
    return " " * len(m.group())


@dataclass
class Quantities:
    """The numbers of a piece of text, each labelled once."""
    dates: list[tuple[str, str]] = field(default_factory=list)     # (as written, text that must appear in a tool result)
    measurements: list[float] = field(default_factory=list)
    counts: list[tuple[float, str]] = field(default_factory=list)  # (number, phrase as written)
    values: list[float] = field(default_factory=list)

    @property
    def numbers(self) -> list[float]:
        return self.measurements + [n for n, _ in self.counts] + self.values


def quantities(text: str) -> Quantities:
    q = Quantities()
    for rx in (EVID_RE,):                                   # dates inside evidence IDs are part of the ID
        text = rx.sub(_blank, text)
    for m in DATE_RE.finditer(text):
        q.dates.append((m.group(), m.group()[:10]))
    text = DATE_RE.sub(_blank, text)
    text = DEVICE_RE.sub(_blank, text)
    text = EMP_RE.sub(_blank, text)

    found, text = written_dates(text)
    q.dates.extend(found)
    for m in YEAR_RE.finditer(text):                        # a bare year: "during 2026"
        q.dates.append((m.group(1), f"{m.group(1)}-"))
    text = YEAR_RE.sub(_blank, text)

    count_spans = {m.span(1): m.group(0) for m in COUNT_TEXT_RE.finditer(text) if not UNIT_RE.match(text, m.end(1))}
    for m in NUM_RE.finditer(text):
        try:
            x = float(m.group().replace(",", ""))
        except ValueError:
            continue
        if UNIT_RE.match(text, m.end()):
            q.measurements.append(x)
        elif m.span() in count_spans:
            q.counts.append((x, count_spans[m.span()]))
        else:
            q.values.append(x)
    return q


def _numbers(text: str) -> list[float]:
    """Every number in the text that is not a date or part of an ID."""
    return quantities(text).numbers


def _decimals(x: float, raw: str | None = None) -> int:
    s = raw if raw is not None else repr(x)
    return len(s.split(".")[1]) if "." in s and not s.endswith(".0") else 0


def _matches(x: float, allowed: set[float]) -> bool:
    """True when the number equals a tool value, or the tool value rounded to the precision the answer used.

    Sizes are compared, not signs: an answer says "falling 0.51% a day" for a slope of -0.51, and the
    direction is in the words. A different size (0.61) still fails.
    """
    x = abs(x)
    if x in ALWAYS_OK_NUMBERS:
        return True
    d = _decimals(x)
    return any(abs(y) == x or round(abs(y), d) == x or abs(abs(y) - x) < 1e-6 for y in allowed)


class Verifier:
    def __init__(self, tk: Toolkit):
        self.tk = tk

    def _allowed_numbers(self) -> set[float]:
        nums: set[float] = set()
        for blob in self.tk.tool_corpus:
            nums.update(_numbers(blob))
        for name in self.tk.config.thresholds.names():
            nums.update(_numbers(self.tk.config.thresholds.describe(name)))
        for r in self.tk.evidence.receipts.values():
            nums.add(float(r["result_count"]))
        return nums

    def _corpus(self) -> str:
        return "\n".join(self.tk.tool_corpus)

    def _names(self, corpus: str) -> list[re.Pattern]:
        """Model names and hostnames a tool returned. A digit inside one is part of the name, not a number
        of its own: "Dell XPS 15 laptop" is one laptop, not fifteen."""
        names: set[str] = set()
        for d in self.tk.repo.devices():
            for key in ("model_name", "model_identifier", "hostname"):
                name = (d.get(key) or "").strip()
                if name and any(ch.isdigit() for ch in name) and name in corpus:
                    names.add(name)
        return [re.compile(rf"(?<![\w-]){re.escape(n)}(?![\w-])") for n in sorted(names, key=len, reverse=True)]

    def _short_histories(self) -> tuple[dict[str, int], set[str]]:
        """Evidence that belongs only to a history with too few snapshots for a trend.

        Returns {evidence or receipt ID: snapshots in that history} and the IDs of lookups that carry no
        finding. A snapshot that a long-enough history also returned is not short."""
        ev = self.tk.evidence
        min_pts = int(self.tk.config.thresholds.get("min_points_for_trend", "points"))
        short: dict[str, int] = {}
        long_ids: set[str] = set()
        lookups: set[str] = set()
        for rid, r in ev.receipts.items():
            if r["tool"] in LOOKUP_TOOLS:
                lookups.add(rid)
            if r["tool"] != "get_device_history":
                continue
            if r["result_count"] < min_pts:
                for e in [rid, *r["evidence_ids"]]:
                    short[e] = r["result_count"]
            else:
                long_ids.update(r["evidence_ids"])
        return {e: n for e, n in short.items() if e not in long_ids}, lookups

    def verify(self, ans: FinalAnswer, question: str = "") -> Verification:
        ev = self.tk.evidence
        errors: list[str] = []
        failed: set[int] = set()
        corpus = self._corpus()
        allowed_nums = self._allowed_numbers()
        # A period the user asked about may be named back ("no data for April 2026"), even when no tool
        # result holds it: that is what an answer about a period outside the data has to say.
        names = self._names(corpus)
        asked_dates = {needle for _, needle in quantities(question).dates}
        asked_months = {MONTHS[m.group(1)] for m in MONTH_RE.finditer(question)}

        def date_ok(needle: str) -> bool:
            if needle in corpus or needle in asked_dates:
                return True
            month_and_year = re.fullmatch(r"(\d{4})-(\d{2})-", needle)   # "April 2026" for a question about "April"
            return bool(month_and_year and int(month_and_year.group(2)) in asked_months and f"{month_and_year.group(1)}-" in corpus)

        def ids_ok(text: str, where: str, idx: int | None) -> None:
            for token in set(DEVICE_RE.findall(EVID_RE.sub(" ", text))) | set(EMP_RE.findall(text)):
                if token not in corpus:
                    errors.append(f"{where}: '{token}' does not appear in any tool result (rule 2).")
                    if idx is not None:
                        failed.add(idx)

        def nums_ok(text: str, where: str, idx: int | None, extra: set[float], count_ok: set[float] | None = None) -> None:
            for name in names:
                text = name.sub(_blank, text)
            q = quantities(text)
            bad: list[str] = []
            for shown, needle in q.dates:
                if not date_ok(needle):
                    bad.append(f"date '{shown}' does not appear in any tool result (rule 3).")
            for x in q.measurements + q.values:
                if not _matches(x, allowed_nums | extra):
                    bad.append(f"number {x:g} does not match any tool value, threshold or count (rule 3).")
            for x, phrase in q.counts:
                if count_ok is None:                         # the summary: any count a claim could state
                    if not _matches(x, allowed_nums | extra):
                        bad.append(f"number {x:g} does not match any tool value, threshold or count (rule 3).")
                elif x not in count_ok and x not in ALWAYS_OK_NUMBERS:
                    bad.append(f"'{phrase}' doesn't match the cited evidence counts {sorted(count_ok)[:12]} (rule 3).")
            for msg in bad:
                errors.append(f"{where}: {msg}")
                if idx is not None:
                    failed.add(idx)

        cited_defs: set[str] = set()
        for i, claim in enumerate(ans.claims):
            where = f"claim {i + 1}"
            missing = [e for e in claim.evidence_ids if not ev.has(e)]
            if missing:
                errors.append(f"{where}: evidence {missing} was not returned by any tool in this session (rule 1).")
                failed.add(i)
            if not claim.evidence_ids and ans.answer_type == "answer":
                errors.append(f"{where}: no evidence cited (rule 5).")
                failed.add(i)
            devices_cited = {ev.get(e).device_id for e in claim.evidence_ids if ev.get(e) and ev.get(e).device_id}
            extra = {float(len(devices_cited)), float(len(claim.evidence_ids))}
            for e in claim.evidence_ids:
                r = ev.receipts.get(e)
                if r:
                    cited_defs.update(r.get("definitions_used", []))
                    extra.add(float(r["result_count"]))
            # A count must be a whole number in the evidence this claim cites, not anywhere in the session:
            # how many devices or records it cites, a cited receipt's result count, or a whole number the
            # cited result itself reported (for example devices_checked).
            count_ok = {float(len(devices_cited)), float(len(claim.evidence_ids))}
            for e in claim.evidence_ids:
                r = ev.receipts.get(e)
                if r:
                    count_ok.add(float(r["result_count"]))
                    count_ok.update(float(n) for n in r.get("numbers", ()))
                elif ev.get(e):
                    count_ok.update(float(n) for n in whole_numbers(ev.get(e).record))
            for k, v in claim.values.items():
                if COUNT_KEY_RE.match(k) and isinstance(v, (int, float)) and not isinstance(v, bool) and float(v) not in count_ok:
                    errors.append(f"{where}: {k}={v} doesn't match the cited evidence (counts {sorted(count_ok)[:12]}) (rule 3).")
                    failed.add(i)
            values_text = " ".join(f"{k} {v}" for k, v in claim.values.items())
            ids_ok(claim.text + " " + values_text, where, i)
            nums_ok(claim.text, where, i, extra, count_ok)
            for k, v in claim.values.items():
                for item in v if isinstance(v, list) else [v]:
                    if isinstance(item, (int, float)) and not isinstance(item, bool):
                        if not _matches(float(item), allowed_nums | extra):
                            errors.append(f"{where}: value {k}={item} does not match tool output (rule 3).")
                            failed.add(i)
            # rule 6: completeness against cited receipts
            mentioned = set(DEVICE_RE.findall(claim.text + " " + values_text))
            for e in claim.evidence_ids:
                r = ev.receipts.get(e)
                if not r:
                    continue
                listed = {ev.get(x).device_id for x in r["evidence_ids"] if ev.get(x) and ev.get(x).device_id}
                max_listed = int(self.tk.config.thresholds.get("max_devices_per_answer", "count"))
                states_count = any(isinstance(v, (int, float)) and float(v) == float(r["result_count"])
                                   for v in claim.values.values()) or str(r["result_count"]) in claim.text
                # "lists some devices" means it names at least one from that result; a device named for
                # another reason (one with too little data, say) is not a partial list
                if listed and len(listed) <= max_listed and mentioned & listed and not listed <= mentioned | set(ans.summary.split()) and not states_count:
                    left_out = sorted(listed - mentioned)
                    errors.append(f"{where}: lists some devices from {e} but leaves out {left_out[:5]} without stating the total (rule 6).")
                    failed.add(i)

        ids_ok(ans.summary, "summary", None)
        summary_extra = {float(len(ans.claims))}
        for c in ans.claims:
            for e in c.evidence_ids:
                r = ev.receipts.get(e)
                if r:
                    summary_extra.add(float(r["result_count"]))
            for v in c.values.values():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    summary_extra.add(float(v))
        nums_ok(ans.summary, "summary", None, summary_extra)

        # rule 4: thresholds stated
        for claim in ans.claims:
            for e in claim.evidence_ids:
                rec = ev.get(e)
                if rec and rec.kind == "insight":
                    cited_defs.update(rec.record.get("definitions_used", []))
        missing_defs = sorted(d for d in cited_defs if d not in ans.definitions_used and d != "min_points_for_trend")
        if missing_defs:
            errors.append(f"definitions_used must include {missing_defs}, which the cited results applied (rule 4).")

        # rule 7: no trend from too few snapshots
        short, lookups = self._short_histories()
        if short:
            min_pts = int(self.tk.config.thresholds.get("min_points_for_trend", "points"))
            on_short = [i for i, c in enumerate(ans.claims) if any(e in short for e in c.evidence_ids)]
            fewest = min((short[e] for i in on_short for e in ans.claims[i].evidence_ids if e in short), default=0)
            how = (f"the history it rests on has {fewest} snapshot(s) and a trend needs at least {min_pts}. State the recorded "
                   "values and that a trend cannot be assessed; do not say whether the value is or is not rising, falling or stable")
            for i in on_short:
                for sentence in trend_statements(ans.claims[i].text):
                    errors.append(f"claim {i + 1}: \"{sentence[:120]}\" states a trend, but {how} (rule 7).")
                    failed.add(i)
            if on_short:
                for sentence in trend_statements(ans.summary):
                    errors.append(f"summary: \"{sentence[:120]}\" states a trend, but {how} (rule 7).")
            only_short = on_short and all(e in short or e in lookups for c in ans.claims for e in c.evidence_ids)
            if only_short and TREND_RE.search(question) and ans.answer_type == "answer":
                errors.append(f"The question asks about a trend and the only history has {fewest} snapshot(s), fewer than the "
                              f"{min_pts} a trend needs: set answer_type to \"insufficient_data\" (rule 7).")

        # rule 5: no-matches must cite a zero-result receipt
        if ans.answer_type == "no_matches":
            zero = [c for c in ans.claims for e in c.evidence_ids if ev.receipts.get(e, {}).get("result_count") == 0]
            if not zero:
                errors.append("A 'no_matches' answer must cite the receipt (Q-…) of a query that returned 0 results (rule 5).")
        if ans.answer_type == "answer" and not ans.claims:
            errors.append("An answer needs at least one claim with evidence (rule 5).")
        return Verification(ok=not errors, errors=errors, failed_claims=sorted(failed))


def strip_failed(ans: FinalAnswer, v: Verification) -> FinalAnswer:
    """Fallback after revisions run out: keep only claims that passed."""
    kept = [c for i, c in enumerate(ans.claims) if i not in v.failed_claims]
    caveats = list(ans.caveats) + ["Some statements could not be verified against the data and were removed."]
    if not kept:
        return FinalAnswer(summary="I could not verify an answer to this from the telemetry.", claims=[],
                           caveats=caveats, answer_type="insufficient_data")
    return FinalAnswer(summary=kept[0].text if v.errors and any("summary" in e for e in v.errors) else ans.summary,
                       claims=kept, definitions_used=ans.definitions_used, caveats=caveats, answer_type=ans.answer_type)


def as_dict(obj: Any) -> Any:
    return obj.model_dump() if hasattr(obj, "model_dump") else obj
