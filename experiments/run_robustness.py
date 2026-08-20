#!/usr/bin/env python3
"""같은 CSV를 여러 번 돌려 결과가 얼마나 흔들리는지 본다. 실험 전용.

LLM은 같은 입력에도 다른 답을 낸다. 한 번 잘 나온 결과는 증거가 아니다 -
(dataset x rep)로 반복해서 분포를 봐야 한다. 그래서 이 스크립트는 결론을
내지 않고 회차별 요약을 JSONL로 append만 한다. 집계/판단은 분석 쪽에서 한다.

**이어달리기**: 이미 끝난 (dataset, model, rep) 조합은 건너뛴다. Job이 중간에
죽어도 다시 실행하면 남은 조합부터 이어서 돈다.

    python experiments/run_robustness.py --data-dir /data/robustness_test \
        --output /data/robustness_results.jsonl --reps 20
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from column_semantics.adapters.env import load_dotenv  # noqa: E402
from column_semantics.app import (  # noqa: E402
    DEFAULT_PROMPT_DIR,
    DEFAULT_SKILL_DIR,
    analyze_csv,
)

Key = Tuple[str, str, int]


def load_done(path: Path) -> Set[Key]:
    if not path.exists():
        return set()
    done: Set[Key] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            done.add((row["dataset"], row["model"], row["rep"]))
        except Exception:  # noqa: BLE001 - 깨진 줄은 안 끝난 것으로 보고 다시 돈다
            continue
    return done


def summarize(documents: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """JSONL 한 줄에 넣을 요약. 전체 결과는 크니 비교 축만 남긴다."""
    meta = documents["columns"]["meta"]
    columns = documents["columns"]["columns"]
    rounds = documents["table"]["validation"].get("rounds") or []
    checks = rounds[-1]["checks"] if rounds else []
    interpretations = [c["final"]["interpretation"] or {} for c in columns.values()]
    gap_rounds = documents["plan"]["gap_rounds"]
    reviewed = sum(len(r["reviewed"]) for r in gap_rounds)
    flagged_names = sorted({c for r in gap_rounds for c in r["flagged"]})
    flagged = sum(len(r["flagged"]) for r in gap_rounds)
    return {
        "validation_status": meta.get("validation_status"),
        "elapsed_seconds": meta.get("elapsed_seconds"),
        "llm_calls": len(documents["llm_calls"]["calls"]),
        "column_count": len(columns),
        "resolved_columns": sum(1 for v in interpretations if v.get("status") == "resolved"),
        # 구조적으로 확정됐는지와 도메인을 식별했는지는 별개 축이다. 후자가 낮으면
        # 프롬프트가 아니라 자료(공통코드/컬럼 코멘트)가 없어서 못 푸는 것이다.
        "columns_with_domain_gap": sum(1 for v in interpretations if v.get("domain_gap")),
        "checks": len(checks),
        "probe_verified_checks": sum(1 for c in checks if c.get("probe_verified")),
        "failed_checks": sum(1 for c in checks if c.get("status") in {"warning", "fail"}),
        # 재본 것과 재보지 못한 것을 갈라 센다. 후자가 많으면 skill이 검사할 수
        # 없는 것을 자꾸 요청하고 있다는 뜻이고, 그건 프롬프트에서 고칠 문제다.
        "probes_run": len(documents["rulebase"]["probes"]),
        "probes_measured": sum(
            1 for p in documents["rulebase"]["probes"] if p.get("observed") is not None
        ),
        "probes_not_evaluable": sum(
            1 for p in documents["rulebase"]["probes"] if p.get("observed") is None
        ),
        # 검토가 제 역할을 하는지 보는 축. flag_ratio가 1에 가까우면 검토가 아무것도
        # 거르지 않는 것이고, 0이면 planner가 아예 돌지 않는다 - 둘 다 프롬프트 문제다.
        #
        # flagged_columns는 이름을 그대로 남긴다. 비율이 적당해도 회차마다 다른
        # 컬럼이 걸리면 검토를 믿을 수 없는데, 그 흔들림은 이름 없이는 못 본다 -
        # 같은 (dataset, model)의 여러 rep에서 컬럼별 등장 횟수를 세면 나온다.
        # (한 회차 안에서 여러 라운드에 걸쳐 걸린 컬럼은 한 번만 센다)
        "gap_rounds": len(gap_rounds),
        "reviewed_columns": reviewed,
        "flagged_count": flagged,
        "flagged_columns": flagged_names,
        "flag_ratio": round(flagged / reviewed, 3) if reviewed else None,
        "gap_actions": sum(len(r["actions"]) for r in gap_rounds),
        "joint_actions": len(documents["table"]["joint_findings"]),
        # 실행 불가로 버려진 행동. 늘어나면 planner가 계약을 못 지키고 있다는 뜻이다.
        "dropped_actions": sum(len(r["dropped"]) for r in gap_rounds),
        "table_context": documents["table"]["table_context"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--prompts", type=Path, default=DEFAULT_PROMPT_DIR)
    parser.add_argument("--skills", type=Path, default=DEFAULT_SKILL_DIR)
    parser.add_argument("--max-rounds", type=int, default=2)
    args = parser.parse_args()

    load_dotenv()
    model = os.environ.get("LLM_MODEL", "")
    done = load_done(args.output)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    csvs = sorted(args.data_dir.glob("*.csv"))
    if not csvs:
        print(f"[ERROR] {args.data_dir}에 CSV가 없습니다.", file=sys.stderr)
        return 1

    planned = [(c, r) for c in csvs for r in range(1, args.reps + 1)]
    print(f"[PLAN] {len(csvs)}개 데이터셋 x {args.reps}회 = {len(planned)}건, 이미 끝난 것 {len(done)}건")

    failures = 0
    for csv, rep in planned:
        key: Key = (csv.stem, model, rep)
        if key in done:
            continue
        print(f"\n[RUN] {csv.stem} rep={rep}")
        started = time.time()
        row: Dict[str, Any] = {"dataset": csv.stem, "model": model, "rep": rep}
        try:
            documents = analyze_csv(
                csv,
                prompt_dir=args.prompts,
                skill_dir=args.skills,
                max_rounds=args.max_rounds,
            )
            row.update(summarize(documents))
            row["status"] = "ok"
        except Exception as e:  # noqa: BLE001 - 한 회차 실패가 실험을 끝내면 안 된다
            traceback.print_exc()
            failures += 1
            row["status"] = "error"
            row["error"] = f"{type(e).__name__}: {e}"
        row["wall_seconds"] = round(time.time() - started, 3)

        with args.output.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"\n==== 완료. 실패 {failures}건. 결과: {args.output} ====")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
