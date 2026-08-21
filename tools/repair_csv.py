"""깨진 CSV를 복구하고, 행별로 무엇을 왜 그렇게 판단했는지 보여주는 CLI.

복구 로직은 여기 없다 - `adapters/csv_repair.py`가 전부 들고 있고, 파이프라인의
자동 경로(`read_csv_safely`)도 같은 코드를 탄다. 로직이 두 벌이면 "CLI에서는
복구되는데 배치에서는 실패한다"가 시작된다.

자동 경로와 다른 점은 **실패를 다루는 방식**뿐이다. 자동 경로는 애매한 행이
하나라도 있으면 통째로 거절하지만(잘못 병합된 값이 측정값 행세를 하며 결과
문서까지 흘러가는 게 더 비싸다), 여기서는 행별로 갈라 보여주고 무엇을 내보낼지
사람이 정한다.

    python tools/repair_csv.py broken.csv -o fixed.csv --report report.json
    python tools/repair_csv.py broken.csv                 # 미리보기만
    python tools/repair_csv.py --selftest                 # 합성 데이터로 로직 확인
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence

# 이 스크립트는 PYTHONPATH 없이도 바로 실행할 수 있어야 한다(전처리는 보통
# 파이프라인을 돌리기 전에 급히 한 번 돌린다).
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from column_semantics.adapters.csv_repair import (  # noqa: E402
    AMBIGUOUS_MARGIN,
    MAX_EXTRA_FIELDS,
    ColumnProfile,
    RowRecord,
    read_rows,
    repair_rows,
)

STATUSES = ("ok", "repaired", "ambiguous", "underflow", "too_many", "unresolved")


def summarize(records: Sequence[RowRecord]) -> Dict[str, int]:
    counts = Counter(r.status for r in records)
    return {k: counts[k] for k in STATUSES if counts[k]}


def write_csv(
    path: str,
    header: Sequence[str],
    records: Sequence[RowRecord],
    encoding: str,
    delimiter: str,
    keep_ambiguous: bool,
    on_fail: str,
) -> int:
    n = len(header)
    written = 0
    with open(path, "w", encoding=encoding, newline="") as f:
        writer = csv.writer(f, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(header)
        for rec in records:
            settled = rec.status in ("ok", "repaired") or (
                rec.status == "ambiguous" and keep_ambiguous
            )
            if settled and rec.values is not None:
                writer.writerow(rec.values)
                written += 1
            elif on_fail == "pad":
                # 복구는 못 했어도 원본은 있다. 컬럼 수에만 맞춰 흘려보내고,
                # 무엇이 잘렸는지는 report의 raw로 확인한다.
                row = list(rec.values if rec.values is not None else rec.raw)[:n]
                writer.writerow(row + [""] * (n - len(row)))
                written += 1
            # on_fail == "drop": 값을 지어내느니 빼고 report에만 남긴다
    return written


def write_report(
    path: str, records: Sequence[RowRecord], profiles: Sequence[ColumnProfile]
) -> None:
    payload = {
        "summary": summarize(records),
        "columns": [
            {
                "name": p.name,
                "categorical": p.categorical,
                "closed": p.closed,
                "open_text": p.open_text,
                "numeric_ratio": round(p.numeric_ratio, 3),
                "comma_rows": p.comma_rows,
                "max_len": p.max_len,
            }
            for p in profiles
        ],
        "merged_column_counts": dict(
            Counter(col for r in records for col in (r.merged or []))
        ),
        "rows": [
            {
                "line": r.line,
                "raw_fields": r.raw_fields,
                "status": r.status,
                "margin": r.margin,
                "merged": r.merged,
                "values": r.values,
                "raw": r.raw,
                "note": r.note,
            }
            for r in records
            if r.status != "ok"
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------- selftest

def _selftest() -> int:
    """합성 데이터로 복구율을 잰다. 실제 파일 없이 로직만 확인할 때 쓴다."""
    rng = random.Random(7)
    # 쉼표를 품는 컬럼은 location(3)과 qty(5) - 이웃하지 않는다(복구의 전제).
    header = ["id", "reg_date", "equip_code", "location", "description", "qty", "status"]
    places = ["Seoul Plant", "Ulsan Plant, Line 2", "Icheon", "Gwangju Plant, B동, 3F"]
    descs = ["정기 점검", "베어링 교체", "이상 없음", "온도 상승 알람 발생", "필터 교체"]
    truth: List[List[str]] = []
    for i in range(400):
        truth.append(
            [
                f"EQ{i:05d}",
                f"2024-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                f"A{rng.randint(100, 999)}-{rng.randint(10, 99)}",
                rng.choice(places),
                rng.choice(descs),
                f"{rng.randint(1, 9):d},{rng.randint(100, 999):03d}"
                if rng.random() < 0.3
                else str(rng.randint(1, 999)),
                rng.choice(["OK", "NG", "HOLD"]),
            ]
        )

    # 깨뜨리기: 따옴표 없이 그대로 이어붙인 뒤 다시 쪼갠다.
    raw_rows = [(i + 2, ",".join(row).split(",")) for i, row in enumerate(truth)]
    records, profiles = repair_rows(header, raw_rows)

    broken = [(r, t) for r, t in zip(records, truth) if r.raw_fields != len(header)]
    exact = sum(1 for r, t in broken if r.values == t)
    ambiguous = sum(1 for r, _ in broken if r.status == "ambiguous")
    print(f"깨진 행 {len(broken)} / 전체 {len(truth)}")
    print(f"정확 복구 {exact} ({exact / max(1, len(broken)):.1%}), 애매 표시 {ambiguous}")
    for r, t in broken:
        if r.values != t:
            print(f"  MISS L{r.line} margin={r.margin}")
            print(f"    got  {r.values}")
            print(f"    want {t}")
    print("summary:", summarize(records))
    print("comma_rows(2pass):", {p.name: p.comma_rows for p in profiles})
    return 0 if exact == len(broken) else 1


# --------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="값에 쉼표가 섞여 깨진 CSV를 컬럼 관계로 복구한다"
    )
    ap.add_argument("csv", nargs="?", help="깨진 CSV 경로")
    ap.add_argument("-o", "--output", help="복구본 경로 (없으면 미리보기만)")
    ap.add_argument("--report", help="행별 판단을 남길 JSON 경로")
    ap.add_argument("--encoding", default="utf-8-sig")
    ap.add_argument("--delimiter", default=",")
    ap.add_argument(
        "--max-extra", type=int, default=MAX_EXTRA_FIELDS, help="한 행의 초과 필드 상한"
    )
    ap.add_argument("--passes", type=int, default=2, help="복구된 행을 프로파일에 되먹일 횟수")
    ap.add_argument(
        "--ambiguous-margin",
        type=float,
        default=AMBIGUOUS_MARGIN,
        help="2등 해와 이 점수 차 미만이면 ambiguous로 표시한다",
    )
    ap.add_argument(
        "--allow-adjacent",
        action="store_true",
        help="쉼표를 품은 컬럼이 이웃해도 허용 (전제가 깨지므로 margin을 꼭 확인할 것)",
    )
    ap.add_argument(
        "--keep-ambiguous", action="store_true", help="애매한 행도 최적해로 출력에 포함"
    )
    ap.add_argument(
        "--on-fail",
        choices=["drop", "pad"],
        default="drop",
        help="복구 실패 행 처리: 빼기(기본) / 컬럼 수만 맞춰 내보내기",
    )
    ap.add_argument("--preview", type=int, default=10, help="화면에 보여줄 행 수")
    ap.add_argument("--selftest", action="store_true", help="합성 데이터로 로직만 검증")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()
    if not args.csv:
        ap.error("CSV 경로가 필요하다 (또는 --selftest)")

    header, raw_rows = read_rows(args.csv, args.encoding, args.delimiter)
    if not header:
        print("빈 파일이다", file=sys.stderr)
        return 2

    # 정상 행이 곧 근거다. 이게 얇으면 복구는 사실상 추측이 되니 먼저 말한다.
    n_clean = sum(1 for _, row in raw_rows if len(row) == len(header))
    if n_clean < 20 or (raw_rows and n_clean < 0.3 * len(raw_rows)):
        print(
            f"경고: 정상 행이 {n_clean}개뿐이다 ({len(raw_rows)}개 중). "
            "프로파일 근거가 얇아 margin이 낮게 나올 것이다 - report를 반드시 볼 것",
            file=sys.stderr,
        )

    records, profiles = repair_rows(
        header,
        raw_rows,
        delimiter=args.delimiter,
        allow_adjacent=args.allow_adjacent,
        max_extra=args.max_extra,
        passes=args.passes,
        ambiguous_margin=args.ambiguous_margin,
    )

    summary = summarize(records)
    print(f"컬럼 {len(header)}개, 행 {len(raw_rows)}개 -> {summary}", file=sys.stderr)

    # 볼 가치가 큰 순서로 보여준다: 복구하지 못한 행이 먼저다.
    ranked = [r for r in records if r.status not in ("ok", "repaired")]
    ranked += [r for r in records if r.status == "repaired"]
    for rec in ranked[: max(0, args.preview)]:
        mark = " " if rec.status == "repaired" else "?"
        print(
            f"{mark} L{rec.line} +{rec.raw_fields - len(header)} margin={rec.margin} "
            f"merged={rec.merged} {rec.values if rec.values is not None else rec.raw}",
            file=sys.stderr,
        )

    if args.report:
        write_report(args.report, records, profiles)
        print(f"report -> {args.report}", file=sys.stderr)

    if args.output:
        written = write_csv(
            args.output,
            header,
            records,
            args.encoding,
            args.delimiter,
            args.keep_ambiguous,
            args.on_fail,
        )
        print(f"{written}행 -> {args.output}", file=sys.stderr)
    else:
        print("(-o 없음: 파일을 쓰지 않았다)", file=sys.stderr)

    unresolved = sum(
        summary.get(k, 0) for k in ("ambiguous", "underflow", "too_many", "unresolved")
    )
    return 1 if unresolved else 0


if __name__ == "__main__":
    raise SystemExit(main())
