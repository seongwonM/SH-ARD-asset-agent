"""조립 지점(composition root).

core/adapters/pipeline를 실제로 이어 붙이는 곳은 여기 한 군데다. CLI든 실험
스크립트든 이 함수를 부르지, 어댑터를 직접 만들지 않는다 - 그래야 "로컬 실행과
k8s 배치가 같은 경로를 탄다"가 코드로 보장된다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from column_semantics.adapters.csv_source import read_csv_safely
from column_semantics.adapters.llm import make_llm_from_env, make_rate_limiter_from_env
from column_semantics.adapters.skills import FileSystemSkillLibrary
from column_semantics.core.timeline import Timeline
from column_semantics.pipeline.orchestrator import PipelineConfig, run_pipeline
from column_semantics.pipeline.plan import REQUIRED_SKILLS
from column_semantics.pipeline.skill_runner import SkillRunner

DEFAULT_SKILL_DIR = Path(__file__).resolve().parents[2] / "skills"


def analyze_csv(
    csv_path: Path,
    skill_dir: Path = DEFAULT_SKILL_DIR,
    max_rounds: int = 2,
    checkpoint_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """CSV 하나를 해석해 결과 dict를 돌려준다.

    checkpoint_path를 주면 skill이 끝날 때마다 그때까지의 결과를 그 파일에
    덮어쓴다. 중간에 죽어도 계산된 skill 출력은 남는다.
    """
    csv_path = Path(csv_path)
    skill_dir = Path(skill_dir)

    df = read_csv_safely(csv_path)
    print(f"[LOAD] {csv_path.name}: {len(df):,} rows x {len(df.columns)} cols", flush=True)

    timeline = Timeline()
    rate_limiter = make_rate_limiter_from_env()
    llm = make_llm_from_env(timeline=timeline, rate_limiter=rate_limiter)
    skills = FileSystemSkillLibrary(skill_dir, required=REQUIRED_SKILLS)
    runner = SkillRunner(skills=skills, llm=llm)

    def save_checkpoint(partial: Dict[str, Any]) -> None:
        if checkpoint_path is None:
            return
        write_json(Path(checkpoint_path), partial)

    config = PipelineConfig(
        max_rounds=max_rounds,
        max_workers=rate_limiter.max_concurrency,
        source_name=csv_path.name,
        on_checkpoint=save_checkpoint,
        meta={"source_csv": str(csv_path), "skills_dir": str(skill_dir)},
    )

    result = run_pipeline(df, runner, config=config, timeline=timeline)

    if checkpoint_path is not None and Path(checkpoint_path).exists():
        Path(checkpoint_path).unlink()
    return result


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
