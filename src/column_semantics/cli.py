"""CSV 한 개를 해석하는 CLI. k8s Job이 CSV마다 이걸 부른다.

    python -m column_semantics ./data.csv
    python -m column_semantics ./data.csv --output-root /data/results/20260820_1200

필요한 환경변수(로컬은 .env, k8s는 secret이 envFrom으로 주입):

    LLM_API_ENDPOINT=http://<vllm-host>:8000/v1
    LLM_API_KEY=EMPTY
    LLM_MODEL=<served-model-name>[,<another-model>...]

`LLM_MODEL`에 쉼표로 여러 모델을 적으면 **같은 CSV를 모델마다 한 번씩** 해석한다.
실행 하나는 언제나 모델 하나이고, 결과도 모델별 폴더로 갈린다 - 한 폴더에 섞이면
어느 모델이 낸 건지 파일만 보고는 알 수 없다.

출력 경로는 두 가지 중 하나로 준다.

    --output-root DIR   <DIR>_<모델명>/<csv이름>/ 아래에 결과 5개 + run.log
                        (배치용. 모델이 여럿이면 폴더가 모델 수만큼 생긴다)
    --output PATH       PATH를 기준으로 결과 5개 (모델 하나일 때만)

결과 파일은 다섯 개다.

    <base>.columns.json     컬럼별 해석이 단계마다 어떻게 바뀌었는지
    <base>.rulebase.json    룰베이스 계산값(프로파일/관계 증거/probe 실측)
    <base>.plan.json        계획과 실행 과정
    <base>.table.json       테이블 단위 산출물과 검증 라운드
    <base>.llm_calls.json   모든 LLM 호출의 입력/출력 원문

단계가 끝날 때마다 이 파일들을 덮어쓴다. 중간에 죽어도 그때까지의 결과는 남고,
완주했는지는 각 파일의 `meta.status`가 done인지로 판별한다.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path
from typing import List, Optional

from column_semantics.adapters.env import load_dotenv
from column_semantics.adapters.llm import models_from_env
from column_semantics.adapters.logtee import tee_output
from column_semantics.app import (
    DEFAULT_PROMPT_DIR,
    DEFAULT_SKILL_DIR,
    analyze_csv,
    output_paths,
    safe_name,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CSV를 프롬프트 파이프라인으로 분석해 컬럼/테이블 의미를 추론합니다."
    )
    parser.add_argument("csv", type=Path, help="입력 CSV 경로")
    parser.add_argument(
        "--prompts",
        type=Path,
        default=DEFAULT_PROMPT_DIR,
        help="고정 단계 프롬프트 폴더 경로",
    )
    parser.add_argument(
        "--skills", type=Path, default=DEFAULT_SKILL_DIR, help="보완 skill 폴더 경로"
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help="배치용 출력 루트. <루트>_<모델명>/<csv이름>/ 아래에 결과와 run.log를 쓴다",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="출력 기준 경로(모델 하나일 때만). 기본값: <csv>.semantic.json",
    )
    parser.add_argument(
        "--max-rounds", type=int, default=2, help="검증 실패 시 최대 재계획 라운드"
    )
    return parser


def resolve_output(
    csv_path: Path, model: str, output_root: Optional[Path], output: Optional[Path]
) -> Path:
    """이 (CSV, 모델) 조합의 결과가 어디로 갈지 정한다.

    폴더 이름에 모델을 넣는 쪽이 output_root다 - 같은 배치의 모델별 결과가
    타임스탬프를 공유하면서 폴더로만 갈린다.
    """
    if output_root is not None:
        root = Path(f"{output_root}_{safe_name(model)}")
        return root / csv_path.stem / "result.semantic.json"
    if output is not None:
        return output
    return csv_path.with_suffix(csv_path.suffix + ".semantic.json")


def main(argv: Optional[List[str]] = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)

    csv_path = args.csv.resolve()
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)

    models = models_from_env()
    if not models:
        print("[ERROR] LLM_MODEL이 비어 있습니다(.env 또는 k8s secret 확인).", file=sys.stderr)
        return 2
    if len(models) > 1 and args.output_root is None:
        print(
            f"[ERROR] LLM_MODEL에 모델이 {len(models)}개인데 --output-root가 없습니다. "
            "모델별 폴더가 필요하므로 --output-root를 쓰거나 모델을 하나만 지정하세요.",
            file=sys.stderr,
        )
        return 2

    # 이 실행에서 LLM_MODEL은 모델마다 덮어쓴다. 원래 무엇을 지정했는지가
    # 로그와 meta에서 사라지지 않도록 따로 남긴다.
    os.environ["LLM_MODELS_CONFIGURED"] = ",".join(models)

    failed: List[str] = []
    for index, model in enumerate(models, start=1):
        os.environ["LLM_MODEL"] = model
        output = resolve_output(csv_path, model, args.output_root, args.output)
        # 배치일 때만 로그를 결과 옆에 남긴다. 로컬 단발 실행은 화면으로 충분하다.
        log_path = output.parent / "run.log" if args.output_root is not None else None

        print(f"\n[MODEL] {model} ({index}/{len(models)})", flush=True)
        if not _run_one(csv_path, model, output, log_path, args):
            failed.append(model)

    if failed:
        print(f"[FAILED] 모델 {len(failed)}/{len(models)}건 실패: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


def _run_one(csv_path: Path, model: str, output: Path, log_path: Optional[Path], args) -> bool:
    """모델 하나로 한 번 해석한다. 실패해도 다음 모델은 계속 돈다."""
    if log_path is None:
        return _analyze(csv_path, model, output, args)
    with tee_output(log_path):
        ok = _analyze(csv_path, model, output, args)
    return ok


def _analyze(csv_path: Path, model: str, output: Path, args) -> bool:
    try:
        analyze_csv(
            csv_path=csv_path,
            prompt_dir=args.prompts.resolve(),
            skill_dir=args.skills.resolve(),
            max_rounds=args.max_rounds,
            output=output,
        )
    except Exception:  # noqa: BLE001 - 모델 하나가 죽어도 나머지는 돌린다
        traceback.print_exc()
        print(f"[FAIL] {csv_path.name} model={model}", flush=True)
        return False

    for path in output_paths(output).values():
        print(f"[DONE] {path}", flush=True)
    return True


if __name__ == "__main__":
    raise SystemExit(main())
