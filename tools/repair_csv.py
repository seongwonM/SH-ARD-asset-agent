"""쉼표가 값에 섞여 깨진 CSV를 열 관계로 되돌린다.

깨진 행은 필드 수가 헤더보다 많고, 그 초과분이 곧 값 안에 들어 있던 쉼표 개수다.
그래서 복구는 "M개 필드를 N개 열로 다시 묶는 분할 문제"가 된다 - 어느 열이 밀렸는지는
정상 행(필드 수가 정확히 N인 행)에서 배운 열별 프로파일(타입 / 모양 / 값 집합 / 길이)에
후보 값이 얼마나 맞는지로 정한다. 최적 분할은 DP로 찾고, 2등 해와의 점수 차(margin)를
함께 남겨 "사실상 아무 데나 붙여도 되는" 애매한 행을 골라낼 수 있게 한다.

전제는 하나: 쉼표를 품은 열이 서로 이웃하지 않는다. 이웃하면 어디까지가 앞 열의
값인지 데이터만으로는 결정되지 않는다(--allow-adjacent로 해제 가능하지만, 그때는
margin이 낮은 행을 반드시 눈으로 볼 것).

값을 지어내지 않는다. 복구가 서지 않는 행은 조용히 채우는 대신 report에 남기고
--on-fail 정책대로 처리한다.

    python tools/repair_csv.py broken.csv -o fixed.csv --report report.json
    python tools/repair_csv.py --selftest
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------- 값의 생김새

_RE_INT = re.compile(r"^[+-]?\d+$")
_RE_THOUSAND = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
_RE_DECIMAL = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")
_RE_DATETIME = re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}[ T]\d{1,2}:\d{2}")
_RE_DATE = re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$")
_RE_TIME = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
_RE_CODE = re.compile(r"^[\w\-./:#+]+$", re.UNICODE)

_BOOLS = {"true", "false", "t", "f", "y", "n", "yes", "no", "0", "1"}

KINDS = ("empty", "bool", "int", "thousand", "decimal", "datetime", "date", "time", "code", "text")
_NUMERIC_KINDS = frozenset({"int", "decimal", "thousand"})
_TEMPORAL_KINDS = frozenset({"date", "datetime", "time"})


def kind_of(value: str) -> str:
    """값 하나를 거친 타입 하나로 접는다. 열의 '닫힌 정도'를 재는 기본 단위."""
    v = value.strip()
    if not v:
        return "empty"
    if v.lower() in _BOOLS and not _RE_INT.match(v):
        return "bool"
    if _RE_INT.match(v):
        return "int"
    if _RE_THOUSAND.match(v):
        return "thousand"
    if _RE_DECIMAL.match(v):
        return "decimal"
    if _RE_DATETIME.match(v):
        return "datetime"
    if _RE_DATE.match(v):
        return "date"
    if _RE_TIME.match(v):
        return "time"
    if _RE_CODE.match(v):
        return "code"
    return "text"


def shape_of(value: str, cap: int = 24) -> str:
    """숫자는 9, 글자는 A로 접고 연속은 하나로 줄인 패턴. 길이는 따로 본다."""
    out: List[str] = []
    for ch in value.strip():
        c = "9" if ch.isdigit() else ("A" if ch.isalpha() else ch)
        if out and out[-1] == c:
            continue
        out.append(c)
        if len(out) >= cap:
            break
    return "".join(out)


# ------------------------------------------------------------- 열 프로파일

# 점수 가중치. 모두 로그 확률 위의 상수라 서로 비교 가능한 스케일이다.
W_KIND = 2.5
W_SHAPE = 1.0
W_COMMA = 2.0
CAT_BONUS = 3.0
CAT_PENALTY = -8.0
LEN_W = 1.5
CLOSED_PENALTY = 4.0
OPEN_RELIEF = 1.5
THOUSAND_BONUS = 1.5  # 정규화가 본 일을 하니 여기선 가벼운 우대만


@dataclass
class ColumnProfile:
    """정상 행에서만 배운 열의 생김새. 여기에 추정값을 섞지 않는다."""

    name: str
    count: int = 0
    kinds: Counter = field(default_factory=Counter)
    shapes: Counter = field(default_factory=Counter)
    values: Counter = field(default_factory=Counter)
    distinct_overflow: bool = False
    max_len: int = 0
    comma_rows: int = 0
    space_rows: int = 0

    _DISTINCT_CAP = 200

    @classmethod
    def build(cls, name: str, values: Sequence[str]) -> "ColumnProfile":
        p = cls(name=name)
        for raw in values:
            v = raw if raw is not None else ""
            p.count += 1
            p.kinds[kind_of(v)] += 1
            p.shapes[shape_of(v)] += 1
            if not p.distinct_overflow:
                p.values[v] += 1
                if len(p.values) > cls._DISTINCT_CAP:
                    p.distinct_overflow = True
                    p.values.clear()
            p.max_len = max(p.max_len, len(v))
            if "," in v:
                p.comma_rows += 1
            if " " in v.strip():
                p.space_rows += 1
        return p

    # --- 파생 판정 -----------------------------------------------------

    @property
    def categorical(self) -> bool:
        if self.distinct_overflow or self.count < 20:
            return False
        d = len(self.values)
        return d <= 50 and d * 4 <= self.count

    @property
    def numeric_ratio(self) -> float:
        if not self.count:
            return 0.0
        return sum(self.kinds[k] for k in _NUMERIC_KINDS) / self.count

    @property
    def temporal_ratio(self) -> float:
        if not self.count:
            return 0.0
        return sum(self.kinds[k] for k in _TEMPORAL_KINDS) / self.count

    @property
    def closed(self) -> bool:
        """쉼표가 들어갈 자리가 애초에 없어 보이는 열."""
        return self.categorical or self.numeric_ratio >= 0.9 or self.temporal_ratio >= 0.9

    @property
    def open_text(self) -> bool:
        """자유 서술이라 쉼표를 품을 만한 열."""
        if not self.count:
            return False
        return (
            self.space_rows / self.count > 0.2
            or self.max_len >= 20
            or self.kinds["text"] / self.count > 0.3
        )

    # --- 점수 ----------------------------------------------------------

    @staticmethod
    def _logp(counter: Counter, key: str, support: int, alpha: float = 0.5) -> float:
        total = sum(counter.values())
        return math.log((counter[key] + alpha) / (total + alpha * support))

    def thousand_like(self, value: str) -> bool:
        """숫자 열에 붙은 1,234 - 깨진 값이 아니라 천 단위 구분자다."""
        return (
            "," in value
            and self.numeric_ratio >= 0.8
            and _RE_THOUSAND.match(value.strip()) is not None
        )

    def score(self, value: str) -> float:
        """이 값이 이 열의 값일 법한 정도. 클수록 그럴듯하다."""
        if self.count == 0:
            return 0.0
        has_comma = "," in value
        thousand = self.thousand_like(value)
        # 천 단위 구분자는 서식 변형일 뿐이라 쉼표를 지운 모습으로 대조한다.
        # 그러지 않으면 정답인 숫자 열이 '한 번도 못 본 모양'으로 크게 깎여
        # 옆 텍스트 열이 이기는 역전이 난다.
        probe = value.replace(",", "") if thousand else value

        k = kind_of(probe)
        s = W_KIND * self._logp(self.kinds, k, len(KINDS))
        s += W_SHAPE * self._logp(self.shapes, shape_of(probe), max(len(self.shapes) + 1, 8))

        if self.categorical:
            s += CAT_BONUS if probe in self.values else CAT_PENALTY

        if self.max_len > 0 and len(probe) > self.max_len:
            s -= LEN_W * min(4.0, (len(probe) - self.max_len) / float(self.max_len))

        if has_comma:
            if thousand:
                s += THOUSAND_BONUS
            else:
                # 이 열이 쉼표를 품은 적이 있는가. 없으면 표본 수만큼 벌점이 커진다.
                s += W_COMMA * math.log((self.comma_rows + 0.2) / (self.count + 1.0))
                if self.closed:
                    s -= CLOSED_PENALTY
                elif self.open_text:
                    s += OPEN_RELIEF
        return s


def build_profiles(header: Sequence[str], rows: Sequence[Sequence[str]]) -> List[ColumnProfile]:
    n = len(header)
    cols: List[List[str]] = [[] for _ in range(n)]
    for row in rows:
        for i in range(n):
            cols[i].append(row[i])
    return [ColumnProfile.build(header[i], cols[i]) for i in range(n)]


# ------------------------------------------------------------------ 재결합

@dataclass
class _Cell:
    score: float
    prev_j: int
    prev_m: int
    rank: int
    take: int


@dataclass
class Segmentation:
    values: List[str]
    score: float
    margin: float  # 2등 해와의 점수 차. inf면 다른 해가 아예 없다
    takes: List[int]

    @property
    def merged_columns(self) -> List[int]:
        return [i for i, t in enumerate(self.takes) if t > 1]


def segment_row(
    fields: Sequence[str],
    profiles: Sequence[ColumnProfile],
    delimiter: str = ",",
    allow_adjacent: bool = False,
    kbest: int = 2,
) -> Optional[Segmentation]:
    """M개 필드를 N개 열로 다시 묶는다. 최적해와 2등해의 차이를 함께 낸다.

    dp[i][j][m] = 앞의 i개 열이 앞의 j개 필드를 가져갔을 때의 상위 k개 점수.
    m은 직전 열이 2개 이상을 합쳤는지 여부 - 이웃한 두 열이 동시에 쉼표를
    품는 경우를 (기본값에서) 막기 위한 상태다.
    """
    n_cols = len(profiles)
    n_fields = len(fields)
    if n_fields < n_cols or n_cols == 0:
        return None
    max_take = n_fields - n_cols + 1

    dp: List[List[List[List[_Cell]]]] = [
        [[[] for _ in range(2)] for _ in range(n_fields + 1)] for _ in range(n_cols + 1)
    ]
    dp[0][0][0].append(_Cell(0.0, -1, 0, -1, 0))

    for i in range(n_cols):
        prof = profiles[i]
        remaining_cols = n_cols - i - 1
        for j in range(n_fields + 1):
            for m in (0, 1):
                cells = dp[i][j][m]
                if not cells:
                    continue
                for take in range(1, max_take + 1):
                    end = j + take
                    if end > n_fields:
                        break
                    # 남은 열들이 남은 필드를 소화할 수 있어야 한다.
                    left = n_fields - end
                    if left < remaining_cols or left > remaining_cols * max_take:
                        continue
                    if take > 1 and m == 1 and not allow_adjacent:
                        continue
                    value = delimiter.join(fields[j:end])
                    gain = prof.score(value)
                    nm = 1 if take > 1 else 0
                    bucket = dp[i + 1][end][nm]
                    for rank, cell in enumerate(cells):
                        bucket.append(_Cell(cell.score + gain, j, m, rank, take))
                    bucket.sort(key=lambda c: c.score, reverse=True)
                    del bucket[kbest:]

    finals: List[Tuple[float, int, int]] = []  # (score, m, rank)
    for m in (0, 1):
        for rank, cell in enumerate(dp[n_cols][n_fields][m]):
            finals.append((cell.score, m, rank))
    if not finals:
        return None
    finals.sort(key=lambda t: t[0], reverse=True)

    best_score, m, rank = finals[0]
    margin = best_score - finals[1][0] if len(finals) > 1 else float("inf")

    takes: List[int] = []
    i, j = n_cols, n_fields
    while i > 0:
        cell = dp[i][j][m][rank]
        takes.append(cell.take)
        i, j, m, rank = i - 1, cell.prev_j, cell.prev_m, cell.rank
    takes.reverse()

    values: List[str] = []
    pos = 0
    for take in takes:
        values.append(delimiter.join(fields[pos : pos + take]))
        pos += take
    return Segmentation(values=values, score=best_score, margin=margin, takes=takes)


# -------------------------------------------------------------- 파이프라인

@dataclass
class RowRecord:
    line: int
    raw_fields: int
    status: str  # ok | repaired | ambiguous | underflow | too_many | unresolved
    values: Optional[List[str]] = None
    margin: Optional[float] = None  # None = 다른 해가 아예 없다(유일해)
    merged: Optional[List[str]] = None
    note: str = ""
    raw: List[str] = field(default_factory=list)  # 복구 실패 시에도 원본은 잃지 않는다


def repair(
    header: List[str],
    raw_rows: List[Tuple[int, List[str]]],
    *,
    delimiter: str = ",",
    allow_adjacent: bool = False,
    max_extra: int = 6,
    passes: int = 2,
    ambiguous_margin: float = 1.0,
) -> Tuple[List[RowRecord], List[ColumnProfile]]:
    """정상 행으로 프로파일을 만들고, 깨진 행을 되돌린다.

    passes >= 2면 확신 있게 복구한 행을 프로파일에 도로 넣고 한 번 더 돈다 -
    "이 열은 원래 쉼표를 품는다"는 사실은 정상 행만 봐서는 절대 알 수 없기
    때문이다. 애매한 행(margin < 임계)은 되먹이지 않는다.
    """
    n = len(header)
    clean = [row for _, row in raw_rows if len(row) == n]
    profiles = build_profiles(header, clean)

    records: List[RowRecord] = []
    for _round in range(max(1, passes)):
        records = []
        extra_corpus: List[List[str]] = []
        for line, row in raw_rows:
            m = len(row)
            if m == n:
                records.append(RowRecord(line, m, "ok", list(row), raw=list(row)))
                continue
            if m < n:
                records.append(
                    RowRecord(
                        line, m, "underflow",
                        note="필드가 모자란다 - 값 안의 줄바꿈이나 잘린 행일 수 있다",
                        raw=list(row),
                    )
                )
                continue
            if m - n > max_extra:
                records.append(
                    RowRecord(
                        line, m, "too_many",
                        note=f"초과 필드 {m - n}개 > --max-extra",
                        raw=list(row),
                    )
                )
                continue

            seg = segment_row(row, profiles, delimiter, allow_adjacent)
            if seg is None:
                records.append(
                    RowRecord(
                        line, m, "unresolved",
                        note="이 전제(쉼표 열이 이웃하지 않음) 아래 가능한 재결합이 없다",
                        raw=list(row),
                    )
                )
                continue

            merged = [header[i] for i in seg.merged_columns]
            status = "repaired" if seg.margin >= ambiguous_margin else "ambiguous"
            margin = None if math.isinf(seg.margin) else round(seg.margin, 3)
            records.append(
                RowRecord(line, m, status, seg.values, margin, merged, raw=list(row))
            )
            if status == "repaired":
                extra_corpus.append(seg.values)

        if _round + 1 >= passes or not extra_corpus:
            break
        profiles = build_profiles(header, clean + extra_corpus)

    return records, profiles


def read_csv(path: str, encoding: str, delimiter: str) -> Tuple[List[str], List[Tuple[int, List[str]]]]:
    with open(path, "r", encoding=encoding, newline="") as f:
        reader = csv.reader(f, delimiter=delimiter)
        rows = [(reader.line_num, row) for row in reader]
    if not rows:
        raise SystemExit("빈 파일이다")
    header = rows[0][1]
    return header, rows[1:]


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
                # 복구는 못 했어도 원본은 있다. 열 수에만 맞춰 흘려보내고,
                # 무엇이 잘렸는지는 report의 raw로 확인한다.
                row = list(rec.values if rec.values is not None else rec.raw)[:n]
                writer.writerow(row + [""] * (n - len(row)))
                written += 1
            # on_fail == "drop": 값을 지어내느니 빼고 report에만 남긴다
    return written


def summarize(records: Sequence[RowRecord]) -> Dict[str, int]:
    c = Counter(r.status for r in records)
    return {k: c[k] for k in ("ok", "repaired", "ambiguous", "underflow", "too_many", "unresolved") if c[k]}


def write_report(path: str, records: Sequence[RowRecord], profiles: Sequence[ColumnProfile]) -> None:
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
    # 쉼표를 품는 열은 location(3)과 qty(5) - 이웃하지 않는다(도구의 전제).
    header = ["id", "reg_date", "equip_code", "location", "description", "qty", "status"]
    places = [
        "Seoul Plant",
        "Ulsan Plant, Line 2",
        "Icheon",
        "Gwangju Plant, B동, 3F",
    ]
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
                f"{rng.randint(1, 9):d},{rng.randint(100, 999):03d}" if rng.random() < 0.3 else str(rng.randint(1, 999)),
                rng.choice(["OK", "NG", "HOLD"]),
            ]
        )

    # 깨뜨리기: 따옴표 없이 그대로 이어붙인 뒤 다시 쪼갠다.
    raw_rows = [(i + 2, ",".join(row).split(",")) for i, row in enumerate(truth)]
    records, profiles = repair(header, raw_rows)

    broken = [(r, t) for r, t in zip(records, truth) if r.raw_fields != len(header)]
    exact = sum(1 for r, t in broken if r.values == t)
    ambiguous = sum(1 for r, _ in broken if r.status == "ambiguous")
    print(f"깨진 행 {len(broken)} / 전체 {len(truth)}")
    print(f"정확 복구 {exact} ({exact / max(1, len(broken)):.1%}), 애매 표시 {ambiguous}")
    for r, t in broken:
        if r.values != t:
            print("  MISS", r.line, "margin=", r.margin)
            print("    got ", r.values)
            print("    want", t)
    print("summary:", summarize(records))
    print("comma_rows(2pass):", {p.name: p.comma_rows for p in profiles})
    return 0 if exact == len(broken) else 1


# --------------------------------------------------------------------- CLI

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="값에 쉼표가 섞여 깨진 CSV를 열 관계로 복구한다")
    ap.add_argument("csv", nargs="?", help="깨진 CSV 경로")
    ap.add_argument("-o", "--output", help="복구본 경로 (없으면 미리보기만)")
    ap.add_argument("--report", help="행별 판단을 남길 JSON 경로")
    ap.add_argument("--encoding", default="utf-8-sig")
    ap.add_argument("--delimiter", default=",")
    ap.add_argument("--max-extra", type=int, default=6, help="한 행이 가질 수 있는 초과 필드 상한")
    ap.add_argument("--passes", type=int, default=2, help="복구된 행을 프로파일에 되먹일 횟수")
    ap.add_argument(
        "--ambiguous-margin",
        type=float,
        default=1.0,
        help="2등 해와 이 점수 차 미만이면 ambiguous로 표시한다",
    )
    ap.add_argument(
        "--allow-adjacent",
        action="store_true",
        help="쉼표를 품은 열이 이웃해도 허용 (전제가 깨지므로 margin을 꼭 확인할 것)",
    )
    ap.add_argument("--keep-ambiguous", action="store_true", help="애매한 행도 최적해로 출력에 포함")
    ap.add_argument(
        "--on-fail",
        choices=["drop", "pad"],
        default="drop",
        help="복구 실패 행 처리: 빼기(기본) / 빈 값으로 채우기",
    )
    ap.add_argument("--preview", type=int, default=10, help="화면에 보여줄 복구 행 수")
    ap.add_argument("--selftest", action="store_true", help="합성 데이터로 로직만 검증")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()
    if not args.csv:
        ap.error("CSV 경로가 필요하다 (또는 --selftest)")

    header, raw_rows = read_csv(args.csv, args.encoding, args.delimiter)

    # 정상 행이 곧 근거다. 이게 얇으면 복구는 사실상 추측이 되니 먼저 말한다.
    n_clean = sum(1 for _, row in raw_rows if len(row) == len(header))
    if n_clean < 20 or (raw_rows and n_clean < 0.3 * len(raw_rows)):
        print(
            f"경고: 정상 행이 {n_clean}개뿐이다 ({len(raw_rows)}개 중). "
            "프로파일 근거가 얇아 margin이 낮게 나올 것이다 - report를 반드시 볼 것",
            file=sys.stderr,
        )

    records, profiles = repair(
        header,
        raw_rows,
        delimiter=args.delimiter,
        allow_adjacent=args.allow_adjacent,
        max_extra=args.max_extra,
        passes=args.passes,
        ambiguous_margin=args.ambiguous_margin,
    )

    summary = summarize(records)
    print(f"열 {len(header)}개, 행 {len(raw_rows)}개 -> {summary}", file=sys.stderr)

    # 볼 가치가 큰 순서로 보여준다: 애매한 행이 먼저다.
    preview_rows = [r for r in records if r.status == "ambiguous"]
    preview_rows += [r for r in records if r.status == "repaired"]
    for rec in preview_rows[: max(0, args.preview)]:
        mark = "?" if rec.status == "ambiguous" else " "
        print(
            f"{mark} L{rec.line} +{rec.raw_fields - len(header)} margin={rec.margin} "
            f"merged={rec.merged} {rec.values}",
            file=sys.stderr,
        )

    if args.report:
        write_report(args.report, records, profiles)
        print(f"report -> {args.report}", file=sys.stderr)

    if args.output:
        written = write_csv(
            args.output, header, records, args.encoding, args.delimiter,
            args.keep_ambiguous, args.on_fail,
        )
        print(f"{written}행 -> {args.output}", file=sys.stderr)
    else:
        print("(-o 없음: 파일을 쓰지 않았다)", file=sys.stderr)

    unresolved = sum(summary.get(k, 0) for k in ("underflow", "too_many", "unresolved"))
    return 1 if unresolved else 0


if __name__ == "__main__":
    raise SystemExit(main())
