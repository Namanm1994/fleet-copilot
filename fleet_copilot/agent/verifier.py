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
# "May 2026", "22 May 2026", "May 22", "May 22, 2026". Month names are matched with their capital letter only.
WRITTEN_DATE_RE = re.compile(r"\b(?:(\d{1,2})(?:st|nd|rd|th)?\s+)?(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b\.?"
                             r"(?:\s+(\d{1,2})(?:st|nd|rd|th)?(?!\d|\s?[%\w]))?(?:,?\s+((?:19|20)\d{2})\b)?")
# A bare year ("during 2026"). Not a year: part of a larger number, a measurement, or a device count.
YEAR_RE = re.compile(r"(?<![\w.,-])((?:19|20)\d{2})(?![\w%-]|[.,]\d|\s?(?:[KMGT]i?B|mAh|Wh)\b"
                     r"|\s+(?:\w+\s+)?(?:devices?|laptops?|machines?|computers?|endpoints?)\b)")


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

    def written(m: re.Match) -> str:
        day, month, day2, year = m.group(1), MONTHS[m.group(2)], m.group(3), m.group(4)
        day = day or day2
        if not day and not year:
            return m.group()                                # a month name alone states no number
        if year and day:
            needle = f"{year}-{month:02d}-{int(day):02d}"
        elif year:
            needle = f"{year}-{month:02d}-"
        else:
            needle = f"-{month:02d}-{int(day):02d}"
        q.dates.append((m.group().strip(), needle))
        return _blank(m)

    text = WRITTEN_DATE_RE.sub(written, text)
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
    if x in allowed or abs(x) in ALWAYS_OK_NUMBERS:
        return True
    d = _decimals(x)
    return any(round(y, d) == x for y in allowed) or any(abs(y - x) < 1e-6 for y in allowed)


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

    def verify(self, ans: FinalAnswer) -> Verification:
        ev = self.tk.evidence
        errors: list[str] = []
        failed: set[int] = set()
        corpus = self._corpus()
        allowed_nums = self._allowed_numbers()

        def ids_ok(text: str, where: str, idx: int | None) -> None:
            for token in set(DEVICE_RE.findall(EVID_RE.sub(" ", text))) | set(EMP_RE.findall(text)):
                if token not in corpus:
                    errors.append(f"{where}: '{token}' does not appear in any tool result (rule 2).")
                    if idx is not None:
                        failed.add(idx)

        def nums_ok(text: str, where: str, idx: int | None, extra: set[float], count_ok: set[float] | None = None) -> None:
            q = quantities(text)
            bad: list[str] = []
            for shown, needle in q.dates:
                if needle not in corpus:
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
