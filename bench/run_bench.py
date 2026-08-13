"""
성능 벤치마크.

    # 0) 엔드포인트 점검 — 어떤 structured 모드가 실제로 강제되는지 확인
    python bench/check_endpoint.py

    # 1) mock 데이터 생성
    python examples/make_mock_data.py --out data/mock --rows 500

    # 2) 비용 0으로 하네스 오버헤드 측정
    python bench/run_bench.py --offline --reps 3

    # 3) 실제 vLLM으로 측정
    export LLM_API_ENDPOINT=http://vllm-host:8000/v1
    export LLM_API_KEY=EMPTY
    export LLM_STRUCTURED_MODE=guided_json
    python bench/run_bench.py --models Qwen/Qwen2.5-32B-Instruct --reps 5

    # 4) 중단 후 재실행 — 이미 끝난 조합은 건너뛴다
    python bench/run_bench.py --models ... --reps 5

결과는 `--output`(기본 results/bench.jsonl)에 한 줄씩 append된다.
기존 레포의 run_robustness_test.py와 **같은 축**(dataset x column_descriptions
유무 x model x rep)을 쓰므로, 옛 결과와 나란히 놓고 볼 수 있다.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from agent.config import load_dotenv_file as load_dotenv  # noqa: E402
from agent.runner import TableAssetContextRunner  # noqa: E402
from bench.scoring import consistency, score_against_truth, score_process  # noqa: E402
from examples.run_local import build_column_descriptions  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------


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
            e = json.loads(line)
            done.add((e["dataset"], e["with_column_descriptions"], e["model"], e["rep"]))
        except Exception:  # noqa: BLE001
            continue
    return done


def build_deps(offline: bool, model: str):
    if offline:
        from bench.offline_llm import OfflineDeps

        return OfflineDeps()

    from openai import OpenAI

    from agent.llm import RuntimeDeps

    endpoint = os.environ["LLM_API_ENDPOINT"]
    key = os.environ.get("LLM_API_KEY", "EMPTY")
    return RuntimeDeps(raw_client=OpenAI(base_url=endpoint, api_key=key), model=model)


# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/mock")
    ap.add_argument("--output", default="results/bench.jsonl")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--models", nargs="*", default=None, help="미지정 시 LLM_MODEL 하나")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--only", nargs="*", help="데이터셋 일부만")
    ap.add_argument("--no-summary", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    load_dotenv()

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        raise SystemExit(f"{data_dir}가 없습니다. 먼저 python examples/make_mock_data.py 를 실행하세요.")

    datasets = [d for d in discover(data_dir) if not args.only or d["name"] in args.only]
    if not datasets:
        raise SystemExit(f"{data_dir}에 데이터셋이 없습니다.")

    if args.offline:
        models = ["offline"]
    else:
        models = args.models or [os.environ.get("LLM_MODEL")]
        if not models[0]:
            raise SystemExit("--models 또는 LLM_MODEL이 필요합니다.")

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = load_done(out_path)

    total = len(datasets) * 2 * len(models) * args.reps
    print(
        f"데이터셋 {len(datasets)} x 설명유무 2 x 모델 {len(models)} x 반복 {args.reps} = {total}회"
        f" (완료 {len(done)}회 건너뜀)\n"
    )

    idx = 0
    for model in models:
        # 모델 하나당 deps 하나. RPM 스로틀 상태를 전체가 공유해야 한도가 지켜진다.
        deps = build_deps(args.offline, model)
        builder = TableAssetContextRunner(deps=deps)

        for ds in datasets:
            df = pd.read_csv(ds["csv"])
            source_description = ds["metadata"].get("source_description")
            full_desc = build_column_descriptions(ds["metadata"])

            for with_desc in (True, False):
                for rep in range(1, args.reps + 1):
                    key = (ds["name"], with_desc, model, rep)
                    if key in done:
                        continue
                    idx += 1
                    print(
                        f"[{idx}/{total - len(done)}] {ds['name']} desc={with_desc} "
                        f"{model} rep={rep}",
                        flush=True,
                    )

                    if hasattr(deps, "reset_stats"):
                        deps.reset_stats()
                    started = time.time()
                    error = None
                    result = None
                    try:
                        result = builder.build(
                            tabular_data=df,
                            asset_name=ds["name"],
                            source_description=source_description,
                            column_descriptions=full_desc if with_desc else None,
                        )
                    except Exception as exc:  # noqa: BLE001 - 한 회차가 죽어도 배치는 계속
                        logger.exception("실행 실패")
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
                        # 원본은 크므로 재현에 필요한 부분만 남긴다.
                        "asset_context": (result or {}).get("asset_context"),
                    }
                    with out_path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print(f"\n완료: {out_path}")
    if not args.no_summary:
        summarize(out_path)


# ---------------------------------------------------------------------------
# 요약
# ---------------------------------------------------------------------------


def summarize(path: Path) -> None:
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not rows:
        return
    ok = [r for r in rows if not r.get("error")]

    print("\n" + "=" * 92)
    print("  정답 대비 정확도  (mock 데이터의 truth.json 기준)")
    print("=" * 92)
    header = f"{'dataset':<22}{'desc':<7}{'kind':>7}{'role':>7}{'grain':>7}{'res':>7}{'PII R/P':>12}{'FD':>7}"
    print(header)
    print("-" * 92)
    for key, group in _group(ok, lambda r: (r["dataset"], r["with_column_descriptions"])).items():
        a = [g["accuracy"] for g in group if g.get("accuracy")]
        if not a:
            continue
        pii_r, pii_p = _mean(a, "pii_recall"), _mean(a, "pii_precision")
        print(
            f"{key[0]:<22}{str(key[1]):<7}"
            f"{_fmt(_mean(a,'kind_accuracy')):>7}{_fmt(_mean(a,'role_accuracy')):>7}"
            f"{_fmt(_mean(a,'grain_exact')):>7}{_fmt(_mean(a,'resolution_accuracy')):>7}"
            f"{(_fmt(pii_r)+'/'+_fmt(pii_p)):>12}{_fmt(_mean(a,'fd_recall')):>7}"
        )

    print("\n" + "=" * 92)
    print("  실행 특성  (정답 없이 재는 항목)")
    print("=" * 92)
    print(
        f"{'dataset':<22}{'desc':<7}{'sec':>7}{'LLM':>6}{'tok':>8}{'iter':>6}"
        f"{'retry':>7}{'cover':>7}{'probeC':>8}{'refut':>7}"
    )
    print("-" * 92)
    for key, group in _group(ok, lambda r: (r["dataset"], r["with_column_descriptions"])).items():
        p = [g["process"] for g in group if g.get("process")]
        if not p:
            continue
        print(
            f"{key[0]:<22}{str(key[1]):<7}"
            f"{_mean(p,'elapsed_seconds',1):>7}{_mean(p,'llm_calls',1):>6}"
            f"{_mean(p,'total_tokens',0):>8}{_mean(p,'iterations',1):>6}"
            f"{_fmt(_mean(p,'retry_rate')):>7}{_fmt(_mean(p,'column_coverage')):>7}"
            f"{_fmt(_mean(p,'probe_coverage')):>8}{_mean(p,'refuted_count',1):>7}"
        )

    # 반복 일관성
    print("\n" + "=" * 92)
    print("  반복 일관성  (같은 입력 N회. 정확도가 아니라 재현성이다)")
    print("=" * 92)
    print(f"{'dataset':<22}{'desc':<7}{'N':>4}{'role':>8}{'resol':>8}{'keyword':>9}{'summary':>9}")
    print("-" * 92)
    for key, group in _group(ok, lambda r: (r["dataset"], r["with_column_descriptions"])).items():
        results = [{"asset_context": g["asset_context"]} for g in group if g.get("asset_context")]
        c = consistency(results)
        if not c:
            continue
        print(
            f"{key[0]:<22}{str(key[1]):<7}{len(results):>4}"
            f"{_fmt(c.get('role_agreement')):>8}{_fmt(c.get('resolution_agreement')):>8}"
            f"{_fmt(c.get('keyword_jaccard')):>9}{_fmt(c.get('summary_jaccard')):>9}"
        )

    errors = [r for r in rows if r.get("error")]
    if errors:
        print(f"\n실패 {len(errors)}회:")
        for e in errors[:5]:
            print(f"  {e['dataset']} rep={e['rep']}: {e['error'][:90]}")

    # 놓친 항목을 눈에 띄게 남긴다. 평균만 보면 어느 컬럼이 틀렸는지 모른다.
    misses = _collect_misses(ok)
    if misses:
        print("\n주요 오답:")
        for m, n in sorted(misses.items(), key=lambda kv: -kv[1])[:12]:
            print(f"  {n:>3}회  {m}")


def _group(rows, keyfn):
    out: Dict[Any, List] = {}
    for r in rows:
        out.setdefault(keyfn(r), []).append(r)
    return dict(sorted(out.items()))


def _mean(dicts, key, digits=3):
    vals = [d[key] for d in dicts if isinstance(d.get(key), (int, float))]
    if not vals:
        return None
    v = sum(vals) / len(vals)
    return round(v, digits) if digits else int(v)


def _fmt(v):
    return "-" if v is None else f"{v:.2f}" if isinstance(v, float) else str(v)


def _collect_misses(rows) -> Dict[str, int]:
    from collections import Counter

    c: Counter = Counter()
    for r in rows:
        a = r.get("accuracy") or {}
        for field in ("kind_misses", "role_misses", "resolution_misses"):
            for m in a.get(field) or []:
                c[f"{r['dataset']}.{m}"] += 1
        for m in a.get("pii_missed") or []:
            c[f"{r['dataset']}.PII미탐:{m}"] += 1
        for m in a.get("pii_false_positive") or []:
            c[f"{r['dataset']}.PII과탐:{m}"] += 1
    return dict(c)


if __name__ == "__main__":
    main()
