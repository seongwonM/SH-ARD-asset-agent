"""CSV 한 개를 해석하는 CLI.

    python run.py ./data.csv
    python run.py ./data.csv --output result.json --max-rounds 2

필요한 환경변수(로컬은 .env, k8s는 secret이 envFrom으로 주입):

    LLM_API_ENDPOINT=http://<vllm-host>:8000/v1
    LLM_API_KEY=EMPTY
    LLM_MODEL=<served-model-name>

결과는 --output 경로를 기준으로 5개 파일에 나뉘어 저장된다.

    <output>.columns.json     컬럼별 해석이 단계마다 어떻게 바뀌었는지
    <output>.rulebase.json    룰베이스 계산값(프로파일/관계 증거/probe 실측)
    <output>.plan.json        계획과 실행 과정
    <output>.table.json       테이블 단위 산출물과 검증 라운드
    <output>.llm_calls.json   모든 LLM 호출의 입력/출력 원문

skill 하나가 끝날 때마다 이 파일들을 덮어쓴다. 중간에 죽어도 그때까지의 결과는
남고, 완주했는지는 각 파일의 `meta.status`가 done인지로 판별한다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from column_semantics.adapters.env import load_dotenv
from column_semantics.app import DEFAULT_SKILL_DIR, analyze_csv, output_paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CSV를 skill 파이프라인으로 분석해 컬럼/테이블 의미를 추론합니다."
    )
    parser.add_argument("csv", type=Path, help="입력 CSV 경로")
    parser.add_argument(
        "--skills", type=Path, default=DEFAULT_SKILL_DIR, help="skills 폴더 경로"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="출력 기준 경로(여기에 .columns/.rulebase/... 가 붙는다). 기본값: <csv>.semantic.json",
    )
    parser.add_argument(
        "--max-rounds", type=int, default=2, help="검증 실패 시 최대 재계획 라운드"
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    args = build_parser().parse_args(argv)

    csv_path = args.csv.resolve()
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)

    output = args.output or csv_path.with_suffix(csv_path.suffix + ".semantic.json")

    analyze_csv(
        csv_path=csv_path,
        skill_dir=args.skills.resolve(),
        max_rounds=args.max_rounds,
        output=output,
    )

    for path in output_paths(output).values():
        print(f"[DONE] {path}")


if __name__ == "__main__":
    main()
