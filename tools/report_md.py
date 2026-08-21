"""결과 문서 5벌을 사람이 읽는 Markdown 한 장으로 옮긴다.

`columns` / `rulebase` / `plan` / `table` / `llm_calls`는 **출처가 다른 것을
갈라 담기 위해** 나뉘어 있고, 교차 참조는 id로만 한다(`check_id`, `probe_id`).
저장 구조로는 옳지만 읽을 때는 사람이 매번 손으로 조인해야 한다 - 이 스크립트가
하는 일이 그 조인이다. "이 check가 실제로 뭘 쟀는지", "이 컬럼에 무슨 판정이
붙었는지"가 한자리에 오게 만든다.

**여기서 새 값을 만들지 않는다.** 세는 것(몇 개가 resolved인지)까지가 전부이고,
문서에 없는 수치를 계산하거나 추정하지 않는다 - 보고서가 결과와 다른 말을 하기
시작하면 어느 쪽이 맞는지 가릴 방법이 없다. 값이 비어 있으면 비어 있다고 쓴다.

    python tools/report_md.py results/20260820_1200_modelA/my_table/
    python tools/report_md.py results/ --all          # 실행마다 report.md + index.md
    python tools/report_md.py <경로> -o out.md --include-calls
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

PARTS = ["columns", "rulebase", "plan", "table", "llm_calls"]


# ------------------------------------------------------------------ 불러오기

def resolve_base(target: Path) -> Optional[Path]:
    """폴더든 기준 경로든 `<base>.<part>.json`의 base를 찾아낸다."""
    if target.is_dir():
        found = sorted(target.glob("*.columns.json"))
        if not found:
            return None
        return Path(str(found[0])[: -len(".columns.json")])
    name = str(target)
    for suffix in (".columns.json", ".rulebase.json", ".plan.json", ".table.json", ".llm_calls.json"):
        if name.endswith(suffix):
            return Path(name[: -len(suffix)])
    if name.endswith(".json"):
        return Path(name[: -len(".json")])
    return target


def load_documents(base: Path) -> Dict[str, Dict[str, Any]]:
    """있는 문서만 읽는다. 중간에 죽은 실행은 일부만 있을 수 있다."""
    docs: Dict[str, Dict[str, Any]] = {}
    for part in PARTS:
        path = Path(f"{base}.{part}.json")
        if not path.exists():
            continue
        try:
            docs[part] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            # 체크포인트 도중에 죽으면 잘린 JSON이 남을 수 있다. 없는 것과
            # 깨진 것은 다른 사실이라 갈라서 남긴다.
            docs[part] = {"_read_error": f"{type(e).__name__}: {e}"}
    return docs


# -------------------------------------------------------------------- 서식

def esc(value: Any, limit: int = 0) -> str:
    """표 칸에 넣을 수 있게 한 줄로 만든다. limit을 주면 잘라낸다."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, float):
        text = f"{value:.4g}"
    elif isinstance(value, (list, tuple)):
        text = ", ".join(esc(v) for v in value)
    elif isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    text = text.replace("\n", " ").replace("|", "\\|").strip()
    if limit and len(text) > limit:
        text = text[: limit - 1] + "…"
    return text or "-"


def table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> List[str]:
    body = [list(r) for r in rows]
    if not body:
        return ["_(없음)_", ""]
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    out += ["| " + " | ".join(esc(c) for c in r) + " |" for r in body]
    out.append("")
    return out


def get(obj: Any, *path: str, default: Any = None) -> Any:
    """중첩 dict를 안전하게 판다. 어느 단계든 dict가 아니면 default."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def semantic_type_of(interpretation: Any) -> Optional[str]:
    """semantic_type은 {type, confidence, ...} 객체다. 문자열로 온 것도 받아준다."""
    st = get(interpretation, "semantic_type")
    if isinstance(st, dict):
        return st.get("type")
    return st if isinstance(st, str) else None


def format_observed(observed: Any) -> str:
    """probe 실측값을 한 줄로. 문서에 있는 값만 그대로 옮긴다."""
    if not isinstance(observed, dict):
        return "-"
    expr = observed.get("expression", "?")
    n = observed.get("n")
    head = f"`{expr}` (n={n:,})" if isinstance(n, int) else f"`{expr}`"
    if "true_ratio" in observed:
        return f"{head} 참 {observed['true_ratio']:.1%}"
    parts = []
    for key in ("median", "min", "max"):
        if key in observed:
            parts.append(f"{key}={esc(observed[key])}")
    if "within_tolerance_ratio" in observed:
        parts.append(
            f"target {esc(observed.get('target'))}±{esc(observed.get('tolerance'))} "
            f"이내 {observed['within_tolerance_ratio']:.1%}"
        )
    return f"{head} " + (", ".join(parts) if parts else "-")


STATUS_MARK = {"pass": "✅", "warning": "⚠️", "fail": "❌"}


# ------------------------------------------------------------------ 각 절

def section_run(meta: Dict[str, Any], docs: Dict[str, Dict[str, Any]]) -> List[str]:
    out = ["## 실행", ""]
    status = meta.get("status")
    if status != "done":
        out += [
            f"> ⚠️ **status: `{esc(status)}`** — 완주하지 않은 실행이다. "
            "여기 없는 컬럼/단계는 아직 돌지 않은 것이지 결과가 없는 게 아니다.",
            "",
        ]
    if meta.get("error"):
        out += [f"> 실패 원인: `{esc(meta['error'])}`", ""]
    if meta.get("validation_status") == "unresolved_after_max_rounds":
        out += ["> ⚠️ 검증이 max_rounds까지 돌고도 needs_revision으로 끝났다.", ""]

    rows = [
        ("모델", meta.get("llm_model")),
        ("CSV", meta.get("source_csv")),
        ("크기", f"{meta.get('row_count', '?')}행 × {meta.get('column_count', '?')}열"),
        ("상태", f"{esc(status)} / 검증 {esc(meta.get('validation_status'))}"),
        ("소요", f"{esc(meta.get('elapsed_seconds'))}초"),
        ("시각", f"{esc(meta.get('started_at'))} → {esc(meta.get('finished_at'))}"),
        (
            "예산",
            f"max_rounds={esc(meta.get('max_rounds'))}, "
            f"gap_rounds={esc(meta.get('max_gap_rounds'))}, "
            f"actions/col={esc(meta.get('max_actions_per_column'))}, "
            f"group_cols={esc(meta.get('max_group_columns'))}",
        ),
        ("엔드포인트", meta.get("llm_endpoint")),
    ]
    out += table(["항목", "값"], rows)

    missing = [p for p in PARTS if p not in docs]
    broken = [p for p, d in docs.items() if "_read_error" in d]
    if missing:
        out += [f"없는 문서: {', '.join(missing)}", ""]
    if broken:
        out += [f"읽지 못한 문서: {', '.join(broken)}", ""]
    return out


def section_summary(docs: Dict[str, Dict[str, Any]]) -> List[str]:
    columns = get(docs.get("columns"), "columns", default={}) or {}
    probes = get(docs.get("rulebase"), "probes", default=[]) or []
    plan = docs.get("plan") or {}
    calls = get(docs.get("llm_calls"), "calls", default=[]) or []

    resolved = sum(
        1 for c in columns.values() if get(c, "final", "interpretation", "status") == "resolved"
    )
    ambiguous = sum(
        1 for c in columns.values() if get(c, "final", "interpretation", "status") == "ambiguous"
    )
    with_gap = sum(
        1 for c in columns.values() if get(c, "final", "interpretation", "domain_gap") is not None
    )
    no_interp = sum(1 for c in columns.values() if get(c, "final", "interpretation") is None)

    checks = _all_checks(docs)
    by_status = {s: sum(1 for c in checks if c.get("status") == s) for s in ("pass", "warning", "fail")}
    measured = [p for p in probes if p.get("observed")]
    not_evaluable = [p for p in probes if not p.get("observed")]

    gap_rounds = plan.get("gap_rounds") or []
    flagged = sum(len(r.get("flagged") or []) for r in gap_rounds)
    actions = sum(len(r.get("actions") or []) for r in gap_rounds)
    dropped = sum(len(r.get("dropped") or []) for r in gap_rounds)
    changed = sum(len(r.get("changed") or []) for r in gap_rounds)

    tokens = sum(c.get("tokens") or 0 for c in calls)
    failed_calls = sum(1 for c in calls if c.get("status") == "error")

    lines = [
        "## 한눈에",
        "",
        f"- **컬럼 {len(columns)}개** — resolved {resolved} / ambiguous {ambiguous}"
        + (f" / 해석 없음 {no_interp}" if no_interp else ""),
        f"- **도메인 미상 {with_gap}개** — 구조는 정해졌어도 실제로 무엇인지 모르는 컬럼 "
        "(`domain_gap`이 채워진 것)",
        f"- **검증** {esc(get(docs.get('table'), 'validation', 'final_status'))} — "
        f"check {len(checks)}건 (통과 {by_status['pass']} / 경고 {by_status['warning']} / 실패 {by_status['fail']})",
        f"- **probe** 실측 {len(measured)}건, 평가 불가 {len(not_evaluable)}건 "
        "(평가 불가는 반증이 아니다)",
        f"- **보완** 검토가 넘긴 컬럼 {flagged}개 → 실행 {actions}건 / 버림 {dropped}건 / 실제 변경 {changed}건",
        f"- **LLM 호출** {len(calls)}회"
        + (f", 토큰 {tokens:,}" if tokens else "")
        + (f", 실패 {failed_calls}회" if failed_calls else ""),
        "",
    ]
    return lines


def section_table_context(docs: Dict[str, Dict[str, Any]]) -> List[str]:
    doc = docs.get("table") or {}
    ctx = doc.get("table_context") or {}
    out = ["## 테이블", ""]
    if not ctx:
        return out + ["_(table_context 없음)_", ""]

    if ctx.get("asset_context"):
        out += ["> " + esc(ctx["asset_context"]), ""]
    grain = ctx.get("row_grain") or {}
    if grain:
        out += [
            f"**행 단위(grain)** — {esc(grain.get('description'))} "
            f"`{esc(grain.get('columns'))}` (confidence {esc(grain.get('confidence'))})",
            "",
        ]
    for key, label in (("entities", "엔티티"), ("measures", "측정값")):
        items = ctx.get(key) or []
        if items:
            out += [f"**{label}**", ""]
            out += table(
                ["이름", "컬럼", "비고", "confidence"],
                [
                    (
                        i.get("name"),
                        i.get("columns"),
                        i.get("role") or i.get("unit"),
                        i.get("confidence"),
                    )
                    for i in items
                    if isinstance(i, dict)
                ],
            )
    if ctx.get("table_scope"):
        out += [f"**범위** — {esc(ctx['table_scope'])}", ""]
    if ctx.get("uncertainties"):
        out += ["**남은 불확실성**", ""] + [f"- {esc(u)}" for u in ctx["uncertainties"]] + [""]
    return out


def section_columns(docs: Dict[str, Dict[str, Any]], detail: bool) -> List[str]:
    columns = get(docs.get("columns"), "columns", default={}) or {}
    profiles = get(docs.get("rulebase"), "column_profiles", default={}) or {}
    checks_by_column = _checks_by_column(docs)
    probe_by_id = _probe_by_id(docs)

    out = ["## 컬럼", ""]
    if not columns:
        return out + ["_(columns 문서 없음)_", ""]

    rows = []
    for name, entry in columns.items():
        interp = get(entry, "final", "interpretation")
        gap = get(interp, "domain_gap")
        related = checks_by_column.get(name, [])
        worst = _worst_status(related)
        rows.append(
            (
                name,
                semantic_type_of(interp),
                get(interp, "status"),
                esc(get(interp, "selected_meaning", "meaning"), 60),
                get(interp, "selected_meaning", "unit"),
                "예" if gap else "-",
                f"{STATUS_MARK.get(worst, '')} {worst}" if worst else "-",
            )
        )
    out += table(["컬럼", "타입", "status", "의미", "단위", "gap", "검증"], rows)

    if not detail:
        return out

    out += ["### 컬럼별 상세", ""]
    for name, entry in columns.items():
        out += _column_detail(name, entry, profiles.get(name), checks_by_column.get(name, []), probe_by_id)
    return out


def _column_detail(
    name: str,
    entry: Dict[str, Any],
    profile: Optional[Dict[str, Any]],
    related_checks: List[Dict[str, Any]],
    probe_by_id: Dict[str, Dict[str, Any]],
) -> List[str]:
    interp = get(entry, "final", "interpretation")
    out = [f"#### `{name}`", ""]
    if interp is None:
        out += ["_(해석 없음 - 이 컬럼까지 돌지 못했다)_", ""]
    else:
        out += [
            f"- **의미**: {esc(get(interp, 'selected_meaning', 'meaning'))}"
            + (f" (단위 {esc(get(interp, 'selected_meaning', 'unit'))})" if get(interp, "selected_meaning", "unit") else ""),
            f"- **타입**: {esc(semantic_type_of(interp))} / **status**: {esc(interp.get('status'))}",
        ]
        evidence = get(interp, "meaning_candidates", default=[])
        top = evidence[0] if isinstance(evidence, list) and evidence else {}
        if get(top, "evidence"):
            out.append(f"- **근거**: {esc(get(top, 'evidence'))}")
        if get(top, "counter_evidence"):
            out.append(f"- **반대 증거**: {esc(get(top, 'counter_evidence'))}")
        gap = interp.get("domain_gap")
        if isinstance(gap, dict):
            out += [
                f"- **도메인 미상**: {esc(gap.get('missing'))}",
                f"  - 이 데이터로 안 되는 이유: {esc(gap.get('why'))}",
                f"  - 풀려면 필요한 자료: {esc(gap.get('would_resolve'))}",
            ]
        out.append("")

    if profile:
        out += [
            "- **프로파일**(측정값): "
            f"{esc(profile.get('physical_type'))}, "
            f"null {esc(profile.get('null_ratio'))}, "
            f"고유값 {esc(profile.get('nunique'))}, "
            f"샘플 {esc(profile.get('sample_values'), 80)}",
            "",
        ]

    if related_checks:
        out += ["**검증**", ""]
        out += table(
            ["check", "상태", "가설", "실측(probe)"],
            [
                (
                    c.get("check_id"),
                    f"{STATUS_MARK.get(c.get('status'), '')} {esc(c.get('status'))}",
                    esc(c.get("hypothesis"), 80),
                    _probe_cell(c.get("probe_id"), probe_by_id),
                )
                for c in related_checks
            ],
        )

    stages = entry.get("stages") or []
    if stages:
        out += ["**단계 이력**", ""]
        for s in stages:
            out.append(f"- {_stage_line(s)}")
        out.append("")
    return out


def _stage_line(s: Dict[str, Any]) -> str:
    stage = s.get("stage")
    if s.get("skill"):
        stage = f"{stage}:{s['skill']}"
    where = f"r{esc(s.get('round'))}"
    if s.get("phase"):
        where += f"/{s['phase']}"
    line = f"`{stage}` ({where})"

    if "changed" in s:
        changed = s.get("changed") or []
        line += f" — 바뀐 필드: {esc(changed) if changed else '없음(단계는 돌았다)'}"
        after_meaning = get(s, "after", "selected_meaning", "meaning")
        if changed and after_meaning:
            line += f" → {esc(after_meaning, 60)}"
    else:
        value = s.get("value")
        if isinstance(value, dict) and "verdict" in value:
            line += f" — {esc(value.get('verdict'))}"
            if value.get("gap"):
                line += f": {esc(value.get('gap'), 70)}"
        elif isinstance(value, dict) and "checks" in value:
            marks = " ".join(
                f"{STATUS_MARK.get(c.get('status'), '')}{esc(c.get('check_id'))}"
                for c in value.get("checks") or []
            )
            line += f" — {marks or '-'}"
        elif isinstance(value, dict) and "error" in value:
            line += f" — ❌ {esc(value.get('error'), 80)}"
        elif isinstance(value, dict) and "selected_meaning" in value:
            line += f" — {esc(value.get('selected_meaning'), 60)}"
    if s.get("with_columns"):
        line += f" [함께 본 컬럼: {esc(s['with_columns'])}]"
    if s.get("reason"):
        line += f" (이유: {esc(s['reason'], 60)})"
    return line


def section_relations(docs: Dict[str, Dict[str, Any]]) -> List[str]:
    doc = docs.get("table") or {}
    rel = doc.get("relation_analysis") or {}
    joint = doc.get("joint_findings") or []
    probe_by_id = _probe_by_id(docs)

    out = ["## 관계", ""]
    groups = rel.get("groups") or []
    if groups:
        out += ["**그룹**", ""]
        out += table(
            ["유형", "컬럼", "해석", "confidence"],
            [(g.get("type"), g.get("columns"), esc(g.get("interpretation"), 80), g.get("confidence")) for g in groups],
        )
    relations = rel.get("relations") or []
    if relations:
        out += ["**쌍 관계**", ""]
        out += table(
            ["컬럼", "관계", "근거", "confidence"],
            [(r.get("columns"), esc(r.get("relation"), 60), esc(r.get("evidence"), 60), r.get("confidence")) for r in relations],
        )
    if joint:
        out += ["**묶어 본 결과(joint_interpretation)**", ""]
        out += table(
            ["컬럼", "관계", "이유", "실측(probe)"],
            [
                (
                    j.get("columns"),
                    esc(j.get("relationship"), 70),
                    esc(j.get("reason"), 50),
                    _probe_cell(j.get("probe_id"), probe_by_id),
                )
                for j in joint
            ],
        )
    if not (groups or relations or joint):
        out += ["_(관계 산출물 없음 - pairwise 증거가 없으면 relation_analysis 자체가 생략된다)_", ""]
    return out


def section_validation(docs: Dict[str, Dict[str, Any]]) -> List[str]:
    rounds = get(docs.get("table"), "validation", "rounds", default=[]) or []
    probe_by_id = _probe_by_id(docs)
    out = ["## 검증", ""]
    if not rounds:
        return out + ["_(검증 라운드 없음)_", ""]

    for r in rounds:
        out += [
            f"### 라운드 {esc(r.get('round'))} ({esc(r.get('phase'))}) — {esc(r.get('overall_status'))}",
            "",
        ]
        out += table(
            ["check", "상태", "심각도", "컬럼", "가설", "실측(probe)"],
            [
                (
                    c.get("check_id"),
                    f"{STATUS_MARK.get(c.get('status'), '')} {esc(c.get('status'))}",
                    c.get("severity"),
                    c.get("columns"),
                    esc(c.get("hypothesis"), 70),
                    _probe_cell(c.get("probe_id"), probe_by_id),
                )
                for c in r.get("checks") or []
            ],
        )
        requests = r.get("revision_requests") or []
        if requests:
            out += ["**수정 요청**", ""]
            out += table(
                ["컬럼", "문제", "제안 단계"],
                [(q.get("columns"), esc(q.get("issue"), 80), q.get("suggested_stages")) for q in requests],
            )
    return out


def section_plan(docs: Dict[str, Dict[str, Any]]) -> List[str]:
    plan = docs.get("plan") or {}
    out = ["## 무엇을 왜 돌렸나", ""]

    first = plan.get("first_pass") or {}
    if first:
        out += [
            f"**1차 고정 순서** — {' → '.join(first.get('stages') or [])}",
            "",
            f"- relation_analysis 포함: {esc(first.get('relation_analysis_included'))} "
            f"({esc(first.get('reason'))})",
            "",
        ]

    for r in plan.get("gap_rounds") or []:
        out += [f"### 보완 라운드 {esc(r.get('round'))}", ""]
        out += [
            f"- 검토 {len(r.get('reviewed') or [])}개 → 넘어감 {len(r.get('flagged') or [])}개: "
            f"{esc(r.get('flagged'), 120)}",
        ]
        if r.get("malformed_reviews"):
            out.append(
                f"- ⚠️ 형식을 못 맞춘 검토 응답 {len(r['malformed_reviews'])}건 "
                f"({esc(r['malformed_reviews'], 80)}) — 전부 pass로 처리됐다"
            )
        out.append(f"- 실제 바뀐 컬럼: {esc(r.get('changed'), 120)}")
        out.append("")
        if r.get("actions"):
            out += ["**실행한 행동**", ""]
            out += table(
                ["행동", "컬럼", "이유"],
                [(a.get("action"), a.get("columns"), esc(a.get("reason"), 80)) for a in r["actions"]],
            )
        if r.get("dropped"):
            out += ["**버린 행동**(planner가 하려 했지만 실행 불가)", ""]
            out += table(
                ["행동", "컬럼", "버린 이유"],
                [(d.get("action"), d.get("columns"), esc(d.get("why"), 60)) for d in r["dropped"]],
            )

    for r in plan.get("replans") or []:
        out += [f"### 재계획 라운드 {esc(r.get('round'))}", ""]
        out += [
            f"- 계기: 실패한 check {esc(get(r, 'trigger', 'failed_checks'))}, "
            f"수정 요청 {esc(get(r, 'trigger', 'revision_requests'))}건",
            f"- 이유: {esc(r.get('reason'))}",
            f"- 다시 돈 단계: {' → '.join(s.get('stage', '') for s in r.get('steps') or [])}",
            "",
        ]

    events = plan.get("execution") or []
    stage_events = [e for e in events if e.get("event") == "stage"]
    if stage_events:
        out += ["### 단계별 소요", ""]
        out += table(
            ["단계", "phase", "라운드", "초"],
            [
                (e.get("stage"), e.get("phase"), e.get("round"), e.get("elapsed_seconds"))
                for e in stage_events
            ],
        )
    return out


def section_probes(docs: Dict[str, Dict[str, Any]]) -> List[str]:
    probes = get(docs.get("rulebase"), "probes", default=[]) or []
    out = ["## probe 실측", ""]
    if not probes:
        return out + ["_(probe 없음)_", ""]
    out += table(
        ["probe", "출처", "check", "실측", "평가 불가 사유"],
        [
            (
                p.get("probe_id"),
                p.get("source") or "semantic_validation",
                p.get("check_id"),
                format_observed(p.get("observed")),
                esc(p.get("not_evaluable"), 70),
            )
            for p in probes
        ],
    )
    not_evaluable = [p for p in probes if not p.get("observed")]
    if not_evaluable:
        out += [
            f"평가하지 못한 probe {len(not_evaluable)}건. **이것은 반증이 아니다** — "
            "해당 check의 상태는 LLM이 쓴 값 그대로 남아 있다.",
            "",
        ]
    return out


def section_calls(docs: Dict[str, Dict[str, Any]], include_raw: bool) -> List[str]:
    calls = get(docs.get("llm_calls"), "calls", default=[]) or []
    out = ["## LLM 호출", ""]
    if not calls:
        return out + ["_(기록 없음)_", ""]

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for c in calls:
        grouped.setdefault(str(c.get("prompt_ref") or c.get("label") or "?"), []).append(c)

    out += table(
        ["프롬프트", "호출", "실패", "입력자", "출력자", "토큰", "초"],
        [
            (
                name,
                len(items),
                sum(1 for c in items if c.get("status") == "error") or "-",
                sum(c.get("input_chars") or 0 for c in items),
                sum(c.get("output_chars") or 0 for c in items),
                sum(c.get("tokens") or 0 for c in items) or "-",
                round(sum(c.get("elapsed_seconds") or 0 for c in items), 1),
            )
            for name, items in grouped.items()
        ],
    )

    errors = [c for c in calls if c.get("status") == "error"]
    if errors:
        out += ["**실패한 호출**", ""]
        out += table(
            ["seq", "대상", "라운드", "시도", "오류"],
            [
                (
                    c.get("seq"),
                    c.get("label"),
                    c.get("round"),
                    c.get("attempt"),
                    esc(c.get("error"), 80),
                )
                for c in errors
            ],
        )

    if include_raw:
        out += ["### 호출 원문", ""]
        for c in calls:
            out += [
                f"<details><summary>#{esc(c.get('seq'))} {esc(c.get('label'))} "
                f"({esc(c.get('status'))})</summary>",
                "",
                "입력:",
                "",
                "```json",
                json.dumps(c.get("input"), ensure_ascii=False, indent=2),
                "```",
                "",
                "출력:",
                "",
                "```json",
                json.dumps(c.get("output"), ensure_ascii=False, indent=2),
                "```",
                "",
                "</details>",
                "",
            ]
    return out


# ------------------------------------------------------------------ 조인

def _all_checks(docs: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    rounds = get(docs.get("table"), "validation", "rounds", default=[]) or []
    if not rounds:
        return []
    # 마지막 라운드가 그 컬럼에 대한 최종 판정이다. 라운드마다 세면 같은 지적이
    # 두 번 잡힌다.
    return [c for c in (rounds[-1].get("checks") or []) if isinstance(c, dict)]


def _checks_by_column(docs: Dict[str, Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for check in _all_checks(docs):
        columns = check.get("columns")
        if not isinstance(columns, list) or not columns:
            probe_columns = get(check, "probe", "columns")
            columns = list(probe_columns.values()) if isinstance(probe_columns, dict) else []
        for col in columns:
            out.setdefault(str(col), []).append(check)
    return out


def _probe_by_id(docs: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    probes = get(docs.get("rulebase"), "probes", default=[]) or []
    return {str(p.get("probe_id")): p for p in probes if p.get("probe_id")}


def _probe_cell(probe_id: Any, probe_by_id: Dict[str, Dict[str, Any]]) -> str:
    if not probe_id:
        return "probe 없음"
    probe = probe_by_id.get(str(probe_id))
    if probe is None:
        return f"{esc(probe_id)} (기록 없음)"
    if probe.get("observed"):
        return format_observed(probe["observed"])
    return f"재보지 못함: {esc(probe.get('not_evaluable'), 60)}"


def _worst_status(checks: Sequence[Dict[str, Any]]) -> Optional[str]:
    for status in ("fail", "warning", "pass"):
        if any(c.get("status") == status for c in checks):
            return status
    return None


# ------------------------------------------------------------------ 조립

def render(docs: Dict[str, Dict[str, Any]], title: str, include_calls: bool, detail: bool) -> str:
    meta = next((d.get("meta") or {} for d in docs.values() if isinstance(d.get("meta"), dict)), {})
    lines = [f"# {title}", ""]
    lines += section_run(meta, docs)
    lines += section_summary(docs)
    lines += section_table_context(docs)
    lines += section_columns(docs, detail)
    lines += section_relations(docs)
    lines += section_validation(docs)
    lines += section_probes(docs)
    lines += section_plan(docs)
    lines += section_calls(docs, include_calls)
    return "\n".join(lines).rstrip() + "\n"


def title_for(base: Path, docs: Dict[str, Dict[str, Any]]) -> str:
    meta = next((d.get("meta") or {} for d in docs.values() if isinstance(d.get("meta"), dict)), {})
    csv_name = Path(str(meta.get("source_csv") or base.parent.name)).name
    model = meta.get("llm_model") or "?"
    return f"{csv_name} — {model}"


def index_row(base: Path, docs: Dict[str, Dict[str, Any]], report: Path, root: Path) -> Tuple:
    meta = next((d.get("meta") or {} for d in docs.values() if isinstance(d.get("meta"), dict)), {})
    columns = get(docs.get("columns"), "columns", default={}) or {}
    resolved = sum(
        1 for c in columns.values() if get(c, "final", "interpretation", "status") == "resolved"
    )
    link = report.relative_to(root).as_posix()
    return (
        f"[{Path(str(meta.get('source_csv') or base.parent.name)).name}]({link})",
        meta.get("llm_model"),
        meta.get("status"),
        meta.get("validation_status"),
        f"{resolved}/{len(columns)}",
        meta.get("elapsed_seconds"),
    )


# --------------------------------------------------------------------- CLI

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="결과 문서 5벌을 Markdown 한 장으로 옮긴다")
    ap.add_argument("target", type=Path, help="결과 폴더 또는 기준 경로")
    ap.add_argument("-o", "--output", type=Path, help="출력 MD 경로 (기본: 결과 폴더의 report.md)")
    ap.add_argument(
        "--all",
        action="store_true",
        help="target 아래 모든 실행을 훑어 각각 report.md와 index.md를 만든다",
    )
    ap.add_argument("--include-calls", action="store_true", help="LLM 호출 원문까지 싣는다(길어진다)")
    ap.add_argument("--no-detail", action="store_true", help="컬럼별 상세 절을 뺀다")
    args = ap.parse_args(argv)

    if args.all:
        return _render_all(args)

    base = resolve_base(args.target)
    if base is None or not Path(f"{base}.columns.json").exists():
        print(f"결과 문서를 찾지 못했습니다: {args.target}", file=sys.stderr)
        return 2
    docs = load_documents(base)
    output = args.output or base.parent / "report.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render(docs, title_for(base, docs), args.include_calls, not args.no_detail),
        encoding="utf-8",
    )
    print(f"{output}", file=sys.stderr)
    return 0


def _render_all(args) -> int:
    root = args.target
    bases = sorted(
        Path(str(p)[: -len(".columns.json")]) for p in root.rglob("*.columns.json")
    )
    if not bases:
        print(f"결과 문서를 찾지 못했습니다: {root}", file=sys.stderr)
        return 2

    rows = []
    for base in bases:
        docs = load_documents(base)
        report = base.parent / "report.md"
        report.write_text(
            render(docs, title_for(base, docs), args.include_calls, not args.no_detail),
            encoding="utf-8",
        )
        rows.append(index_row(base, docs, report, root))
        print(f"{report}", file=sys.stderr)

    index = root / "index.md"
    lines = [f"# 결과 목록 ({len(rows)}건)", ""]
    lines += table(["CSV", "모델", "상태", "검증", "resolved", "초"], rows)
    index.write_text("\n".join(lines), encoding="utf-8")
    print(f"{index}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
