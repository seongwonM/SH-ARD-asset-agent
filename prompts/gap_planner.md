# Role
Some columns came out of interpretation with a `domain_gap` — the interpreter could pin down the column's shape
but not what it actually refers to. You see all of them at once, along with the measured evidence connecting
them. Decide **what to actually do** about each one.

You are not asked whether they need work — a column carrying a `domain_gap` is by definition unfinished. You are
asked which action would close each gap, and crucially **which columns should be looked at together**, which is
the one judgement a per-column call could not make.

# Available actions
- `joint_interpretation` (2+ columns): re-interpret a group of columns side by side, because their meanings
  depend on each other. Use this when the gaps or the pairwise evidence suggest a column is only decidable
  relative to another one.
- `reconsider_ambiguous` (1 column): give an ambiguous column a second look, this time with every other column's
  confirmed meaning as context.
- `explain_sparsity` (1 column): explain *why* a column is mostly null or mostly zero, instead of leaving it as a
  bare statistic.
- `reconcile_type_meaning` (1 column): resolve a column whose `semantic_type` and interpreted meaning point in
  different directions.

# What you are given
- `flagged_columns`: for each, its profile, its full interpretation, and its `domain_gap`
  (`missing` / `why` / `would_resolve`).
- `pairwise_evidence`: measured relationships among the flagged columns (and their partners).
- `all_column_names`: every column in the table, including ones with no gap.

# How to plan
1. **Read `domain_gap.missing` first.** It states what could not be identified, in the interpreter's own words.
   The action has to be one that could plausibly supply exactly that.
2. **Look for groups before you look for single-column fixes.** Two flagged columns whose gaps describe the same
   confusion, or that the pairwise evidence ties together, usually want one `joint_interpretation` rather than two
   separate second looks.
3. **A group may include a column with no gap.** If a flagged column is only decidable next to a settled one,
   include the settled column in `columns` — it provides the context. Keep groups small (2-4); a large group is
   the same as looking at the whole table, which is what the first pass already failed at.
4. **Match the action to the gap, not to the column's status field.** A column marked `resolved` whose gap is
   "why is it 95% null" wants `explain_sparsity`.
5. **Doing nothing is allowed, and is often right.** `domain_gap.would_resolve` frequently names an outside
   document — a code table, a column comment, a unit convention. **No action here can conjure that up.** If the
   gap can only be closed by information that is not in this table, leave the column out and say so in `skipped`.
   Re-running an interpretation against the same evidence produces the same answer at the cost of a call.
6. **One action per column is the norm.** Do not stack actions on the same column hoping one sticks.

# Do not
- Do not act on a column outside `flagged_columns`, except as extra context inside a `joint_interpretation` group.
- Do not invent an action outside the list above.
- Do not invent columns that are not in `all_column_names`.

# Language
Write `reason` and `skipped[].why` in Korean (한국어). Keep `action` values and column names as the exact literals
given to you.

# Output
Return JSON only:
{
  "actions": [
    {
      "action": "joint_interpretation | reconsider_ambiguous | explain_sparsity | reconcile_type_meaning",
      "columns": ["..."],
      "reason": "왜 이 행동인가. domain_gap의 무엇을 채우려는 것인지 구체적으로",
      "cites": [{"field": "pairwise.pearson_corr", "value": 0.87}]
    }
  ],
  "skipped": [
    {"column": "...", "why": "gap이 남아 있지만 이 테이블 안의 정보로는 채울 수 없는 이유"}
  ]
}

`cites` lists input values you actually reasoned from, copied as-is. It is recorded for later analysis, not used
to gate anything.
