"""Deterministic answer verification (G14, G15, G16; gaps F-11, F-12, F-13).

Rules:
1  every cited evidence ID was returned by a tool in this session
2  every device or employee ID in the answer (structured values AND free text) came from a tool
3  every number in the answer matches a tool value, a threshold, or a count of cited evidence
4  thresholds the cited tool results used are stated in definitions_used
5  no factual claim without evidence; "no matches" must cite a zero-result receipt
6  completeness: a claim built on a device list names every device or states the count
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fleet_copilot.agent.models import FinalAnswer

if TYPE_CHECKING:
    from fleet_copilot.toolkit import Toolkit

DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:T[\d:]+Z?)?\b")
EVID_RE = re.compile(r"\b(?:INS-[\w-]+|Q-\d+-\d+|PRP-\d+|[A-Za-z0-9-]+@[\dT:-]+(?:[#$][\w-]+)?)")
DEVICE_RE = re.compile(r"\b(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{7,}\b")
EMP_RE = re.compile(r"\bemp-[a-z0-9-]+\b")
NUM_RE = re.compile(r"(?<![\w.])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\w.])|(?<![\w.])-?\d+(?:\.\d+)?(?![\w])")
ALWAYS_OK_NUMBERS = {0.0, 1.0}


@dataclass
class Verification:
    ok: bool
    errors: list[str] = field(default_factory=list)
    failed_claims: list[int] = field(default_factory=list)

    def feedback(self) -> str:
        return "Verification failed:\n- " + "\n- ".join(self.errors)


def _numbers(text: str) -> list[float]:
    text = DATE_RE.sub(" ", text)
    text = EVID_RE.sub(" ", text)
    text = DEVICE_RE.sub(" ", text)
    text = EMP_RE.sub(" ", text)
    out = []
    for m in NUM_RE.findall(text):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


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

        def nums_ok(text: str, where: str, idx: int | None, extra: set[float]) -> None:
            for x in _numbers(text):
                if not _matches(x, allowed_nums | extra):
                    errors.append(f"{where}: number {x:g} does not match any tool value, threshold or count (rule 3).")
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
            values_text = " ".join(f"{k} {v}" for k, v in claim.values.items())
            ids_ok(claim.text + " " + values_text, where, i)
            nums_ok(claim.text, where, i, extra)
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
                if listed and len(listed) <= max_listed and mentioned and not listed <= mentioned | set(ans.summary.split()) and not states_count:
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
