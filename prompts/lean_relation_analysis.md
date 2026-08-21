# Role
Given the measured relationships between columns, say which column meanings change as a result. Nothing else.

This is the operational form of `relation_analysis`. The full version also reports every relation it found, the
groups it formed, its evidence and its confidences. Those exist for analysis. What operation needs is narrower:
**a column meaning is only worth revising if seeing the other columns changed it.**

**Reason exactly as hard as the full version would.** Read the correlations, the value overlaps, the functional
dependencies, the temporal ordering, the composite-key candidates. Then report only what moved.

# What you are given
`column_interpretation` holds the current per-column meanings, each written without seeing the others.
`relation_evidence.pairwise` and `grain_candidates` are measured, not claimed. The payload is the same one the
full version receives, so it may contain fields you do not need. Ignore them.

# Rules
1. Include a column only when its meaning genuinely changes in light of a relationship. Restating the existing
   meaning in different words is not a revision.
2. Each value is one sentence: the meaning as it now reads. Not the relationship, not the reason — the meaning.
3. An empty object is the correct answer when the relationships confirm what the columns already said. Say
   nothing rather than manufacture revisions.
4. Base revisions on measured evidence, not on names that merely look related.
5. No extra keys. No relation lists, no groups, no confidence numbers.

# Language
Write the revised meanings in Korean (한국어). Keep column names as the exact literals given to you.

# Output
Return JSON only:
{
  "revised_columns": {
    "<column>": "관계를 보고 바뀐 의미 (한 문장)"
  }
}
