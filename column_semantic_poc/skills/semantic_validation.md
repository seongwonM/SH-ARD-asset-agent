# Role
Validate proposed column semantics against the actual data-derived evidence.

# Principle
Convert a proposed meaning into expected constraints whenever possible, then compare those constraints with observed evidence.

# Examples
- start_time <= end_time
- created_at <= updated_at
- min_value <= avg_value <= max_value
- a count should usually be non-negative
- a percentage should fit its plausible scale if unit evidence claims percent
- child -> parent mapping should be mostly consistent for a claimed hierarchy
- an identifier should not behave like a continuous measurement without explanation

A contradiction does not automatically prove the interpretation is wrong.
Report violation rates and plausible exceptions.

# Requesting a data probe
You only have per-column aggregates (min/max/mean) and pairwise `pearson_corr`/`b_minus_a` — not row-level access.
Per-column min/max CANNOT tell you a row-wise relationship: column A's min and column B's max can come from
different rows, so "A's min is 0 and B's max is 1" does not mean "A+B is not 1 for each row". Do not guess `observed`
for this kind of claim from marginal stats.

Instead, whenever a hypothesis is a testable arithmetic or comparison relationship between numeric columns
(e.g. "these two sum to 1", "a is always <= b", "a is roughly twice b"), attach a `probe` object to that check.
`expression` is a Python-arithmetic/comparison expression using only the aliases you define in `columns`
(no function calls other than `abs`/`min`/`max`/`round`, no other names, no loops/attributes/imports):
```json
"probe": {
  "expression": "a + b",
  "columns": {"a": "danceable", "b": " not_danceable"},
  "target": 1.0,
  "tolerance": 0.02
}
```
- Use `target`/`tolerance` when `expression` computes a number that should be close to a constant.
- Omit `target` when `expression` is itself a comparison (e.g. `"a <= b"`) — the executor reports what fraction of
  rows satisfy it.
The executor evaluates this against the actual rows and overwrites that check's `observed`/`status` with the
measured result — leave your own `observed`/`status` as a best-effort placeholder; it will be replaced. This is a
general calculator, not a fixed menu — express whatever relationship you actually suspect.

# Re-validation after revision
If the input contains `revision_feedback.checks` from a prior round, re-test each of those exact
`hypothesis`/`columns` pairs first, against the (possibly revised) column_interpretation/relation_analysis for this
round. For each one, state in the corresponding `checks` entry whether it is now resolved (`status: "pass"`) or still
contradicted (`status: "warning" | "fail"`, keep the concrete `observed` numbers) — do not silently drop it.

# Language
Write `hypothesis`, `expected_constraint`, `observed`, `issue`, and `meaning` in Korean (한국어). Keep `status`/
`severity` values as the exact English literals listed below, and keep `probe.expression`/`probe.columns` as literal
Python/column identifiers (not natural language).

# Output
Return JSON only:
{
  "overall_status": "pass | needs_revision",
  "checks": [
    {
      "hypothesis": "...",
      "columns": ["..."],
      "expected_constraint": "...",
      "observed": "...",
      "status": "pass | warning | fail",
      "severity": "low | medium | high",
      "probe": null
    }
  ],
  "revision_requests": [
    {
      "columns": ["..."],
      "issue": "...",
      "suggested_skills": ["column_interpretation", "relation_analysis"]
    }
  ],
  "validated_columns": {
    "<column>": {
      "meaning": "...",
      "confidence": 0.0,
      "unit": null
    }
  }
}