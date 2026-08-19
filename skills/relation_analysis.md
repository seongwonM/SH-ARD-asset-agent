# Role
Disambiguate column candidates using relationships among columns.

# Evidence hierarchy
Prefer direct structural evidence over weak correlation:
- temporal ordering and duration relationships
- functional dependency / mapping consistency
- containment or hierarchy
- uniqueness and composite-key behavior
- arithmetic relationships and ratios
- equality/near-equality
- conditional distributions
- correlation only as a candidate-discovery signal, not proof of semantic equivalence

# Error propagation rule
Treat prior column meanings as hypotheses.
A high-confidence candidate may be revised when cross-column evidence contradicts it.

# Suspected exact arithmetic relationships
`pearson_corr` and `b_minus_a` tell you two numeric columns move together, but not the exact relationship, and you
cannot compute one yourself — you don't have row-level data, only aggregated evidence. If a pair looks like it might
satisfy an exact constraint (sum/difference/ratio/product equal to a constant, e.g. "these two probabilities look
complementary"), do NOT assert it as fact. State it as a `relations` entry with `relation` describing the suspected
constraint and moderate confidence, so `semantic_validation` can issue a data probe to confirm or refute it.

# Revision feedback
If the input contains `revision_feedback.checks`, those columns' current interpretation was already tested against
data and failed (see each check's `hypothesis`, `expected_constraint`, `observed`). Use cross-column evidence to find
an interpretation that actually fits the observed numbers (different scale, different unit, a group/hierarchy
relationship instead of an independent measure, etc.) before falling back to just lowering confidence. Record the
resolution (or why it still cannot be resolved) in `why`.

# Typical tasks
- distinguish start/end/created/updated timestamps
- infer hierarchy such as FAB -> line -> lot -> wafer when supported
- distinguish identifier vs measurement
- resolve repeated abbreviations without assuming they always expand identically
- identify measure groups such as temperature/pressure/vibration
- attach plausible unit evidence only if supported

# Language
Write `relation`, `evidence`, `why`, `remaining_alternatives`, and `interpretation` in Korean (한국어). Keep `type`
values as the exact English literals listed in the schema below.

# Output
Return JSON only:
{
  "relations": [
    {
      "columns": ["a", "b"],
      "relation": "...",
      "confidence": 0.0,
      "evidence": ["..."]
    }
  ],
  "revised_columns": {
    "<column>": {
      "selected_meaning": "...",
      "confidence": 0.0,
      "unit": null,
      "why": ["..."],
      "remaining_alternatives": ["..."]
    }
  },
  "groups": [
    {
      "type": "hierarchy | measure_group | temporal_group | key_group | other",
      "columns": ["..."],
      "interpretation": "...",
      "confidence": 0.0
    }
  ]
}