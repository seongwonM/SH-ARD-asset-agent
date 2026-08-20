#!/usr/bin/env python3
"""디렉터리 안의 CSV를 전부 해석한다. 실험/재현용이지 제품 경로가 아니다.

k8s의 column-poc-batch Job은 같은 일을 셸 루프로 한다(Job은 CSV 하나가 실패해도
컨테이너가 죽지 않도록 bash에서 감싸는 편이 단순해서다). 이 스크립트는 로컬에서
같은 배치를 돌려보기 위한 것이고, 출력 폴더 구조를 Job과 똑같이 맞춘다:

    <out>/<실행타임스탬프>/<csv_stem>/result.semantic.json

실행 한 번 = 실험 하나 = 타임스탬프 폴더 하나. CSV마다 타임스탬프를 새로 찍지
않는다(그러면 같은 배치로 돌린 결과가 흩어져 어느 실행에 속하는지 알 수 없다).

    python experiments/run_batch.py --data-dir ./data --out ./results
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from column_semantics.adapters.env import load_dotenv  # noqa: E402
from column_semantics.adapters.llm import models_from_env  # noqa: E402
from column_semantics.app import (  # noqa: E402
    DEFAULT_PROMPT_DIR,
    DEFAULT_SKILL_DIR,
    analyze_csv,
    safe_name,
)
from column_semantics.core.clock import KST  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True, help="CSV가 들어있는 폴더")
    parser.add_argument("--out", type=Path, required=True, help="결과 루트 폴더")
    parser.add_argument("--prompts", type=Path, default=DEFAULT_PROMPT_DIR)
    parser.add_argument("--skills", type=Path, default=DEFAULT_SKILL_DIR)
    parser.add_argument("--max-rounds", type=int, default=2)
    args = parser.parse_args()

    load_dotenv()

    csvs = sorted(args.data_dir.glob("*.csv"))
    if not csvs:
        print(f"[ERROR] {args.data_dir}에 CSV가 없습니다.", file=sys.stderr)
        return 1

    # 모델은 LLM_MODEL에 쉼표로 여러 개 적을 수 있다. 실행 하나는 항상 모델
    # 하나이고, 모델마다 결과 폴더를 따로 만든다 - 한 폴더에 섞이면 어느 모델이
    # 낸 결과인지 파일만 보고는 알 수 없다.
    models = models_from_env()
    if not models:
        print("[ERROR] LLM_MODEL이 비어 있습니다(.env 확인).", file=sys.stderr)
        return 1

    # 타임스탬프는 배치 전체에 하나. 모델이 여럿이어도 같은 실험이다.
    run_ts = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    print(f"[BATCH] 모델 {len(models)}개: {', '.join(models)}")
    print(f"[BATCH] CSV {len(csvs)}개: {', '.join(c.name for c in csvs)}")
    print(f"[BATCH] 출력 루트: {args.out} / 실행 타임스탬프: {run_ts}")

    failed = []
    for model in models:
        os.environ["LLM_MODEL"] = model
        run_dir = args.out / f"{run_ts}_{safe_name(model)}"
        print(f"\n[MODEL] {model} -> {run_dir}")

        for csv in csvs:
            exp_dir = run_dir / csv.stem
            out = exp_dir / "result.semantic.json"
            print(f"\n[RUN] {csv.name} -> {exp_dir}")
            try:
                analyze_csv(
                    csv_path=csv,
                    prompt_dir=args.prompts,
                    skill_dir=args.skills,
                    max_rounds=args.max_rounds,
                    output=out,
                )
            except Exception:  # noqa: BLE001 - CSV 하나가 실패해도 나머지는 계속 돈다
                traceback.print_exc()
                failed.append(f"{model}/{csv.stem}")

    total = len(csvs) * len(models)
    print(f"\n==== 완료: {total}건 중 {total - len(failed)}건 성공 ====")
    if failed:
        print("[FAILED LIST] " + " ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
