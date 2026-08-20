#!/usr/bin/env python3
"""디렉터리 안의 CSV를 전부 해석한다. 실험/재현용이지 제품 경로가 아니다.

k8s의 column-poc-batch Job과 **같은 일을 같은 방식으로** 한다 - CSV를 순회하며
CLI를 한 번씩 부르고, 하나가 실패해도 나머지를 계속 돈다. 모델별 반복과 결과
폴더 규칙은 CLI가 갖고 있으므로 여기서 다시 구현하지 않는다. 규칙이 두 곳에
있으면 로컬과 배치 결과가 조용히 달라진다.

    <out>/<실행타임스탬프>_<모델명>/<csv_stem>/result.semantic.*.json + run.log

실행 한 번 = 실험 하나 = 타임스탬프 하나. 모델이 여럿이면 타임스탬프를 공유하고
폴더만 갈린다(LLM_MODEL에 쉼표로 나열).

    python experiments/run_batch.py --data-dir ./data --out ./results
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from column_semantics.adapters.env import load_dotenv  # noqa: E402
from column_semantics.app import DEFAULT_PROMPT_DIR, DEFAULT_SKILL_DIR  # noqa: E402
from column_semantics.cli import main as run_one  # noqa: E402
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

    run_root = args.out / datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    print(f"[BATCH] CSV {len(csvs)}개 -> {run_root}_<모델명>/<csv이름>/")

    failed = []
    for csv in csvs:
        print(f"\n[RUN] {csv.name}")
        code = run_one(
            [
                str(csv),
                "--prompts", str(args.prompts),
                "--skills", str(args.skills),
                "--output-root", str(run_root),
                "--max-rounds", str(args.max_rounds),
            ]
        )
        if code != 0:
            failed.append(csv.stem)

    print(f"\n==== 완료: CSV {len(csvs)}개 중 {len(csvs) - len(failed)}개 성공 ====")
    if failed:
        print("[FAILED LIST] " + " ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
