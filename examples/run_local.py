"""
외부 환경용 단일 실행 CLI.

    # 1) mock 데이터 생성
    python examples/make_mock_data.py --out data/mock

    # 2) 실행
    export LLM_API_ENDPOINT=https://api.openai.com/v1
    export LLM_API_KEY=sk-...
    export LLM_MODEL=gpt-4o-mini
    export LLM_STRUCTURED_MODE=json_schema
    python examples/run_local.py data/mock/process_log.csv process_log

    # 3) LLM 없이 파이프라인만 확인 (비용 0)
    python examples/run_local.py data/mock/process_log.csv process_log --offline

`.env` 파일이 있으면 자동으로 읽는다(python-dotenv 불필요, 자체 파서).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))  # bench 패키지 접근용

from agent.csv_repair import repair_ragged_csv  # noqa: E402
from agent.llm import RuntimeDeps  # noqa: E402
from agent.runner import TableAssetContextRunner  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 환경 준비
# ---------------------------------------------------------------------------


def load_dotenv(path: str = ".env") -> None:
    """값 뒤 인라인 주석까지 처리하는 최소 파서. 이미 설정된 환경변수는 덮지 않는다."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #")[0].strip().strip("'\"")
        os.environ.setdefault(key.strip(), value)


def build_deps(offline: bool = False, model: str | None = None) -> RuntimeDeps:
    if offline:
        from bench.offline_llm import OfflineDeps

        return OfflineDeps()

    endpoint = os.environ.get("LLM_API_ENDPOINT")
    api_key = os.environ.get("LLM_API_KEY")
    if not endpoint or not api_key:
        raise SystemExit(
            "LLM_API_ENDPOINT / LLM_API_KEY가 필요합니다.\n"
            "  .env에 넣거나 export 하세요. LLM 없이 돌리려면 --offline을 쓰세요."
        )
    try:
        from openai import OpenAI
    except ImportError:
        raise SystemExit("openai 패키지가 필요합니다: pip install openai")

    return RuntimeDeps(raw_client=OpenAI(base_url=endpoint, api_key=api_key), model=model)


# ---------------------------------------------------------------------------
# metadata.json → column_descriptions
# ---------------------------------------------------------------------------


def load_metadata(csv_path: Path) -> dict:
    meta_path = csv_path.with_name(f"{csv_path.stem}_metadata.json")
    if not meta_path.exists():
        return {}
    return json.loads(meta_path.read_text(encoding="utf-8"))


def build_column_descriptions(metadata: dict) -> dict[str, str] | None:
    """
    기존 레포 run_from_csv.py와 동일한 규칙:
    표준용어/표준용어_내용/설명/추가설명을 이어붙여 하나의 문자열로 만든다.
    """
    entries = metadata.get("column_descriptions")
    if not entries:
        return None
    out: dict[str, str] = {}
    for entry in entries:
        name = entry.get("컬럼명")
        if not name:
            continue
        parts = [
            entry.get("표준용어"),
            entry.get("표준용어_내용"),
            entry.get("설명"),
            entry.get("추가설명"),
        ]
        text = " ".join(str(p) for p in parts if p)
        if text:
            out[name] = text
    return out or None


# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("asset_name", nargs="?")
    ap.add_argument("--source-description", default=None)
    ap.add_argument("--no-column-descriptions", action="store_true", help="설명 없이 실행(비교용)")
    ap.add_argument("--offline", action="store_true", help="LLM 없이 결정론적 스텁으로 실행")
    ap.add_argument("--model", default=None)
    ap.add_argument("--output", default=None, help="결과 JSON 저장 경로")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    load_dotenv()

    csv_path = Path(args.csv_path)
    asset_name = args.asset_name or csv_path.stem
    df = repair_ragged_csv(csv_path)

    metadata = load_metadata(csv_path)
    source_description = args.source_description or metadata.get("source_description")
    column_descriptions = None if args.no_column_descriptions else build_column_descriptions(metadata)

    deps = build_deps(offline=args.offline, model=args.model)
    builder = TableAssetContextRunner(deps=deps)
    result = builder.build(
        tabular_data=df,
        asset_name=asset_name,
        source_description=source_description,
        column_descriptions=column_descriptions,
    )

    _print_report(result)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n저장: {args.output}")


def _print_report(r: dict) -> None:
    ac = r.get("asset_context") or {}
    details = ac.get("asset_context_details", {})
    col_block = r.get("column_analysis") or {}
    data_interp = r.get("data_interpretation") or {}
    perf = r.get("performance", {})
    ver = ac.get("verification", {})

    print("\n" + "=" * 68)
    print(f"  {r['input']['asset_name']}")
    print("=" * 68)
    print(f"\n[요약]\n{details.get('summary', '(없음)')}")
    print(f"\n[입도] {ac.get('grain', '(미확정)')}")
    if details.get("keywords"):
        print(f"[키워드] {', '.join(details['keywords'][:12])}")
    if col_block.get("summary"):
        print(f"\n[컬럼]\n{col_block['summary']}")

    if ac.get("compliance", {}).get("pii_columns"):
        print("\n[개인정보]")
        for c in ac["compliance"]["pii_columns"]:
            print(f"  {c['level']:7s} {c['column']}  {c.get('note','')}")

    if ac.get("glossary", {}).get("mappings"):
        print(f"\n[표준용어] {ac['glossary']['mappings']}")
        if ac["glossary"].get("unmapped"):
            print(f"  미정의: {ac['glossary']['unmapped']}")

    deps_found = (ac.get("constraints") or {}).get("functional_dependencies", [])
    if deps_found:
        print(f"\n[함수 종속] {len(deps_found)}건")
        for d in deps_found[:5]:
            print(f"  {d['determinant']} → {d['dependent']}")
    if data_interp.get("time_axes"):
        print("\n[시간축]")
        for axis in data_interp["time_axes"][:5]:
            print(f"  {axis['name']} ({axis.get('resolution') or 'unknown'})")

    print("\n[검증]")
    print(
        f"  통과 {ver.get('verified', 0)} / 반증 {len(ver.get('refuted', []))} / "
        f"검증불가 {ver.get('unverified_count', 0)}  (probe 검증률 {ver.get('probe_coverage', 0)})"
    )
    for s in ver.get("refuted", []):
        print(f"  [반증] {s}")

    print("\n[성능]")
    print(
        f"  {perf.get('elapsed_seconds', 0)}초 | LLM {perf.get('llm_call_count', 0)}회 | "
        f"probe {perf.get('probe_runs', 0)}회 | 토큰 {perf.get('llm_total_tokens', 0)} | "
        f"iteration {perf.get('iterations', 0)}"
    )
    if perf.get("blocked"):
        print(f"  격리: {perf['blocked']}")
    if r.get("issues"):
        print(f"\n[이슈] {len(r['issues'])}건")
        for i in r["issues"][:5]:
            print(f"  [{i['stage']}] {i['message'][:100]}")


if __name__ == "__main__":
    main()
