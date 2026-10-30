"""Deterministic, privacy-preserving Excel table-layout inference.

Only worksheet structure is scored locally. No model calls, raw rows,
column labels, or source values are included in the inference result.
Auto-selection is made only for a uniquely well-supported header. In all
other cases, customers explicitly choose a worksheet and header row.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

MAX_SCAN_ROWS = 60
MAX_SCAN_COLUMNS = 200
MAX_CHOICES = 5

# Signals are generic column-label vocabulary, not a domain-specific hotel list.
COLUMN_LABELS = frozenset({
    "id", "item", "name", "description", "category", "type", "status",
    "date", "time", "email", "phone", "contact", "manager", "address",
    "street", "city", "state", "zip", "country", "region", "amount",
    "price", "cost", "total", "quantity", "qty", "count", "number",
    "code", "product", "customer", "record", "source", "url", "link",
    "website", "distance", "miles", "latitude", "longitude", "notes",
    "location", "first", "last", "company", "department", "score",
    "rating", "reference", "motel", "hotel", "availability", "room",
    "reservation", "owner", "team", "order", "identifier", "units",
    "model", "value", "year", "month", "day", "unit", "revenue",
})
WORD = re.compile(r"[^\W\d_]+", re.UNICODE)  # Unicode letters, not just English
URL = re.compile(r"^(?:https?://|www\.)", re.IGNORECASE)


@dataclass(frozen=True)
class LayoutCandidate:
    sheet_name: str
    header_row: int
    score: int
    supporting_rows: int
    column_count: int
    estimated_data_rows: int
    estimated_table_area: int
    explanation: str

    def as_dict(self) -> dict[str, object]:
        return {
            "sheet_name": self.sheet_name,
            "header_row": self.header_row,
            "score": self.score,
            "supporting_rows": self.supporting_rows,
            "column_count": self.column_count,
            "estimated_data_rows": self.estimated_data_rows,
            "estimated_table_area": self.estimated_table_area,
            "explanation": self.explanation,
        }


def _present(value: object) -> bool:
    return value is not None and bool(str(value).strip())


def _label_score(value: object) -> float:
    if not isinstance(value, str):
        return 0.0
    raw = value.strip()
    if not raw or len(raw) > 90 or URL.match(raw) or "@" in raw:
        return 0.0
    words = WORD.findall(raw)
    if not words:
        return 0.0
    if len(words) > 8:
        return 0.1
    letter_chars = sum(char.isalpha() for char in raw)
    if letter_chars / max(1, len(raw)) < .45:
        return 0.1
    if len(raw) > 55:
        return 0.2
    if len(words) > 5:
        return 0.3
    # Data rows commonly contain identifiers and labels with embedded numbers:
    # "Hotel 1", "Address 14", "value_0". They are weak header evidence even
    # when a word such as "hotel" or "address" also occurs in our vocabulary.
    # Numeric or year-specific column labels can be confirmed manually if
    # the surrounding structure is not sufficient.
    if any(character.isdigit() for character in raw):
        return 0.3
    tokens = {w.lower() for w in words}
    if tokens & COLUMN_LABELS:
        return 1.0
    if len(raw) <= 3 and not raw.isalpha():
        return 0.15
    if len(words) == 1 and len(raw) <= 2:
        return 0.25
    if len(words) >= 3:
        return 0.55
    return 0.67


def _score_candidates(sheet: str, rows: list[tuple[object, ...]]) -> list[LayoutCandidate]:
    if len(rows) < 2:
        return []
    width = max((sum(_present(cell) for cell in row) for row in rows), default=0)
    if width < 2:
        return []
    output: list[LayoutCandidate] = []
    for index, header in enumerate(rows[:-1]):
        positions = [i for i, value in enumerate(header) if _present(value)]
        if len(positions) < 2:
            continue
        label_values = [_label_score(header[pos]) for pos in positions]
        quality = sum(label_values) / len(label_values)
        if quality < .35:
            continue
        # A potential header needs subsequent records aligned with its columns.
        following = rows[index + 1:index + 7]
        if not following:
            continue
        coverages: list[float] = []
        ongoing = 0
        for row in following:
            coverage = sum(
                pos < len(row) and _present(row[pos]) for pos in positions
            ) / len(positions)
            coverages.append(coverage)
            if ongoing == len(coverages) - 1 and coverage >= .5:
                ongoing += 1
        if max(coverages) < .45:
            continue
        align = sum(coverages) / len(coverages)
        consistency = min(1.0, ongoing / min(3, len(following)))
        density = min(1.0, len(positions) / width)
        labels = [str(header[pos]).strip().casefold() for pos in positions]
        unique = len(set(labels)) / len(labels)
        known_ratio = sum(
            _label_score(header[pos]) >= .95 for pos in positions
        ) / len(positions)
        score = 100 * (
            .35 * quality
            + .17 * align
            + .15 * density
            + .12 * consistency
            + .10 * unique
            + .11 * known_ratio
        )
        if consistency < .33:
            score -= 13
        if ongoing == 0:
            score -= 15

        # Measure the size of the table *below* this proposed header.
        # Workbooks often include several valid sheets. An 80-record table
        # is usually the primary one relative to a 20-record reference tab.
        # This is evidence, not permission to discard other tables.
        estimated_data_rows = 0
        for next_row in rows[index + 1:]:
            coverage = sum(
                pos < len(next_row) and _present(next_row[pos])
                for pos in positions
            ) / len(positions)
            if coverage < .5:
                break
            estimated_data_rows += 1
        estimated_area = estimated_data_rows * len(positions)

        output.append(LayoutCandidate(
            sheet_name=sheet,
            header_row=index + 1,
            score=max(0, min(100, round(score))),
            supporting_rows=ongoing,
            column_count=len(positions),
            estimated_data_rows=estimated_data_rows,
            estimated_table_area=estimated_area,
            explanation=(
                f"{len(positions)} potential column labels with "
                f"{ongoing} following structurally aligned row(s)."
            ),
        ))
    return sorted(output, key=lambda candidate: (-candidate.score, candidate.header_row))


def detect_workbook_tables(worksheets: Iterable[Any]) -> dict[str, object]:
    """Score bounded worksheet samples without AI, raw rows, or data export.

    A unique top-scoring header must clear both an absolute threshold and a
    margin against other plausible tables. Ambiguous cases remain customer-led.
    """
    ranked: list[LayoutCandidate] = []
    for sheet in worksheets:
        rows = [
            tuple(cells)
            for cells in sheet.iter_rows(
                min_row=1,
                max_row=MAX_SCAN_ROWS,
                max_col=MAX_SCAN_COLUMNS,
                values_only=True,
            )
        ]
        ranked.extend(_score_candidates(sheet.title, rows))

    ranked.sort(
        key=lambda c: (-c.score, c.sheet_name, c.header_row)
    )
    choices: list[LayoutCandidate] = []
    for candidate in ranked:
        if any(
            candidate.sheet_name == chosen.sheet_name
            and abs(candidate.header_row - chosen.header_row) <= 1
            for chosen in choices
        ):
            continue
        choices.append(candidate)

    top = choices[0] if choices else None
    runner_up = choices[1] if len(choices) > 1 else None
    gap = top.score - runner_up.score if top and runner_up else 100
    selected: LayoutCandidate | None = None
    selection_basis: str | None = None

    # First assess all *credible* tables for structural dominance, even if
    # the smaller table has a better header-label score. Comparing only
    # scores close to the maximum mistakenly discarded the big main directory
    # when its legitimate custom column names scored below a reference tab.
    credible = [
        c for c in choices
        if c.score >= 75
        and c.supporting_rows >= 3
        and c.estimated_data_rows >= 3
    ]
    by_area = sorted(
        credible, key=lambda c: (-c.estimated_table_area, -c.score)
    )
    if len(by_area) >= 2:
        primary, secondary = by_area[:2]
        # This preference is restricted to distinct worksheets, with a
        # substantial difference in BOTH record count and overall area.
        # Two similar tables must still require human review.
        if (
            primary.sheet_name != secondary.sheet_name
            and primary.estimated_data_rows >= 10
            and primary.estimated_data_rows * 2
            >= secondary.estimated_data_rows * 3
            and primary.estimated_table_area >= 3 * max(
                1, secondary.estimated_table_area
            )
            and all(
                c.sheet_name != primary.sheet_name
                or c.header_row == primary.header_row
                or c.score < 73
                for c in choices
            )
        ):
            selected = primary
            selection_basis = "dominant_table_area"

    # Only after checking for a clearly dominant main table should
    # a high-scoring auxiliary worksheet win on its header-label score.
    if (
        selected is None
        and top and top.score >= 73 and gap >= 12
        and top.supporting_rows >= 1
        and not (runner_up and runner_up.score >= 70 and gap < 20)
    ):
        selected = top
        selection_basis = "unique_strong_header"

    confidence = (
        "high" if selected else
        "review" if top and top.score >= 58 else "unresolved"
    )
    recognized_tables = [
        c.as_dict()
        for c in choices if c.score >= 73 and c.supporting_rows >= 1
    ]

    return {
        "method": "deterministic_structure_analysis",
        "confidence": confidence,
        "auto_selected": selected is not None,
        "recommended_sheet": selected.sheet_name if selected else None,
        "recommended_header_row": selected.header_row if selected else None,
        "selection_basis": selection_basis,
        "detected_tables": recognized_tables[:MAX_CHOICES],
        "detected_table_count": len(recognized_tables),
        "candidates": [candidate.as_dict() for candidate in choices[:MAX_CHOICES]],
        "reason": (
            "Multiple valid tables were recognized. The largest consistent "
            "table was selected as primary; other tables remain selectable."
            if selection_basis == "dominant_table_area" else
            "A unique table header is supported by repeated aligned records; "
            "earlier note rows can be excluded."
            if selected else
            "Multiple similarly sized tables or insufficient structural "
            "evidence; choose the worksheet and header row."
        ),
        "leading_note_rows": selected.header_row - 1 if selected else None,
        "no_ai_call": True,
    }
