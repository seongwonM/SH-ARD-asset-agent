# Role
(`semantic_type`과 `selected_meaning`은 이제 한 번의 컬럼 해석이 함께 낸다. 그래서 이 skill이 다루는 것은
"두 단계가 어긋난 경우"가 아니라 **한 응답이 스스로 모순된 경우**다 - 타입은 count인데 의미는 비율을 말하는 식.)

A single column's `semantic_type` (from the value-pattern-only first pass) and its interpreted `meaning`/`unit`
(from the naming/context-aware pass) point in different directions. Resolve the conflict.

# Rules
1. State the conflict concretely: which `semantic_type` label and which claimed meaning/unit actually disagree, and
   why they disagree (e.g. `semantic_type: count` implies non-negative integers, but the meaning candidate describes
   a percentage, which usually isn't reported as a bare count).
2. Re-examine `column_profile` (already given) to decide which side the data actually supports, and adjust
   `selected_meaning` to be consistent with it. `semantic_type` itself is not yours to change here — only
   `selected_meaning`/`status`.
3. It's possible both were reasonable given what each pass could see, and the "conflict" is actually fine (e.g. a
   count-like integer column that's ALSO plausibly a rank — not a real contradiction). If so, say that explicitly
   and don't force a change.
4. If you genuinely can't tell which is right from the evidence, mark `status: "ambiguous"` rather than picking one
   arbitrarily.

# Language
Write `selected_meaning.meaning` and `note` in Korean (한국어). Keep `unit`/`status` as their existing English
literal conventions.

# Output
Return JSON only:
{
  "selected_meaning": {
    "meaning": "...",
    "unit": null
  },
  "status": "resolved | ambiguous",
  "note": "무엇이 충돌했고 어떻게 판단했는지"
}
