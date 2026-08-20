"""실행 순서와 병렬화. 파일도 네트워크도 직접 만지지 않는다.

입력은 DataFrame + SkillRunner(어댑터가 주입된 것)뿐이고, 출력은 문서 5벌이다.
파일로 쓰는 일조차 콜백(`on_checkpoint`)으로 빼서, 이 모듈은 "무엇을 어떤
순서로 돌리는가"만 알게 했다 - 그래서 가짜 LLM 하나만 있으면 전 구간이
테스트된다.

구조:

    1차 pass   semantic_type -> column_interpretation(컬럼별 병렬)
               -> relation_analysis(pairwise 증거 있을 때만)
    gap 보충   gap_planner 판단 -> 배정된 (컬럼, skill) 병렬 실행
    검증       관계 그룹별 semantic_validation 병렬 -> probe로 실측 대조
    마무리     table_context
    수정 라운드 검증이 needs_revision이면 replan -> 해당 skill만 재실행 -> 재검증

단계를 지날 때마다 컬럼이 어떻게 바뀌었는지를 `ColumnHistory`에 남긴다. 결과
dict는 최신 상태만 들고 있어서, 기록하지 않으면 "gap 보충이 무엇을 바꿨는지"가
덮어쓰기와 함께 사라진다.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from column_semantics.core.clock import now_iso
from column_semantics.core.evidence import build_table_evidence
from column_semantics.core.history import ColumnHistory
from column_semantics.core.llm_log import LLMLog
from column_semantics.core.probes import apply_probes, with_measurements
from column_semantics.core.relations import build_relation_groups
from column_semantics.core.timeline import Timeline
from column_semantics.pipeline.documents import PARTS, build_documents
from column_semantics.pipeline.plan import SKILL_ORDER, first_pass_skills, revision_steps
from column_semantics.pipeline.skill_runner import SkillRunner

Documents = Dict[str, Dict[str, Any]]


@dataclass
class PipelineConfig:
    max_rounds: int = 2
    max_workers: int = 12
    source_name: str = ""
    # 각 skill이 끝날 때마다 그때까지의 문서 5벌을 넘긴다. 중간에 죽어도 여기까지는 남는다.
    on_checkpoint: Optional[Callable[[Documents], None]] = None
    # 모든 문서의 meta에 그대로 합쳐진다(입력 경로, skills 경로 등 실행 환경 정보).
    meta: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------
# 병렬 실행 단위
# ---------------------------------------------------------------------


def _parallel(max_workers: int, jobs: List[Any], fn: Callable[[Any], Any]) -> Dict[int, Any]:
    """jobs를 병렬 실행하고 {인덱스: 결과}를 돌려준다. 예외는 그대로 올라간다."""
    outputs: Dict[int, Any] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fn, job): i for i, job in enumerate(jobs)}
        for future in as_completed(futures):
            outputs[futures[future]] = future.result()
    return outputs


def interpret_columns_parallel(
    runner: SkillRunner,
    evidence: Dict[str, Any],
    results: Dict[str, Any],
    max_workers: int,
    columns: List[str],
    revision_feedback: Optional[Dict[str, Any]] = None,
    existing: Optional[Dict[str, Any]] = None,
    history: Optional[ColumnHistory] = None,
    stage_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """columns 각각에 대해 column_interpretation을 병렬 호출한다. existing이 있으면
    (재검증 라운드에서 focus로 좁힌 경우) 그 위에 columns만 덮어쓰고 나머지 컬럼은
    이전 라운드 결과를 유지한다."""
    semantic_type_result = results.get("semantic_type")
    merged: Dict[str, Any] = dict(existing or {})

    print(f"[PARALLEL] column_interpretation {len(columns)}개 컬럼 병렬 실행", flush=True)
    outputs = _parallel(
        max_workers,
        columns,
        lambda col: runner.interpret_column(col, evidence, semantic_type_result, revision_feedback),
    )
    for i, col in enumerate(columns):
        before = merged.get(col)
        merged[col] = outputs[i]
        if history is not None:
            _record(history, col, "column_interpretation", before, outputs[i], stage_info)

    return {"columns": merged}


def validate_grouped(
    runner: SkillRunner,
    evidence: Dict[str, Any],
    results: Dict[str, Any],
    max_workers: int,
    revision_feedback: Optional[Dict[str, Any]] = None,
    round_idx: int = 1,
) -> Dict[str, Any]:
    """테이블 전체를 한 번에 넣는 대신, relation_analysis가 확정한 관계 그룹별로
    쪼개서 병렬 검증한다. 어느 그룹에도 안 걸린 컬럼들은 하나의 배치로 묶어
    (컬럼 간 관계가 없으니 단일 컬럼 제약만 검증) 같이 병렬 실행한다."""
    all_columns = evidence["table"]["columns"]
    groups, ungrouped = build_relation_groups(
        all_columns,
        results.get("relation_analysis"),
        evidence.get("grain_candidates", []),
        evidence.get("relation_evidence", {}).get("pairwise", []),
    )

    jobs: List[tuple] = [(f"group{i + 1}", g) for i, g in enumerate(groups)]
    if ungrouped:
        jobs.append(("ungrouped", ungrouped))

    print(
        f"[VALIDATE] {len(jobs)}개 단위 병렬 검증 "
        f"(관계그룹 {len(groups)}개, 단일컬럼 배치 "
        f"{'1개(' + str(len(ungrouped)) + '개 컬럼)' if ungrouped else '없음'})",
        flush=True,
    )

    outputs = _parallel(
        max_workers,
        jobs,
        lambda job: runner.validate_group(
            job[1], evidence, results, revision_feedback, job[0]
        ),
    )

    merged_checks: List[Dict[str, Any]] = []
    merged_requests: List[Dict[str, Any]] = []
    merged_validated: Dict[str, Any] = {}
    any_needs_revision = False
    for out in outputs.values():
        merged_checks.extend(out.get("checks", []) or [])
        merged_requests.extend(out.get("revision_requests", []) or [])
        merged_validated.update(out.get("validated_columns", {}) or {})
        if out.get("overall_status") == "needs_revision":
            any_needs_revision = True

    # 그룹별로 쪼개 돌린 check를 하나의 목록으로 합치면서 라운드 안에서 유일한
    # id를 준다. 컬럼 문서와 probe 실측값이 이 id로 같은 check를 가리킨다.
    for i, check in enumerate(merged_checks, start=1):
        if isinstance(check, dict):
            check["check_id"] = f"r{round_idx}-c{i}"

    return {
        "overall_status": "needs_revision" if any_needs_revision else "pass",
        "checks": merged_checks,
        "revision_requests": merged_requests,
        "validated_columns": merged_validated,
    }


def resolve_gaps(
    runner: SkillRunner,
    evidence: Dict[str, Any],
    results: Dict[str, Any],
    max_workers: int,
    history: Optional[ColumnHistory] = None,
    stage_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """1차 해석 직후 호출. gap_planner가 컬럼별 상태를 보고 무엇을 붙일지 판단하면
    그 배정을 병렬로 실행해서 column_interpretation.columns[col]에 반영한다."""
    diagnosis = runner.diagnose_gaps(evidence, results)
    assignments = diagnosis.get("gap_assignments", [])
    record = {"raw": diagnosis.get("raw"), "assignments": assignments}
    if not assignments:
        print("[GAP] 보충 필요한 컬럼 없음", flush=True)
        return record

    print(
        f"[GAP] {len(assignments)}건 보충 실행: "
        + ", ".join(f"{a['column']}->{a['skill']}" for a in assignments),
        flush=True,
    )

    outputs = _parallel(
        max_workers,
        assignments,
        lambda a: runner.run_gap_skill(
            a["skill"], a["column"], evidence, results, a.get("reason", "")
        ),
    )

    columns = results.setdefault("column_interpretation", {}).setdefault("columns", {})
    for i, a in enumerate(assignments):
        out = outputs.get(i)
        col = a["column"]
        if not isinstance(out, dict) or col not in columns:
            continue
        before = dict(columns[col]) if isinstance(columns[col], dict) else columns[col]
        for key in ("selected_meaning", "status", "sparsity_reason", "note"):
            # None은 "이번엔 안 바꿈"이라는 뜻이라 - 있는 값을 null로 지워버리면 안 된다.
            if out.get(key) is not None:
                columns[col][key] = out[key]
        columns[col].setdefault("gap_history", []).append(
            {"skill": a["skill"], "reason": a.get("reason", "")}
        )
        if history is not None:
            history.record_change(
                col,
                "gap",
                before,
                dict(columns[col]),
                skill=a["skill"],
                reason=a.get("reason", ""),
                skill_output=out,
                **(stage_info or {}),
            )

    return record


# ---------------------------------------------------------------------
# 기록 도우미
# ---------------------------------------------------------------------


def _record(
    history: ColumnHistory,
    column: str,
    stage: str,
    before: Any,
    after: Any,
    stage_info: Optional[Dict[str, Any]] = None,
) -> None:
    """처음 나온 값이면 그대로, 덮어쓴 값이면 before/after로 남긴다."""
    info = stage_info or {}
    if before is None:
        history.record(column, stage, after, **info)
    else:
        history.record_change(column, stage, before, after, **info)


def _record_table_skill_columns(
    history: ColumnHistory,
    skill: str,
    before: Optional[Dict[str, Any]],
    after: Optional[Dict[str, Any]],
    stage_info: Dict[str, Any],
) -> None:
    """테이블 단위 skill이 컬럼별로 낸 값을 컬럼 이력에 흩뿌린다.

    semantic_type은 `columns`, relation_analysis는 `revised_columns`에 컬럼별
    판단을 담는다 - 그 두 개만 컬럼 이력의 재료가 된다.
    """
    key = {"semantic_type": "columns", "relation_analysis": "revised_columns"}.get(skill)
    if key is None:
        return
    previous = (before or {}).get(key) or {}
    current = (after or {}).get(key) or {}
    for col, value in current.items():
        _record(history, col, skill, previous.get(col), value, stage_info)


# ---------------------------------------------------------------------
# 파이프라인
# ---------------------------------------------------------------------


def run_pipeline(
    df: pd.DataFrame,
    runner: SkillRunner,
    config: Optional[PipelineConfig] = None,
    timeline: Optional[Timeline] = None,
    llm_log: Optional[LLMLog] = None,
) -> Documents:
    config = config or PipelineConfig()
    timeline = timeline if timeline is not None else Timeline()
    max_workers = config.max_workers

    run_started = time.time()
    run_started_at = now_iso()

    # 프로파일링/probe는 전부 pandas 연산이라 LLM 비용과 무관하다 - 행을 미리
    # 샘플링해서 넘기면 uniqueness/상관관계/probe 통과율 같은 통계 자체가 부정확해질
    # 뿐, LLM에 보내는 payload는 sample_values()가 컬럼당 12개로 이미 고정돼 있어
    # 줄지 않는다. 그래서 df를 그대로 쓴다.
    with timeline.measure(event="profile"):
        profile_started = time.time()
        evidence = build_table_evidence(df)
    print(f"[PROFILE] 완료 ({time.time() - profile_started:.1f}초)", flush=True)

    if config.source_name:
        evidence["table"]["source_file"] = config.source_name
    all_columns = evidence["table"]["columns"]

    results: Dict[str, Any] = {}
    history = ColumnHistory()
    probe_log: List[Dict[str, Any]] = []
    validation_rounds: List[Dict[str, Any]] = []
    planning: Dict[str, Any] = {"first_pass": {}, "gap_planning": {}, "replans": []}

    def build_docs(status: str, validation_status: str = "not_yet_run") -> Documents:
        return build_documents(
            meta={
                **config.meta,
                "llm_model": getattr(runner.llm, "model", ""),
                "max_rounds": config.max_rounds,
                "status": status,
                "validation_status": validation_status,
                "started_at": run_started_at,
                "finished_at": now_iso(),
                "elapsed_seconds": round(time.time() - run_started, 3),
            },
            evidence=evidence,
            results=results,
            history=history,
            probe_log=probe_log,
            planning=planning,
            validation_rounds=validation_rounds,
            timeline_events=timeline.events(),
            llm_log=llm_log,
        )

    def checkpoint() -> None:
        if config.on_checkpoint is not None:
            config.on_checkpoint(build_docs("in_progress"))

    def exec_table_skill(skill: str, phase: str, round_idx: Optional[int] = None, **kwargs: Any) -> None:
        label = "[EXEC]" if phase == "exec" else "[RE-EXEC]"
        print(f"{label} {skill} 시작", flush=True)
        started = time.time()
        before = results.get(skill)
        with timeline.measure(event="skill", phase=phase, skill=skill, **_round(round_idx)):
            with runner.stage(phase=phase, **_round(round_idx)):
                results[skill] = runner.run_table_skill(skill, evidence, results, **kwargs)
        _record_table_skill_columns(
            history, skill, before, results[skill], _stage_info(phase, round_idx)
        )
        print(f"{label} {skill} 완료 ({time.time() - started:.1f}초)", flush=True)
        checkpoint()

    def exec_columns(phase: str, columns: List[str], round_idx: Optional[int] = None, **kwargs: Any) -> None:
        label = "[EXEC]" if phase == "exec" else "[RE-EXEC]"
        print(f"{label} column_interpretation 시작 ({len(columns)}개 컬럼)", flush=True)
        started = time.time()
        with timeline.measure(
            event="skill", phase=phase, skill="column_interpretation", **_round(round_idx)
        ):
            with runner.stage(phase=phase, **_round(round_idx)):
                results["column_interpretation"] = interpret_columns_parallel(
                    runner,
                    evidence,
                    results,
                    max_workers,
                    columns=columns,
                    history=history,
                    stage_info=_stage_info(phase, round_idx),
                    **kwargs,
                )
        print(f"{label} column_interpretation 완료 ({time.time() - started:.1f}초)", flush=True)
        checkpoint()

    def exec_validation(
        phase: str,
        round_idx: Optional[int] = None,
        revision_feedback: Optional[Dict[str, Any]] = None,
    ) -> None:
        label = "[EXEC]" if phase == "exec" else "[RE-EXEC]"
        round_no = round_idx or 1
        print(f"{label} semantic_validation 시작", flush=True)
        started = time.time()
        with timeline.measure(
            event="skill", phase=phase, skill="semantic_validation", **_round(round_idx)
        ):
            with runner.stage(phase=phase, **_round(round_idx)):
                results["semantic_validation"] = validate_grouped(
                    runner,
                    evidence,
                    results,
                    max_workers,
                    revision_feedback=revision_feedback,
                    round_idx=round_no,
                )
            # 검증 결과의 주장을 실제 데이터에 대고 반증한다. LLM이 "pass"라고 쓴
            # check도 여기서 실측과 어긋나면 fail로 내려간다. 실측값 자체는 check에
            # 남기지 않고 probe_log(rulebase 문서)로 간다.
            probe_started = time.time()
            with timeline.measure(event="probe", skill="semantic_validation", **_round(round_idx)):
                results["semantic_validation"] = apply_probes(
                    df,
                    results["semantic_validation"],
                    probe_log,
                    {"round": round_no, "phase": phase},
                )
            print(f"[PROBE] semantic_validation 검증 완료 ({time.time() - probe_started:.1f}초)", flush=True)
        _record_validation(history, all_columns, results["semantic_validation"], phase, round_no)
        validation_rounds.append(
            {
                "round": round_no,
                "phase": phase,
                "overall_status": results["semantic_validation"].get("overall_status"),
                "checks": results["semantic_validation"].get("checks", []),
                "revision_requests": results["semantic_validation"].get("revision_requests", []),
            }
        )
        print(f"{label} semantic_validation 완료 ({time.time() - started:.1f}초)", flush=True)
        checkpoint()

    # -- 1차 pass ------------------------------------------------------
    has_pairwise = bool(evidence.get("relation_evidence", {}).get("pairwise"))
    first_pass = first_pass_skills(has_pairwise)
    planning["first_pass"] = {
        "skills": first_pass,
        "source": "fixed_order",
        "relation_analysis_included": has_pairwise,
        "reason": (
            "pairwise 증거가 있어 relation_analysis를 포함했다"
            if has_pairwise
            else "pairwise 증거가 없어 relation_analysis를 생략했다"
        ),
    }
    print(
        "[PLAN] 1차 고정 순서:",
        " -> ".join(first_pass),
        "" if has_pairwise else "(pairwise 증거 없어 relation_analysis 생략)",
        flush=True,
    )

    with runner.stage(stage="first_pass", round=1):
        for skill in first_pass:
            if skill == "column_interpretation":
                exec_columns(phase="exec", columns=all_columns)
            else:
                exec_table_skill(skill, phase="exec")

    # -- gap 보충 -------------------------------------------------------
    with timeline.measure(event="gap_resolution") as entry:
        with runner.stage(stage="gap", round=1, phase="exec"):
            gap_record = resolve_gaps(
                runner,
                evidence,
                results,
                max_workers,
                history=history,
                stage_info={"phase": "exec", "round": 1},
            )
        planning["gap_planning"] = gap_record
        entry["assignments"] = len(gap_record.get("assignments", []))
    checkpoint()

    # -- 검증 -> 마무리 --------------------------------------------------
    with runner.stage(stage="validation", round=1):
        exec_validation(phase="exec")
    with runner.stage(stage="first_pass", round=1):
        exec_table_skill("table_context", phase="exec")

    # -- 수정 라운드 -----------------------------------------------------
    # 검증이 needs_revision이면 replan으로 필요한 skill만 다시 돈다. max_rounds까지
    # 반복해도 여전히 실패면 meta.validation_status로 명시한다 - 실패한 채
    # "done"으로만 남기지 않는다.
    rounds_run = 1
    for round_idx in range(2, config.max_rounds + 1):
        rounds_run = round_idx
        validation = results.get("semantic_validation") or {}
        if validation.get("overall_status") != "needs_revision":
            break

        # 재시도 힌트에는 실측값을 붙여서 보낸다 - 반증의 근거가 곧 힌트다.
        failed_checks = [
            x for x in validation.get("checks", []) if x.get("status") in {"warning", "fail"}
        ]
        feedback = {
            "revision_requests": validation.get("revision_requests", []),
            "checks": with_measurements(failed_checks, probe_log),
        }
        with runner.stage(stage="replan", round=round_idx, phase="re-exec"):
            replan = runner.plan(evidence, previous_results=results, validation_feedback=feedback)
        steps = revision_steps(replan["plan"])

        planning["replans"].append(
            {
                "round": round_idx,
                "reason": replan["plan"].get("reason", ""),
                "raw": replan["raw"],
                "steps": steps,
                "trigger": {
                    "failed_checks": [c.get("check_id") for c in failed_checks],
                    "revision_requests": len(feedback["revision_requests"]),
                },
            }
        )
        print(f"[PLAN {round_idx}]", " -> ".join(step["skill"] for step in steps), flush=True)

        for step in steps:
            skill = step["skill"]
            if skill == "column_interpretation":
                exec_columns(
                    phase="re-exec",
                    columns=step.get("focus") or all_columns,
                    round_idx=round_idx,
                    revision_feedback=feedback,
                    existing=(results.get("column_interpretation") or {}).get("columns", {}),
                )
            elif skill == "semantic_validation":
                exec_validation(phase="re-exec", round_idx=round_idx, revision_feedback=feedback)
            else:
                exec_table_skill(
                    skill,
                    phase="re-exec",
                    round_idx=round_idx,
                    focus=step.get("focus", []),
                    revision_feedback=feedback,
                )

        # 수정 후에는 항상 table_context를 다시 만든다.
        exec_table_skill(
            "table_context", phase="re-exec", round_idx=round_idx, revision_feedback=feedback
        )

    final_validation = results.get("semantic_validation") or {}
    validation_status = "pass"
    if final_validation.get("overall_status") == "needs_revision":
        unresolved = [
            c for c in final_validation.get("checks", []) if c.get("status") in {"warning", "fail"}
        ]
        validation_status = "unresolved_after_max_rounds"
        print(
            f"[VALIDATION] max_rounds({config.max_rounds}) 도달 - {rounds_run}라운드까지 돌았지만 "
            f"여전히 needs_revision. 미해결 {len(unresolved)}건: "
            + "; ".join((c.get("hypothesis") or "")[:60] for c in unresolved[:5]),
            flush=True,
        )

    return build_docs("done", validation_status=validation_status)


def _record_validation(
    history: ColumnHistory,
    all_columns: List[str],
    validation: Dict[str, Any],
    phase: str,
    round_idx: int,
) -> None:
    """검증 결과를 컬럼별로 흩뿌린다. check 본문은 table 문서에 있으므로 여기엔
    id와 판정만 남긴다 - 같은 문장을 두 문서에 복사하지 않는다."""
    checks = validation.get("checks", []) or []
    validated = validation.get("validated_columns", {}) or {}
    for col in all_columns:
        related = [
            {
                "check_id": c.get("check_id"),
                "status": c.get("status"),
                "probe_id": c.get("probe_id"),
            }
            for c in checks
            if isinstance(c, dict) and col in _check_columns(c)
        ]
        if not related and col not in validated:
            continue
        history.record(
            col,
            "semantic_validation",
            {"validated": validated.get(col), "checks": related},
            phase=phase,
            round=round_idx,
        )


def _check_columns(check: Dict[str, Any]) -> List[str]:
    """check가 어느 컬럼에 대한 것인지. skill이 `columns`를 빠뜨리면 probe가
    가리키는 컬럼으로 대신한다 - 프롬프트가 필드를 하나 빠뜨렸다고 그 컬럼의
    이력에서 검증 단계가 통째로 사라지면 안 된다."""
    columns = check.get("columns")
    if isinstance(columns, list) and columns:
        return [str(c) for c in columns]
    probe_columns = (check.get("probe") or {}).get("columns")
    if isinstance(probe_columns, dict):
        return [str(v) for v in probe_columns.values()]
    return []


def _round(round_idx: Optional[int]) -> Dict[str, Any]:
    """타임라인/호출 기록용. 1차 pass는 라운드 개념이 없어 필드 자체를 안 넣는다."""
    return {} if round_idx is None else {"round": round_idx}


def _stage_info(phase: str, round_idx: Optional[int]) -> Dict[str, Any]:
    """컬럼 이력용. 여기서는 라운드를 비우지 않는다 - 단계 목록을 시간순으로 읽을 때
    앞부분만 round가 없으면 1차인지 빠진 것인지 구분되지 않는다."""
    return {"phase": phase, "round": round_idx or 1}


__all__ = [
    "PARTS",
    "PipelineConfig",
    "SKILL_ORDER",
    "interpret_columns_parallel",
    "resolve_gaps",
    "run_pipeline",
    "validate_grouped",
]
