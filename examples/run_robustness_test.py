from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from agent.config import get_models, get_reps, load_dotenv_file  # noqa: E402
from agent.exp_logging import log_end, log_start, new_exp_dir, setup_logging  # noqa: E402
from agent.llm import (  # noqa: E402
    MAX_CONCURRENCY,
    MAX_HTTP_RETRIES,
    REQUESTS_PER_MINUTE,
    STRUCTURED_MODE,
)
from agent.runner import TableAssetContextRunner  # noqa: E402
from bench.scoring import score_against_truth, score_process  # noqa: E402
from examples.run_local import build_column_descriptions  # noqa: E402


def discover(data_dir: Path) -> List[Dict[str, Any]]:
    out = []
    for csv in sorted(data_dir.glob("*.csv")):
        meta_path = csv.with_name(f"{csv.stem}_metadata.json")
        truth_path = csv.with_name(f"{csv.stem}_truth.json")
        out.append(
            {
                "name": csv.stem,
                "csv": csv,
                "metadata": json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {},
                "truth": json.loads(truth_path.read_text(encoding="utf-8")) if truth_path.exists() else {},
            }
        )
    return out


def build_deps(model: str):
    from openai import OpenAI

    from agent.llm import RuntimeDeps

    endpoint = os.environ["LLM_API_ENDPOINT"]
    key = os.environ.get("LLM_API_KEY", "EMPTY")
    return RuntimeDeps(raw_client=OpenAI(base_url=endpoint, api_key=key), model=model)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/mock")
    ap.add_argument("--results-dir", default="results")
    ap.add_argument("--reps", type=int, default=None)
    ap.add_argument("--models", nargs="*", default=None)
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()

    load_dotenv_file()

    data_dir = Path(args.data_dir)
    datasets = [d for d in discover(data_dir) if not args.only or d["name"] in args.only]
    if not datasets:
        raise SystemExit(f"no datasets found in {data_dir}")

    models = args.models or get_models()
    reps = args.reps or get_reps()

    exp_dir = new_exp_dir(Path(args.results_dir))
    logger = setup_logging(exp_dir)
    out_path = exp_dir / "robustness_results.jsonl"

    log_start(
        logger,
        {
            "data_dir": str(data_dir),
            "datasets": [d["name"] for d in datasets],
            "models": models,
            "reps": reps,
            "requests_per_minute": REQUESTS_PER_MINUTE,
            "max_concurrency": MAX_CONCURRENCY,
            "max_http_retries": MAX_HTTP_RETRIES,
            "structured_mode": STRUCTURED_MODE,
            "results_dir": str(exp_dir),
        },
    )

    total_runs = 0
    total_errors = 0
    batch_started = time.time()

    for model in models:
        deps = build_deps(model)
        runner = TableAssetContextRunner(deps=deps)

        for ds in datasets:
            df = pd.read_csv(ds["csv"])
            source_description = ds["metadata"].get("source_description")
            full_desc = build_column_descriptions(ds["metadata"])

            for with_desc in (True, False):
                for rep in range(1, reps + 1):
                    started = time.time()
                    error = None
                    result = None
                    try:
                        result = runner.build(
                            tabular_data=df,
                            asset_name=ds["name"],
                            source_description=source_description,
                            column_descriptions=full_desc if with_desc else None,
                        )
                    except Exception as exc:  # noqa: BLE001
                        error = f"{type(exc).__name__}: {exc}"

                    entry = {
                        "dataset": ds["name"],
                        "rows": len(df),
                        "columns": len(df.columns),
                        "with_column_descriptions": with_desc,
                        "model": model,
                        "rep": rep,
                        "wall_seconds": round(time.time() - started, 2),
                        "error": error,
                        "accuracy": score_against_truth(result, ds["truth"]) if result else {},
                        "process": score_process(result) if result else {},
                        "column_analysis": (result or {}).get("column_analysis"),
                        "data_interpretation": (result or {}).get("data_interpretation"),
                        "asset_context": (result or {}).get("asset_context"),
                    }
                    with out_path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

                    total_runs += 1
                    if error:
                        total_errors += 1
                    logger.info(
                        "run dataset=%s desc=%s model=%s rep=%d error=%s wall=%ss",
                        ds["name"], with_desc, model, rep, error or "-", entry["wall_seconds"],
                    )

    log_end(
        logger,
        {
            "total_runs": total_runs,
            "errors": total_errors,
            "wall_seconds": round(time.time() - batch_started, 1),
            "results_dir": str(exp_dir),
        },
    )


if __name__ == "__main__":
    main()
