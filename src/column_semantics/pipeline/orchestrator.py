"""실행 순서와 병렬화. 파일도 네트워크도 직접 만지지 않는다.

입력은 DataFrame + StageRunner(어댑터가 주입된 것)뿐이고, 출력은 문서 6벌이다.
파일로 쓰는 일조차 콜백(`on_checkpoint`)으로 빼서, 이 모듈은 "무엇을 어떤
순서로 돌리는가"만 알게 했다 - 그래서 가짜 LLM 하나만 있으면 전 구간이
테스트된다.

구조:

    1차 pass   column_interpretation(컬럼별 병렬, 타입+의미를 한 번에) [고정 단계]
    gap 보충   domain_gap이 남은 컬럼이 있을 때만 gap_planner -> 배정 병렬 실행 [보완 skill]
    관계       relation_analysis(pairwise 증거 있을 때만)             [고정 단계]
    검증       관계 그룹별 semantic_validation 병렬 -> probe로 실측 대조 [고정 단계]
    마무리     table_context                                          [고정 단계]
    수정 라운드 검증이 needs_revision이면 replan -> 해당 단계만 재실행 -> 재검증

여기서 LLM에게 "무엇을 돌릴까"를 묻는 곳은 gap 보충 하나뿐이다. 나머지는 전부
코드가 정한 순서다 - 무조건 만들어야 하는 산출물에 계획 호출을 넣으면 비용만
늘고 재현성이 떨어진다.

gap 보충은 판단이 둘로 갈린다. **누구를 볼지는 규칙이 정하고**(해석이 남긴
`domain_gap` 유무), 넘어온 컬럼들을 gap_planner가 한자리에서 보고 "무엇을 할지"를
정한다(단독 호출). 여러 컬럼을 묶어 보는 판단은 컬럼 하나만 보는 호출이 할 수
없어서 뒤쪽만 LLM에 맡긴다. 넘어온 컬럼이 없으면 planner를 아예 부르지 않는다.

단계를 지날 때마다 컬럼이 어떻게 바뀌었는지를 `ColumnHistory`에 남긴다. 결과
dict는 최신 상태만 들고 있어서, 기록하지 않으면 "gap 보충이 무엇을 바꿨는지"가
덮어쓰기와 함께 사라진다.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import pandas as pd

from column_semantics.core.clock import now_iso
from column_semantics.core.evidence import build_table_evidence
from column_semantics.core.history import ColumnHistory
from column_semantics.core.llm_log import LLMLog
from column_semantics.core.probes import apply_probes, run_probe, with_measurements
from column_semantics.core.relations import build_relation_groups
from column_semantics.core.timeline import Timeline
from column_semantics.pipeline.documents import PARTS, build_documents
from column_semantics.pipeline.plan import (
    MAX_ACTIONS_PER_COLUMN,
    MAX_GAP_ROUNDS,
    MAX_GROUP_COLUMNS,
    STAGE_ORDER,
    first_pass_stages,
    flagged_columns,
    revision_steps,
    sanitize_gap_actions,
)
from column_semantics.pipeline.stage_runner import StageRunner

Documents = Dict[str, Dict[str, Any]]


@dataclass
class PipelineConfig:
    max_rounds: int = 2
    max_workers: int = 12
    source_name: str = ""
    # 각 skill이 끝날 때마다 그때까지의 문서 6벌을 넘긴다. 중간에 죽어도 여기까지는 남는다.
    on_checkpoint: Optional[Callable[[Documents], None]] = None
    # 모든 문서의 meta에 그대로 합쳐진다(입력 경로, skills 경로 등 실행 환경 정보).
    meta: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------
# 병렬 실행 단위
# ---------------------------------------------------------------------


def _parallel(max_workers: int, jobs: List[Any], fn: Callable[[Any], Any]) -> Dict[int, Any]:
    """jobs를 병렬 실행하고 {인덱스: 결과}를 돌려준다. 예외는 그대로 올라간다."""
    outputs, errors = _parallel_collect(max_workers, jobs, fn)
    for error in errors.values():
        raise error
    return outputs


def _parallel_collect(
    max_workers: int,
    jobs: List[Any],
    fn: Callable[[Any], Any],
    on_each: Optional[Callable[[int, Any, Optional[BaseException]], None]] = None,
) -> Tuple[Dict[int, Any], Dict[int, BaseException]]:
    """병렬 실행하되 성공한 것과 실패한 것을 갈라서 돌려준다.

    하나가 죽었다고 끝난 나머지를 버리면, 컬럼 40개 중 39개를 이미 해석해놓고도
    결과가 통째로 비어버린다 - 그 39번의 호출은 이미 지불한 비용이다. 죽는 것은
    호출부가 정하고, 여기서는 무엇이 살아남았는지를 잃지 않는 것만 책임진다.
    """
    outputs: Dict[int, Any] = {}
    errors: Dict[int, BaseException] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fn, job): i for i, job in enumerate(jobs)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                outputs[index] = future.result()
            except BaseException as e:  # noqa: BLE001 - 갈라 담고 호출부가 결정한다
                errors[index] = e
            if on_each is not None:
                # 완료되는 대로 값을 함께 넘긴다. 여기(메인 스레드)에서만 불리므로
                # 호출부는 락 없이 결과를 갱신해도 된다.
                on_each(index, outputs.get(index), errors.get(index))
    return outputs, errors


def interpret_columns_parallel(
    runner: StageRunner,
    evidence: Dict[str, Any],
    results: Dict[str, Any],
    max_workers: int,
    columns: List[str],
    revision_feedback: Optional[Dict[str, Any]] = None,
    existing: Optional[Dict[str, Any]] = None,
    history: Optional[ColumnHistory] = None,
    stage_info: Optional[Dict[str, Any]] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, Any]:
    """columns 각각에 대해 column_interpretation을 병렬 호출한다. existing이 있으면
    (재검증 라운드에서 focus로 좁힌 경우) 그 위에 columns만 덮어쓰고 나머지 컬럼은
    이전 라운드 결과를 유지한다."""
    merged: Dict[str, Any] = dict(existing or {})

    print(f"[PARALLEL] column_interpretation {len(columns)}개 컬럼 병렬 실행", flush=True)
    document = {"columns": merged}
    # 결과 슬롯을 먼저 걸어둔다. 컬럼이 끝나는 대로 여기 쌓이므로, 이 단계 도중에
    # 찍히는 체크포인트와 죽을 때 쓰이는 문서가 "그때까지 끝난 컬럼"을 담는다.
    # 예전에는 단계가 통째로 끝나야 한 번에 들어가서, 40개 중 39개를 해석한
    # 시점에 프로세스가 사라지면 파일에는 전부 null만 남았다.
    results["column_interpretation"] = document
    done = 0

    def absorb(i: int, value: Any, error: Optional[BaseException]) -> None:
        nonlocal done
        done += 1
        if error is None:
            col = columns[i]
            before = merged.get(col)
            merged[col] = value
            if history is not None:
                _record(history, col, "column_interpretation", before, value, stage_info)
        if on_progress is not None:
            on_progress(done, len(columns))

    outputs, errors = _parallel_collect(
        max_workers,
        columns,
        lambda col: runner.interpret_column(col, evidence, revision_feedback),
        on_each=absorb,
    )
    if errors:
        failed = [columns[i] for i in sorted(errors)]
        for i in sorted(errors):
            col = columns[i]
            if history is not None:
                history.record(
                    col,
                    "column_interpretation",
                    {"error": f"{type(errors[i]).__name__}: {errors[i]}"},
                    **(stage_info or {}),
                )
        print(f"[FAIL] column_interpretation 실패 {len(failed)}개 컬럼: {', '.join(failed)}", flush=True)
        raise next(iter(errors[i] for i in sorted(errors)))

    return document


def validate_grouped(
    runner: StageRunner,
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

    outputs, errors = _parallel_collect(
        max_workers,
        jobs,
        lambda job: runner.validate_group(
            job[1], evidence, results, revision_feedback, job[0]
        ),
    )
    if errors:
        print(
            f"[FAIL] semantic_validation 실패 {len(errors)}/{len(jobs)}개 단위 - "
            "성공한 단위의 check는 남긴다",
            flush=True,
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

    validation = {
        "overall_status": "needs_revision" if any_needs_revision else "pass",
        "checks": merged_checks,
        "revision_requests": merged_requests,
        "validated_columns": merged_validated,
    }
    if errors:
        # 검증을 끝까지 못 했으면 "pass"라고 말할 수 없다. 죽기 전에 결과 슬롯에
        # 넣어두되, 통과로 읽히지 않게 상태를 남긴다.
        validation["overall_status"] = "incomplete"
        results["semantic_validation"] = validation
        raise next(iter(errors[i] for i in sorted(errors)))
    return validation


def resolve_gaps(
    df: pd.DataFrame,
    runner: StageRunner,
    evidence: Dict[str, Any],
    results: Dict[str, Any],
    max_workers: int,
    candidates: List[str],
    probe_log: List[Dict[str, Any]],
    joint_findings: List[Dict[str, Any]],
    history: Optional[ColumnHistory] = None,
    stage_info: Optional[Dict[str, Any]] = None,
    action_counts: Optional[Dict[str, int]] = None,
    done_actions: Optional[Set[Tuple[str, Tuple[str, ...]]]] = None,
) -> Dict[str, Any]:
    """domain_gap이 남은 컬럼에 대해 gap_planner가 정한 행동을 실행한다.

    게이트는 LLM 호출이 아니라 `domain_gap` 유무를 보는 규칙이다(`plan.py`).
    넘어온 컬럼이 없으면 planner를 부르지 않고 그대로 끝낸다.

    여기서 돌려주는 기록은 **계획에 관한 것만**이다. 컬럼이 어떻게 바뀌었는지는
    컬럼 이력에, 호출 원문은 llm_calls에, probe 실측은 probe_log에 각각 들어간다 -
    같은 값을 두 문서에 복사하지 않는다.
    """
    interpretations = (results.get("column_interpretation") or {}).get("columns", {}) or {}
    flagged = flagged_columns(interpretations, candidates)
    record: Dict[str, Any] = {
        # 무엇을 보고 무엇이 넘어갔는지. 게이트가 규칙이라 이 둘만 있으면 판정을
        # 그대로 재현할 수 있다.
        "gate": "domain_gap",
        "considered": list(candidates),
        "flagged": flagged,
        "planner": None,
        "actions": [],
        "dropped": [],
        "changed": [],
    }
    if not flagged:
        print("[GAP] domain_gap이 남은 컬럼 없음 - planner 호출 생략", flush=True)
        return record

    print(f"[GAP] domain_gap이 남은 컬럼 {len(flagged)}개: {', '.join(flagged)}", flush=True)
    planned = runner.plan_gaps(flagged, evidence, results)
    actions, dropped = sanitize_gap_actions(
        planned.get("actions"),
        flagged,
        evidence["table"]["columns"],
        action_counts,
        done_actions,
    )
    record["planner"] = {"raw": planned.get("raw"), "skipped": planned.get("skipped")}
    record["actions"] = actions
    record["dropped"] = dropped
    if dropped:
        print(f"[GAP] 실행 불가로 버린 행동 {len(dropped)}건", flush=True)
    if not actions:
        print("[GAP] 실행할 행동 없음", flush=True)
        return record

    print(
        f"[GAP] {len(actions)}건 실행: "
        + ", ".join(f"{a['action']}({'+'.join(a['columns'])})" for a in actions),
        flush=True,
    )
    outputs = _parallel(
        max_workers,
        actions,
        lambda a: runner.run_gap_skill(
            a["action"], a["columns"], evidence, results, a.get("reason", "")
        ),
    )

    columns = results.setdefault("column_interpretation", {}).setdefault("columns", {})
    changed: List[str] = []
    for i, action in enumerate(actions):
        out = outputs.get(i)
        if not isinstance(out, dict):
            continue

        # 여러 컬럼을 묶어 본 결과의 "관계"는 컬럼 하나에 속한 값이 아니다.
        # 컬럼마다 복사하지 않고 테이블 문서로 한 벌만 보낸다.
        probe_id = None
        if action["action"] == "joint_interpretation":
            probe_id = _run_group_probe(df, out.get("probe"), action, probe_log, stage_info)
            joint_findings.append(
                {
                    "columns": action["columns"],
                    "relationship": out.get("relationship"),
                    "reason": action.get("reason", ""),
                    "probe_id": probe_id,
                    **(stage_info or {}),
                }
            )

        for col in action["columns"]:
            update = _column_update(action["action"], col, out)
            if not update or col not in columns:
                continue
            before = dict(columns[col]) if isinstance(columns[col], dict) else columns[col]
            for key in ("selected_meaning", "status", "sparsity_reason", "note", "evidence"):
                # None은 "이번엔 안 바꿈"이라는 뜻이라 - 있는 값을 null로 지워버리면 안 된다.
                if update.get(key) is not None:
                    columns[col][key] = update[key]
            # domain_gap만은 키가 있으면 null이라도 그대로 반영한다. 이 필드가 곧
            # 보완 게이트라, 지울 방법이 없으면 한 번 걸린 컬럼은 라운드마다 다시
            # 걸리고 planner 호출만 늘어난다. 보완이 실제로 대상을 밝혔다는 보고는
            # "이제 없다"로만 할 수 있다.
            if "domain_gap" in update:
                columns[col]["domain_gap"] = update["domain_gap"]
            columns[col].setdefault("gap_history", []).append(
                {"skill": action["action"], "reason": action.get("reason", "")}
            )
            changed.append(col)
            if history is not None:
                history.record_change(
                    col,
                    "gap",
                    before,
                    dict(columns[col]),
                    skill=action["action"],
                    with_columns=[c for c in action["columns"] if c != col],
                    reason=action.get("reason", ""),
                    skill_output=update,
                    probe_id=probe_id,
                    **(stage_info or {}),
                )

    record["changed"] = sorted(set(changed))
    return record


def _run_group_probe(
    df: pd.DataFrame,
    probe: Any,
    action: Dict[str, Any],
    probe_log: List[Dict[str, Any]],
    stage_info: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """묶어 본 결과가 행 단위로 검사 가능한 관계를 냈으면 그 자리에서 실측한다.

    검사 가능한 주장은 데이터에 대고 본다는 규칙이 보완 단계라고 달라지지 않는다.
    실측값은 다른 probe와 같은 자리(rulebase 문서)로 가고, 컬럼 이력과 그룹
    기록은 probe_id로만 가리킨다. 재보지 못했으면 그 이유를 같은 자리에 남긴다.
    """
    if not isinstance(probe, dict):
        return None
    result = run_probe(df, probe)
    probe_id = f"probe-{len(probe_log) + 1}"
    probe_log.append(
        {
            "probe_id": probe_id,
            **(stage_info or {}),
            "source": "joint_interpretation",
            "columns": action["columns"],
            "requested": probe,
            "observed": result.observed,
            "not_evaluable": result.reason,
        }
    )
    return probe_id


def _column_update(action: str, column: str, output: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """보완 출력에서 이 컬럼에 반영할 부분을 꺼낸다.

    컬럼 하나짜리 보완은 출력 전체가 그 컬럼 몫이고, 여러 컬럼을 보는 보완은
    `columns[col]`이 그 컬럼 몫이다.
    """
    if action == "joint_interpretation":
        update = (output.get("columns") or {}).get(column)
        return update if isinstance(update, dict) else None
    return output


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


def _record_stage_columns(
    history: ColumnHistory,
    stage: str,
    before: Optional[Dict[str, Any]],
    after: Optional[Dict[str, Any]],
    stage_info: Dict[str, Any],
) -> None:
    """테이블 단위 고정 단계가 컬럼별로 낸 값을 컬럼 이력에 흩뿌린다.

    relation_analysis의 `revised_columns`만 여기 해당한다 - 나머지 테이블 단위
    단계는 컬럼별 판단을 담지 않는다.
    """
    key = {"relation_analysis": "revised_columns"}.get(stage)
    if key is None:
        return
    previous = (before or {}).get(key) or {}
    current = (after or {}).get(key) or {}
    for col, value in current.items():
        _record(history, col, stage, previous.get(col), value, stage_info)


# ---------------------------------------------------------------------
# 파이프라인
# ---------------------------------------------------------------------


def run_pipeline(
    df: pd.DataFrame,
    runner: StageRunner,
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
    joint_findings: List[Dict[str, Any]] = []
    planning: Dict[str, Any] = {"first_pass": {}, "gap_rounds": [], "replans": []}

    def build_docs(
        status: str,
        validation_status: str = "not_yet_run",
        error: Optional[str] = None,
    ) -> Documents:
        return build_documents(
            meta={
                **config.meta,
                **({"error": error} if error else {}),
                "llm_model": getattr(runner.llm, "model", ""),
                # 이 실행이 어떤 예산으로 돌았는지. 결과를 나중에 비교할 때 예산이
                # 바뀐 건지 모델이 다르게 답한 건지 구분하려면 같이 남아야 한다.
                "max_rounds": config.max_rounds,
                "max_gap_rounds": MAX_GAP_ROUNDS,
                "max_actions_per_column": MAX_ACTIONS_PER_COLUMN,
                "max_group_columns": MAX_GROUP_COLUMNS,
                # 최소 출력을 같이 받았는지. 호출 수가 두 배로 달라지므로 결과를
                # 비교할 때 반드시 봐야 하는 조건이다.
                "lean_track": runner.lean is not None,
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
            joint_findings=joint_findings,
            timeline_events=timeline.events(),
            llm_log=llm_log,
            lean=runner.lean,
        )

    last_checkpoint = [0.0]

    def checkpoint(min_interval: float = 0.0) -> None:
        """진행 상황을 파일로 내보낸다.

        min_interval을 주면 그 간격 안에서는 건너뛴다 - 컬럼이 끝날 때마다 문서
        6벌을 다시 쓰면 컬럼 수만큼 PVC I/O가 늘어난다. 단계 경계에서는 항상
        쓰고(min_interval=0), 긴 단계 도중에는 드문드문 쓴다.
        """
        if config.on_checkpoint is None:
            return
        now = time.time()
        if min_interval and now - last_checkpoint[0] < min_interval:
            return
        last_checkpoint[0] = now
        config.on_checkpoint(build_docs("in_progress"))

    def exec_stage(stage: str, phase: str, round_idx: Optional[int] = None, **kwargs: Any) -> None:
        label = "[EXEC]" if phase == "exec" else "[RE-EXEC]"
        print(f"{label} {stage} 시작", flush=True)
        started = time.time()
        before = results.get(stage)
        with timeline.measure(event="stage", phase=phase, stage=stage, **_round(round_idx)):
            with runner.stage(phase=phase, **_round(round_idx)):
                results[stage] = runner.run_stage(stage, evidence, results, **kwargs)
        _record_stage_columns(
            history, stage, before, results[stage], _stage_info(phase, round_idx)
        )
        print(f"{label} {stage} 완료 ({time.time() - started:.1f}초)", flush=True)
        checkpoint()

    def _on_column_done(done: int, total: int) -> None:
        # 컬럼 수가 많으면 이 단계 하나가 몇 분이다. 그동안 파일이 비어 있으면
        # "돌고 있는지 멈춘 건지"를 알 수 없고, 프로세스가 예외 없이 사라지면
        # (OOM, 강제 종료) 그때까지의 해석이 통째로 날아간다.
        if done % 10 == 0 or done == total:
            print(f"[PARALLEL] column_interpretation {done}/{total} 완료", flush=True)
        checkpoint(min_interval=15.0)

    def exec_columns(phase: str, columns: List[str], round_idx: Optional[int] = None, **kwargs: Any) -> None:
        label = "[EXEC]" if phase == "exec" else "[RE-EXEC]"
        print(f"{label} column_interpretation 시작 ({len(columns)}개 컬럼)", flush=True)
        started = time.time()
        with timeline.measure(
            event="stage", phase=phase, stage="column_interpretation", **_round(round_idx)
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
                    on_progress=_on_column_done,
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
            event="stage", phase=phase, stage="semantic_validation", **_round(round_idx)
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
            with timeline.measure(event="probe", stage="semantic_validation", **_round(round_idx)):
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

    # 죽는 순간까지의 기록을 반드시 파일로 내보낸다. 마지막 체크포인트 이후의
    # 호출(특히 죽인 그 호출)은 메모리에만 있어서, 여기서 한 번 더 쓰지 않으면
    # 실패 원인이 담긴 기록이 통째로 사라진다.
    try:
        # -- 1차 pass ------------------------------------------------------
        has_pairwise = bool(evidence.get("relation_evidence", {}).get("pairwise"))
        first_pass = first_pass_stages(has_pairwise)
        planning["first_pass"] = {
            "stages": first_pass,
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

        action_counts: Dict[str, int] = {}
        done_actions: Set[Tuple[str, Tuple[str, ...]]] = set()

        def gap_round(round_no: int, candidates: List[str]) -> Dict[str, Any]:
            """게이트 -> (넘어온 게 있으면) planner -> 실행. 한 라운드가 여기 전부다."""
            with timeline.measure(event="gap_resolution", round=round_no) as entry:
                with runner.stage(stage="gap", round=round_no, phase="exec"):
                    record = resolve_gaps(
                        df,
                        runner,
                        evidence,
                        results,
                        max_workers,
                        candidates=candidates,
                        probe_log=probe_log,
                        joint_findings=joint_findings,
                        history=history,
                        stage_info=_stage_info("exec", round_no),
                        action_counts=action_counts,
                        done_actions=done_actions,
                    )
                entry["considered"] = len(record["considered"])
                entry["flagged"] = len(record["flagged"])
                entry["actions"] = len(record["actions"])
            record["round"] = round_no
            planning["gap_rounds"].append(record)
            checkpoint()
            return record

        with runner.stage(stage="first_pass", round=1):
            exec_columns(phase="exec", columns=all_columns)

        # -- gap 보충 ---------------------------------------------------------
        # 보완이 컬럼 해석을 바꾸므로 relation_analysis보다 먼저 돈다 - 관계 분석이
        # 바뀌기 전 해석을 근거로 삼으면 그만큼 낡은 판단이 된다.
        touched = gap_round(1, all_columns)
        for round_no in range(2, MAX_GAP_ROUNDS + 1):
            # 다시 판정하는 것은 이번 라운드에 실제로 바뀐 컬럼뿐이다. 손대지 않은
            # 컬럼은 domain_gap도 그대로라 결과가 같다.
            if not touched["changed"]:
                break
            touched = gap_round(round_no, touched["changed"])

        if "relation_analysis" in first_pass:
            with runner.stage(stage="first_pass", round=1):
                exec_stage("relation_analysis", phase="exec")

        # -- 검증 -> 마무리 --------------------------------------------------
        with runner.stage(stage="validation", round=1):
            exec_validation(phase="exec")
        with runner.stage(stage="first_pass", round=1):
            exec_stage("table_context", phase="exec")

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
            print(f"[PLAN {round_idx}]", " -> ".join(step["stage"] for step in steps), flush=True)

            for step in steps:
                stage = step["stage"]
                if stage == "column_interpretation":
                    exec_columns(
                        phase="re-exec",
                        columns=step.get("focus") or all_columns,
                        round_idx=round_idx,
                        revision_feedback=feedback,
                        existing=(results.get("column_interpretation") or {}).get("columns", {}),
                    )
                elif stage == "semantic_validation":
                    exec_validation(phase="re-exec", round_idx=round_idx, revision_feedback=feedback)
                else:
                    exec_stage(
                        stage,
                        phase="re-exec",
                        round_idx=round_idx,
                        focus=step.get("focus", []),
                        revision_feedback=feedback,
                    )

            # 수정 후에는 항상 table_context를 다시 만든다.
            exec_stage(
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
    except BaseException as e:  # noqa: BLE001 - 기록만 남기고 그대로 올려보낸다
        if config.on_checkpoint is not None:
            config.on_checkpoint(build_docs("failed", error=f"{type(e).__name__}: {e}"))
        raise


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
    "STAGE_ORDER",
    "interpret_columns_parallel",
    "resolve_gaps",
    "run_pipeline",
    "validate_grouped",
]
