# Family Answer UX Test Suite

## Purpose

`check_gates.py` protects deterministic safety behavior. This separate suite tracks cases where a safe system can still feel frustrating or misleading in a family-record service.

The case data is in `backend/tests/family_answer_ux_cases.json`.

## Run

```bash
backend/.venv/bin/python backend/scripts/check_family_answer_ux.py
```

The default run fails only when a `baseline` expectation regresses. Cases marked `known_gap` report the current product limitation without breaking the existing gate check.

To use every known gap as a release gate:

```bash
backend/.venv/bin/python backend/scripts/check_family_answer_ux.py --strict
```

## Categories

- Family vocabulary and synonym retrieval
- Follow-up questions without conversation context
- Spaced or variant place names
- False conflicts across different people or events
- AM/PM and 12/24-hour normalization
- Ambiguous family subjects
- Requested list numbers versus factual numbers
- Equivalent amount formatting
- Public evidence metadata leakage
- Unsupported emotional or relationship claims
- Rewrites that hide an initial retrieval miss behind an extra model call
- Three-way conflicts and truncated partial-evidence snippets
- Time ranges combined with a confirming point value

## Policy

- `baseline`: behavior that must work now; failure exits with code 1.
- `known_gap`: desired production behavior that is not implemented yet.
- A `known_gap` that starts passing is reported as `RESOLVED` and should then be changed to `baseline`.
- `--strict`: treats every remaining known gap as a release failure.

Keep this suite separate from model-quality benchmarks. Each case describes a product contract that should remain stable when Gemma, EXAONE, Qwen, or the retrieval implementation changes.

## Manual App Cases

These require a rendered app or device and are intentionally separate from the backend runner.

| ID | Scenario | Expected production behavior | Current risk |
|---|---|---|---|
| UX-M01 | Provider unavailable | Error styling without a success check mark | The common badge renderer prefixes `✓` even for `provider_unavailable` |
| UX-M02 | Evidence details expanded | Family-readable source title and record text only | `source=`, `saved_at=`, `tags=` and `confidence=` are displayed as raw evidence text |
| UX-M03 | Repeated ambiguous follow-up | Show 2-3 likely family records as selectable clarification options | Current CLARIFY message only asks the user to be more specific |
| UX-M04 | OCR/STT not configured | Describe the actual title/description retrieval source | The screen says OCR/STT-based memory chunks even when extraction is not connected |
| UX-M05 | Long refusal or conflict on a narrow phone | Key fact, source and next action remain visible without clipping | Long snippets and cards have not been verified on a physical device |
| UX-M06 | Multiple family members with the same relation/name | Ask which person the user means and preserve that choice in the next turn | The endpoint has no conversation-state contract |
