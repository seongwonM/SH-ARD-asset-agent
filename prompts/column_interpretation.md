# Role
Generate lexical expansions and ranked semantic meaning candidates for a single column (`target_column`). Every
column is interpreted through a separate, parallel call like this one — you only see this one column's own profile,
plus the raw names of every other column for naming-convention context.

# Core principles
1. Split `target_column`'s name by delimiters and naming patterns. Expand ambiguous tokens into candidate full
   words.
2. Do NOT force a table-wide abbreviation dictionary. The same token may mean something different in another
   column — do not mechanically match your expansion to whatever the same token might mean elsewhere. Instead, read
   `raw_other_column_names` as a whole to get a general sense of the table's naming convention (e.g. mostly
   `snake_case` domain abbreviations, a recurring prefix/suffix scheme), and let that general understanding inform
   plausibility, not force agreement with any one specific other column.
3. This is the first, independent pass — you do not have other columns' *interpreted meanings* (only their raw
   names, per rule 2, and the type/value evidence for `target_column` itself). If `target_column` still comes out
   ambiguous, that's fine — a later step re-examines ambiguous columns with full table context.
4. Keep multiple candidates in `meaning_candidates` when evidence is insufficient, but always order them by
   confidence (highest first) and copy the top one's `meaning`/`unit` into `selected_meaning` — downstream steps use
   `selected_meaning` as *the* description, so it must be explicit, not left for the reader to infer from the list.
5. **Say only what the evidence supports, and put what you cannot identify in `domain_gap`.** The profile tells
   you the shape of a column; it does not tell you which process, product or business object it belongs to. When
   that referent is missing, do not pad `selected_meaning` with filler that sounds like an answer — "어떤 공정의
   측정값", "무언가의 코드" carry no information and hide the fact that nobody knows yet. Write the part you can
   defend ("장비별로 기록되는 압력 계열 수치, 단위 미상"), and move the rest into `domain_gap`: what is missing,
   why this data cannot settle it, and what source would. If you can identify the referent, set `domain_gap` to
   null.
6. **Structural resolution and domain identification are different axes.** `status: "resolved"` means the naming
   and value evidence picked one candidate over the others; it does not mean you know what the column is in
   business terms. A column can be `resolved` and still carry a `domain_gap`, and that combination is an honest,
   useful answer - far more useful than an ambiguous status attached to a vague sentence.
7. State units/scale only when supported by data/name/context. Never guess seconds/minutes merely because the
   column is temporal/numeric — same for percent/ratio: don't assume a 0-100 or 0-1 scale by convention, check
   `column_profile`'s own observed min/max and state which scale it actually is (e.g. `unit: "percent_0_100"` vs
   `"fraction_0_1"`). `semantic_validation` will test row values against exactly the scale you state here, so a
   wrong guess here becomes a false "this isn't actually a percent" failure downstream.
8. "High confidence" is not "ground truth"; leave evidence trails.
9. If `revision_feedback.checks` is present, it may include entries about columns other than `target_column` — only
   act on ones that actually name `target_column`. For those, treat the entry as a falsified hypothesis (the
   contradiction is in `measured` - the executor's actual measurement - vs `expected_constraint`): either (a) pick
   a different meaning_candidate (and update `selected_meaning` to match) consistent with `measured`, (b) add the
   contradiction to `counter_evidence`
   and lower confidence, or (c) mark `status: "ambiguous"` if no candidate fits. Say explicitly in `evidence` that
   this was revised because of validation feedback. If no entry names `target_column`, ignore `revision_feedback`
   entirely and interpret normally.

# Language
Write `meaning`, `evidence`, and `counter_evidence` in Korean (한국어). `expansions[].word` may stay in whatever
language the expanded token actually is (an English abbreviation expands to an English word; do not force-translate it).

# Output
Return JSON only — this is `target_column`'s interpretation directly, not wrapped in a column-name key:
{
  "tokens": [
    {
      "token": "chg",
      "expansions": [
        {"word": "change", "confidence": 0.45},
        {"word": "charge", "confidence": 0.45}
      ]
    }
  ],
  "meaning_candidates": [
    {
      "meaning": "...",
      "confidence": 0.0,
      "unit": null,
      "evidence": ["..."],
      "counter_evidence": ["..."]
    }
  ],
  "selected_meaning": {
    "meaning": "... (copy of the top meaning_candidates[0].meaning, always set)",
    "unit": null
  },
  "domain_gap": {
    "missing": "무엇을 식별하지 못했는가 (예: 어느 공정의 어떤 물리량인지)",
    "why": "이 데이터만으로 정할 수 없는 이유",
    "would_resolve": ["이걸 풀어줄 자료 (공통코드 표, 컬럼 코멘트, 단위 표기 등)"]
  },
  "status": "resolved | ambiguous"
}

Set `domain_gap` to null when the column's real-world referent is identified. Keep `would_resolve` concrete -
name the kind of document or table that would answer it, not "더 많은 정보".
