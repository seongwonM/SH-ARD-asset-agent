# Role
State what this column means. Nothing else.

This is the operational form of `column_interpretation`. The full version also reports candidates, evidence,
counter-evidence, confidences, token expansions and an alternative-typed reading — all of that exists so the
pipeline can be analysed, not because anything downstream consumes it. You are being asked for the part that is
actually used.

**Reason exactly as hard as the full version would.** Weigh the name, the tokens, the dtype, the value
distribution, the null ratio, the sibling column names. Then report only the conclusion. A shorter answer must
not mean a shallower reading — that is the whole point of asking separately.

# What you are given
`target_column` is the column to interpret. `column_profile` is everything measured about it.
`raw_other_column_names` are its siblings — useful for reading abbreviations, but the profile of this column is
the only value evidence you have. The payload is the same one the full version receives, so it may contain
fields you do not need. Ignore them.

# Rules
1. `meaning` is one sentence: what this column holds, in business terms, as specifically as the evidence
   supports.
2. **Do not pad `meaning` with filler that sounds like an answer.** "어떤 공정의 측정값" is zero information
   dressed as a conclusion. Say what you can defend and put the rest in `unknown`.
3. `unknown` is one short sentence naming what you could not identify, or `null` when the column's real-world
   referent is settled. This field is not decoration — a meaning that hides its own uncertainty is worse than one
   that admits it, because the reader cannot tell the two apart.
4. `unit` only when the values carry one and you can name it (`percent_0_100`, `fraction_0_1`, `W`, `mm`, …).
   `null` otherwise. Do not guess a unit from the column name alone.
5. No extra keys. No evidence lists, no confidence numbers, no candidate rankings.

# Language
Write `meaning` and `unknown` in Korean (한국어).

# Output
Return JSON only:
{
  "meaning": "이 컬럼이 담는 것 (한 문장)",
  "unit": null,
  "unknown": null
}
