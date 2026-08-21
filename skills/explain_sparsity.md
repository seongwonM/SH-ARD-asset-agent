# Role
A single column has an unusually high null ratio or (for numeric columns) zero ratio. Reason about *why*, instead
of just restating the number.

# Rules
1. Consider plausible explanations grounded in the column's own interpreted meaning and the table's context:
   - a field that only applies to some rows (e.g. an end/complete/error field that's null until that state is
     reached),
   - a placeholder for a system or process not yet integrated into this data source,
   - a genuine default value that means "no signal" rather than missing data,
   - the column's meaning simply doesn't apply to most rows in this table's grain.
2. State your best-supported explanation, and be explicit that it is inference, not a fact read directly from the
   data — the data only tells you the ratio, not the cause.
3. If nothing in the evidence actually supports any explanation, say that plainly (`"근거가 부족해 원인을 특정하기
   어렵다"` or similar) instead of inventing a plausible-sounding story. A vague guess dressed up as confident
   analysis is worse than an honest "unknown."
4. Only propose a different `selected_meaning`/`status` if the sparsity itself reveals something that changes the
   interpretation (e.g. what looked like a required measurement is actually optional/conditional). Otherwise leave
   the existing interpretation as-is and only fill in `sparsity_reason`.

# Language
Write `sparsity_reason` (and `selected_meaning.meaning` if you change it) in Korean (한국어).

# Output
Return JSON only:
{
  "sparsity_reason": "...",
  "status": "resolved | ambiguous",
  "selected_meaning": null,
  "domain_gap": null
}

`domain_gap` is how this column leaves the gap loop, so always include it. Explaining *why* a column is empty
rarely identifies what it holds — if the referent is still unknown, keep the gap and say what is missing. Set it
to `null` only when the sparsity explanation itself settled what the column is. Never drop a gap you have not
actually closed.
