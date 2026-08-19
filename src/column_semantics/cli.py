"""CSV 한 개를 해석하는 CLI.

    python run.py ./data.csv
    python run.py ./data.csv --output result.json --max-rounds 2

필요한 환경변수(로컬은 .env, k8s는 secret이 envFrom으로 주입):

    LLM_API_ENDPOINT=http://<vllm-host>:8000/v1
    LLM_API_KEY=EMPTY
    LLM_MODEL=<served-model-name>

skill 하나가 끝날 때마다 <output>.partial.json에 그때까지의 결과를 체크포인트로
남긴다. 끝까지 성공하면 <output>에 최종 결과가 쓰이고 partial 파일은 지워진다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from column_semantics.adapters.env import load_dotenv
from column_semantics.app import DEFAULT_SKILL_DIR, analyze_csv, write_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CSV를 skill 파이프라인으로 분석해 컬럼/테이블 의미를 추론합니다."
    )
    parser.add_argument("csv", type=Path, help="입력 CSV 경로")
    parser.add_argument(
        "--skills", type=Path, default=DEFAULT_SKILL_DIR, help="skills 폴더 경로"
    )
    parser.add_argument(
        "--output", type=Path, default=None, help="출력 JSON 경로. 기본값: <csv>.semantic.json"
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
    checkpoint_path = output.with_suffix(output.suffix + ".partial.json")

    result = analyze_csv(
        csv_path=csv_path,
        skill_dir=args.skills.resolve(),
        max_rounds=args.max_rounds,
        checkpoint_path=checkpoint_path,
    )

    write_json(output, result)
    print(f"[DONE] {output}")


if __name__ == "__main__":
    main()
