"""실행 순서와 병렬화. 파일도 네트워크도 직접 만지지 않는다.

입력은 DataFrame + SkillRunner(어댑터가 주입된 것)뿐이고, 출력은 결과 dict다.
체크포인트 저장조차 콜백(`on_checkpoint`)으로 빼서, 이 모듈은 "무엇을 어떤
순서로 돌리는가"만 알게 했다 - 그래서 가짜 LLM 하나만 있으면 전 구간이
테스트된다.

구조:

    1차 pass   semantic_type -> column_interpretation(컬럼별 병렬)
               -> relation_analysis(pairwise 증거 있을 때만)
    gap 보충   gap_planner 판단 -> 배정된 (컬럼, skill) 병렬 실행
    검증       관계 그룹별 semantic_validation 병렬 -> probe로 실측 대조
    마무리     table_context
    수정 라운드 검증이 needs_revision이면 replan -> 해당 skill만 재실행 -> 재검증
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from column_semantics.core.clock import now_iso
from column_semantics.core.evidence import build_table_evidence
from column_semantics.core.jsonx import clean_for_json
from column_semantics.core.probes import apply_probes
from column_semantics.core.relations import build_relation_groups
from column_semantics.core.timeline import Timeline
from column_semantics.pipeline.plan import SKILL_ORDER, first_pass_skills, revision_steps
from column_semantics.pipeline.skill_runner import SkillRunner


@dataclass
class PipelineConfig:
    max_rounds: int = 2
    max_workers: int = 12
    source_name: str = ""
    # 각 skill이 끝날 때마다 중간 결과를 넘긴다. 중간에 죽어도 여기까지는 남는다.
    on_checkpoint: Optional[Callable[[Dict[str, Any]], None]] = None
    # 결과 meta에 그대로 합쳐진다(입력 경로, skills 경로 등 실행 환경 정보).
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
        merged[col] = outputs[i]

    return {"columns": merged}


def validate_grouped(
    runner: SkillRunner,
    evidence: Dict[str, Any],
    results: Dict[str, Any],
    max_workers: int,
    revision_feedback: Optional[Dict[str, Any]] = None,
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
) -> List[Dict[str, Any]]:
    """1차 해석 직후 호출. gap_planner가 컬럼별 상태를 보고 무엇을 붙일지 판단하면
    그 배정을 병렬로 실행해서 column_interpretation.columns[col]에 반영한다."""
    diagnosis = runner.diagnose_gaps(evidence, results)
    assignments = diagnosis.get("gap_assignments", [])
    if not assignments:
        print("[GAP] 보충 필요한 컬럼 없음", flush=True)
        return []

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
        for key in ("selected_meaning", "status", "sparsity_reason", "note"):
            # None은 "이번엔 안 바꿈"이라는 뜻이라 - 있는 값을 null로 지워버리면 안 된다.
            if out.get(key) is not None:
                columns[col][key] = out[key]
        columns[col].setdefault("gap_history", []).append(
            {"skill": a["skill"], "reason": a.get("reason", "")}
        )

    return assignments


# ---------------------------------------------------------------------
# 파이프라인
# ---------------------------------------------------------------------


def run_pipeline(
    df: pd.DataFrame,
    runner: SkillRunner,
    config: Optional[PipelineConfig] = None,
    timeline: Optional[Timeline] = None,
) -> Dict[str, Any]:
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
    plans: List[Dict[str, Any]] = []

    def build_result(status: str, validation_status: str = "not_yet_run") -> Dict[str, Any]:
        return clean_for_json(
            {
                "meta": {
                    **config.meta,
                    "llm_model": getattr(runner.llm, "model", ""),
                    "max_rounds": config.max_rounds,
                    "status": status,
                    "validation_status": validation_status,
                    "started_at": run_started_at,
                    "finished_at": now_iso(),
                    "elapsed_seconds": round(time.time() - run_started, 3),
                },
                "plans": plans,
                "evidence": evidence,
                "results": results,
                "timeline": timeline.events(),
            }
        )

    def checkpoint() -> None:
        if config.on_checkpoint is not None:
            config.on_checkpoint(build_result("in_progress"))

    def exec_table_skill(skill: str, phase: str, round_idx: Optional[int] = None, **kwargs: Any) -> None:
        label = "[EXEC]" if phase == "exec" else "[RE-EXEC]"
        print(f"{label} {skill} 시작", flush=True)
        started = time.time()
        with timeline.measure(event="skill", phase=phase, skill=skill, **_round(round_idx)):
            results[skill] = runner.run_table_skill(skill, evidence, results, **kwargs)
        print(f"{label} {skill} 완료 ({time.time() - started:.1f}초)", flush=True)
        checkpoint()

    def exec_columns(phase: str, columns: List[str], round_idx: Optional[int] = None, **kwargs: Any) -> None:
        label = "[EXEC]" if phase == "exec" else "[RE-EXEC]"
        print(f"{label} column_interpretation 시작 ({len(columns)}개 컬럼)", flush=True)
        started = time.time()
        with timeline.measure(
            event="skill", phase=phase, skill="column_interpretation", **_round(round_idx)
        ):
            results["column_interpretation"] = interpret_columns_parallel(
                runner, evidence, results, max_workers, columns=columns, **kwargs
            )
        print(f"{label} column_interpretation 완료 ({time.time() - started:.1f}초)", flush=True)
        checkpoint()

    def exec_validation(
        phase: str,
        round_idx: Optional[int] = None,
        revision_feedback: Optional[Dict[str, Any]] = None,
    ) -> None:
        label = "[EXEC]" if phase == "exec" else "[RE-EXEC]"
        print(f"{label} semantic_validation 시작", flush=True)
        started = time.time()
        with timeline.measure(
            event="skill", phase=phase, skill="semantic_validation", **_round(round_idx)
        ):
            results["semantic_validation"] = validate_grouped(
                runner, evidence, results, max_workers, revision_feedback=revision_feedback
            )
            # 검증 결과의 주장을 실제 데이터에 대고 반증한다. LLM이 "pass"라고 쓴
            # check도 여기서 실측과 어긋나면 fail로 내려간다.
            probe_started = time.time()
            with timeline.measure(event="probe", skill="semantic_validation", **_round(round_idx)):
                results["semantic_validation"] = apply_probes(df, results["semantic_validation"])
            print(f"[PROBE] semantic_validation 검증 완료 ({time.time() - probe_started:.1f}초)", flush=True)
        print(f"{label} semantic_validation 완료 ({time.time() - started:.1f}초)", flush=True)
        checkpoint()

    # -- 1차 pass ------------------------------------------------------
    has_pairwise = bool(evidence.get("relation_evidence", {}).get("pairwise"))
    first_pass = first_pass_skills(has_pairwise)
    print(
        "[PLAN] 1차 고정 순서:",
        " -> ".join(first_pass),
        "" if has_pairwise else "(pairwise 증거 없어 relation_analysis 생략)",
        flush=True,
    )

    for skill in first_pass:
        if skill == "column_interpretation":
            exec_columns(phase="exec", columns=all_columns)
        else:
            exec_table_skill(skill, phase="exec")

    # -- gap 보충 -------------------------------------------------------
    with timeline.measure(event="gap_resolution") as entry:
        assignments = resolve_gaps(runner, evidence, results, max_workers)
        entry["assignments"] = len(assignments)
    checkpoint()

    # -- 검증 -> 마무리 --------------------------------------------------
    exec_validation(phase="exec")
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

        feedback = {
            "revision_requests": validation.get("revision_requests", []),
            "checks": [
                x for x in validation.get("checks", []) if x.get("status") in {"warning", "fail"}
            ],
        }
        replan = runner.plan(evidence, previous_results=results, validation_feedback=feedback)
        steps = revision_steps(replan)

        plans.append({"round": round_idx, "reason": replan.get("reason", ""), "steps": steps})
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

    return build_result("done", validation_status=validation_status)


def _round(round_idx: Optional[int]) -> Dict[str, Any]:
    return {} if round_idx is None else {"round": round_idx}


__all__ = [
    "PipelineConfig",
    "SKILL_ORDER",
    "interpret_columns_parallel",
    "resolve_gaps",
    "run_pipeline",
    "validate_grouped",
]
