from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from agent.csv_repair import repair_ragged_csv  # noqa: E402
from agent.runner import TableAssetContextRunner  # noqa: E402
from bench.scoring import score_against_truth, score_process  # noqa: E402
from examples.run_local import build_column_descriptions, load_dotenv  # noqa: E402


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


def load_done(path: Path) -> set:
    if not path.exists():
        return set()
    done = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            done.add((row["dataset"], row["with_column_descriptions"], row["model"], row["rep"]))
        except Exception:  # noqa: BLE001
            continue
    return done


def build_deps(model: str):
    from openai import OpenAI

    from agent.llm import RuntimeDeps

    endpoint = os.environ["LLM_API_ENDPOINT"]
    key = os.environ.get("LLM_API_KEY", "EMPTY")
    return RuntimeDeps(raw_client=OpenAI(base_url=endpoint, api_key=key), model=model)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/mock")
    ap.add_argument("--output", default="results/robustness_results.jsonl")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--models", nargs="*", default=None)
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()

    load_dotenv()

    data_dir = Path(args.data_dir)
    datasets = [d for d in discover(data_dir) if not args.only or d["name"] in args.only]
    if not datasets:
        raise SystemExit(f"no datasets found in {data_dir}")

    models = args.models or [os.environ.get("LLM_MODEL")]
    if not models[0]:
        raise SystemExit("--models or LLM_MODEL is required")

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = load_done(out_path)

    for model in models:
        deps = build_deps(model)
        runner = TableAssetContextRunner(deps=deps)

        for ds in datasets:
            try:
                df = repair_ragged_csv(ds["csv"])
            except Exception as exc:  # noqa: BLE001 - CSV 하나가 깨져도 나머지 데이터셋은 계속 돈다
                print(
                    f"dataset={ds['name']}: {ds['csv']}를 읽지 못해 건너뜀 - {type(exc).__name__}: {exc}",
                    flush=True,
                )
                continue
            source_description = ds["metadata"].get("source_description")
            full_desc = build_column_descriptions(ds["metadata"])

            for with_desc in (True, False):
                for rep in range(1, args.reps + 1):
                    key = (ds["name"], with_desc, model, rep)
                    if key in done:
                        continue

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

                    print(
                        f"{ds['name']} desc={with_desc} model={model} rep={rep} "
                        f"error={error or '-'} wall={entry['wall_seconds']}s",
                        flush=True,
                    )


if __name__ == "__main__":
    main()
