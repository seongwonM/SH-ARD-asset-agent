"""조립 지점(composition root).

core/adapters/pipeline를 실제로 이어 붙이는 곳은 여기 한 군데다. CLI든 실험
스크립트든 이 함수를 부르지, 어댑터를 직접 만들지 않는다 - 그래야 "로컬 실행과
k8s 배치가 같은 경로를 탄다"가 코드로 보장된다.

결과는 문서 5벌(columns/rulebase/plan/table/llm_calls)이고, `output`을 주면
그 경로를 기준으로 5개 파일에 나눠 쓴다. 어느 문서에 무엇이 들어가는지는
`pipeline/documents.py`에 있다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from column_semantics.adapters.csv_source import read_csv_safely
from column_semantics.adapters.llm import make_llm_from_env, make_rate_limiter_from_env
from column_semantics.adapters.skills import FileSystemSkillLibrary
from column_semantics.core.llm_log import LLMLog
from column_semantics.core.timeline import Timeline
from column_semantics.pipeline.documents import PARTS
from column_semantics.pipeline.orchestrator import Documents, PipelineConfig, run_pipeline
from column_semantics.pipeline.plan import REQUIRED_SKILLS
from column_semantics.pipeline.skill_runner import SkillRunner

DEFAULT_SKILL_DIR = Path(__file__).resolve().parents[2] / "skills"


def output_paths(output: Path) -> Dict[str, Path]:
    """출력 기준 경로 하나에서 문서별 파일 경로 5개를 만든다.

        result.semantic.json -> result.semantic.columns.json
                                result.semantic.rulebase.json
                                ...

    k8s Job과 업로드 스크립트가 잡고 있는 것은 이 기준 경로 하나뿐이라,
    문서가 늘거나 줄어도 바깥 계약은 그대로다.
    """
    base = str(output)
    if base.endswith(".json"):
        base = base[: -len(".json")]
    return {part: Path(f"{base}.{part}.json") for part in PARTS}


def analyze_csv(
    csv_path: Path,
    skill_dir: Path = DEFAULT_SKILL_DIR,
    max_rounds: int = 2,
    output: Optional[Path] = None,
) -> Documents:
    """CSV 하나를 해석해 문서 5벌을 돌려준다.

    output을 주면 skill이 끝날 때마다 같은 파일들을 덮어쓴다. 중간에 죽어도
    그때까지 계산된 내용은 파일에 남아 있고, 끝났는지 여부는 각 문서의
    `meta.status`(in_progress/done)로 구분한다.
    """
    csv_path = Path(csv_path)
    skill_dir = Path(skill_dir)
    paths = output_paths(Path(output)) if output is not None else None

    df = read_csv_safely(csv_path)
    print(f"[LOAD] {csv_path.name}: {len(df):,} rows x {len(df.columns)} cols", flush=True)

    timeline = Timeline()
    llm_log = LLMLog()
    rate_limiter = make_rate_limiter_from_env()
    llm = make_llm_from_env(llm_log=llm_log, rate_limiter=rate_limiter)
    skills = FileSystemSkillLibrary(skill_dir, required=REQUIRED_SKILLS)
    runner = SkillRunner(skills=skills, llm=llm)

    config = PipelineConfig(
        max_rounds=max_rounds,
        max_workers=rate_limiter.max_concurrency,
        source_name=csv_path.name,
        on_checkpoint=(lambda docs: write_documents(paths, docs)) if paths else None,
        meta={"source_csv": str(csv_path), "skills_dir": str(skill_dir)},
    )

    documents = run_pipeline(df, runner, config=config, timeline=timeline, llm_log=llm_log)

    if paths is not None:
        write_documents(paths, documents)
    return documents


def write_documents(paths: Dict[str, Path], documents: Documents) -> None:
    for part, path in paths.items():
        if part in documents:
            write_json(path, documents[part])


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
