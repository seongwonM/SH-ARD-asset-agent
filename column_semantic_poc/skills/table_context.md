# Role
Create the final table-level semantic context after column interpretation and validation.

# Internal ontology
Use a data-friendly ontology rather than forcing every 5W1H slot:
- row_grain: what one row represents; composite grain is allowed
- entities: business/physical/logical entities that identify the row or provide context
- measures: observations/measurements/events/attributes collected; there may be MANY, not one target
- when: event/start/end/created/updated/validity time roles
- who: operator/author/owner/assignee/etc. only when supported
- where: row-level location and table-level scope; cardinality > 1 is allowed
- how: equipment, channel, collection method, source system, process, etc. only when supported

# Rules
1. Never invent Who/Where/How when absent.
2. Row grain is the first table-level question.
3. Use composite-key candidates and hierarchy evidence.
4. Do not assume a single primary target.
5. Preserve uncertainty explicitly.
6. Write all free-text values (`row_grain.description`, `entities[].role`, `table_scope`, `asset_context`,
   `uncertainties`) in Korean (한국어). `asset_context` specifically must be a concise Korean description suitable
   for Asset Context retrieval.
7. If `semantic_validation.checks` (or `revision_feedback.checks`) still contains `warning`/`fail` entries, reflect
   each one in `uncertainties` by name — state which columns and what concrete contradiction remains (e.g. "genre_dan
   최대값이 0.01로 다른 genre_* 컬럼과 스케일이 달라 동일 기준 확률로 보기 어려움"), not a generic sentence like
   "일부 열의 데이터가 모호할 수 있음".

# Output
Return JSON only:
{
  "row_grain": {
    "description": "...",
    "columns": ["..."],
    "confidence": 0.0
  },
  "entities": [
    {"name": "...", "columns": ["..."], "role": "...", "confidence": 0.0}
  ],
  "measures": [
    {"name": "...", "columns": ["..."], "unit": null, "confidence": 0.0}
  ],
  "when": [],
  "who": [],
  "where": [],
  "how": [],
  "table_scope": "...",
  "asset_context": "한두 문장의 한국어 테이블 설명",
  "uncertainties": ["..."]
}