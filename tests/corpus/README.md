# Evaluation corpus and quality gates

Deterministic regression scenarios for ingestion fidelity, citations, temporal rules, and epistemic honesty. These tests run offline in CI without hosted models.

## Quality thresholds (v0.1)

| Gate | Threshold |
|------|-----------|
| Unit + integration suite | 100% pass offline |
| `knowledge-desk validate` on empty/bootstrap vault | valid |
| Citation round-trip (locator → normalized text) | required for corpus fixtures |
| Unknown vs neutral | missing evidence must yield `unknown` / `insufficient_evidence`, never synthetic neutral orientation |
| Explicit vs inferred | `statement_basis` preserved; inference without rationale is a lint warning |
| Rebuild determinism | index rebuild twice yields same hit count for fixed vault |
| Unsupported synthesis | wiki material prose without evidence fails lint/refine-validate |

Optional model-assisted evals are **out of scope** here and must be marked separately if added later.

## Domains in fixtures

- Ecology (field notes / acoustic sampling)
- History (letter transcription)
- Generic interview/text + markdown research note

Finance-specific fields are intentionally absent from core fixtures.
