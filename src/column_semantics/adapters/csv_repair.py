"""쉼표가 값 안에 섞여 깨진 CSV를 복구하는 유틸리티.

`read_csv_safely`가 pandas ParserError를 만났을 때만 호출한다(정상 CSV는
이 경로를 타지 않는다).

CSV 값 안에 이스케이프 안 된 ","가 들어있으면 그 값이 여러 컬럼으로 쪼개져서
행의 필드 수가 헤더보다 많아진다("초과 컬럼"). 초과분은 곧 값 안에 있던 쉼표
개수이므로, 복구는 **M개 필드를 N개 컬럼으로 다시 묶는 분할 문제**가 된다.
어느 컬럼이 밀렸는지는 정상 행(필드 수가 정확히 N인 행)에서 배운 컬럼 프로파일
(타입 / 모양 / 값 집합 / 길이)에 후보 값이 얼마나 맞는지로 정한다.

전제는 하나: **쉼표를 품은 컬럼이 서로 이웃하지 않는다.** 이웃하면 어디까지가
앞 컬럼의 값인지 데이터만으로는 결정되지 않는다. 이 전제는 DP의 상태로 직접
강제한다(직전 컬럼이 합쳐졌으면 다음 컬럼은 합칠 수 없다).

**애매하면 복구하지 않는다.** 최적해와 2등해의 점수 차(margin)가 임계 미만이면
`CsvRepairError`를 낸다 - 자동 수집 경로에서 잘못 병합된 값은 그 순간부터
"데이터"가 되어 프로파일·관계 증거·probe 실측까지 전부 오염시키고, 결과 문서는
그게 측정값이라고 말한다. 못 하겠으면 못 하겠다고 하는 편이 싸다.

행 단위 판단을 눈으로 보려면 `tools/repair_csv.py`(같은 로직을 쓰는 CLI)로
report를 뽑을 것.
"""

from __future__ import annotations

import csv
import io
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import pandas as pd


class CsvRepairError(Exception):
    """쉼표 때문에 깨진 CSV 행을 자동으로 복구하지 못했을 때 발생한다."""


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
    """값 하나를 거친 타입 하나로 접는다. 컬럼의 '닫힌 정도'를 재는 기본 단위."""
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


# ------------------------------------------------------------- 컬럼 프로파일

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
    """정상 행에서만 배운 컬럼의 생김새. 여기에 추정값을 섞지 않는다."""

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
        """쉼표가 들어갈 자리가 애초에 없어 보이는 컬럼."""
        return self.categorical or self.numeric_ratio >= 0.9 or self.temporal_ratio >= 0.9

    @property
    def open_text(self) -> bool:
        """자유 서술이라 쉼표를 품을 만한 컬럼."""
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
        """숫자 컬럼에 붙은 1,234 - 깨진 값이 아니라 천 단위 구분자다."""
        return (
            "," in value
            and self.numeric_ratio >= 0.8
            and _RE_THOUSAND.match(value.strip()) is not None
        )

    def score(self, value: str) -> float:
        """이 값이 이 컬럼의 값일 법한 정도. 클수록 그럴듯하다."""
        if self.count == 0:
            return 0.0
        has_comma = "," in value
        thousand = self.thousand_like(value)
        # 천 단위 구분자는 서식 변형일 뿐이라 쉼표를 지운 모습으로 대조한다.
        # 그러지 않으면 정답인 숫자 컬럼이 '한 번도 못 본 모양'으로 크게 깎여
        # 옆 텍스트 컬럼이 이기는 역전이 난다.
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
                # 이 컬럼이 쉼표를 품은 적이 있는가. 없으면 표본 수만큼 벌점이 커진다.
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
    return [ColumnProfile.build(str(header[i]), cols[i]) for i in range(n)]


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
    """M개 필드를 N개 컬럼으로 다시 묶는다. 최적해와 2등해의 차이를 함께 낸다.

    dp[i][j][m] = 앞의 i개 컬럼이 앞의 j개 필드를 가져갔을 때의 상위 k개 점수.
    m은 직전 컬럼이 2개 이상을 합쳤는지 여부 - 이웃한 두 컬럼이 동시에 쉼표를
    품는 경우를 (기본값에서) 막기 위한 상태다.

    margin을 함께 내는 이유는 "정답이 이겼는지"와 "아무거나 이겼는지"가 점수
    하나로는 구분되지 않기 때문이다. 셋이 동점이면 margin은 0이 된다.
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
                    # 남은 컬럼들이 남은 필드를 소화할 수 있어야 한다.
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


# -------------------------------------------------------------- 행 단위 복구

# 2등해와 이만큼도 벌어지지 않으면 "데이터가 정하지 못했다"로 본다. 동점(margin 0)이
# 대표적인 경우이고, 전형적으로 쉼표를 품은 컬럼 둘이 붙어 있을 때 그렇게 된다.
AMBIGUOUS_MARGIN = 0.5
# 한 행이 가질 수 있는 초과 필드 상한. 이보다 벌어지면 분할 후보가 폭발하고,
# 그쯤 되면 쉼표 하나가 아니라 파일 자체가 다른 방식으로 깨진 것이다.
MAX_EXTRA_FIELDS = 6
# 복구된 행을 프로파일에 되먹이는 횟수. "이 컬럼은 원래 쉼표를 품는다"는 사실은
# 정상 행만 봐서는 알 수 없어서, 1차 결과를 근거에 넣고 한 번 더 본다.
DEFAULT_PASSES = 2

_STATUS_OK = "ok"
_STATUS_REPAIRED = "repaired"
_STATUS_AMBIGUOUS = "ambiguous"
_STATUS_UNDERFLOW = "underflow"
_STATUS_TOO_MANY = "too_many"
_STATUS_UNRESOLVED = "unresolved"

# 자동 경로가 그대로 흘려보낼 수 있는 상태. 나머지는 복구 실패다.
SETTLED = frozenset({_STATUS_OK, _STATUS_REPAIRED})


@dataclass
class RowRecord:
    """행 하나에 대한 판단. 복구 실패도 이유와 원본을 들고 남는다."""

    line: int
    raw_fields: int
    status: str
    values: Optional[List[str]] = None
    margin: Optional[float] = None  # None = 다른 해가 아예 없다(유일해)
    merged: Optional[List[str]] = None
    note: str = ""
    raw: List[str] = field(default_factory=list)


def read_rows(
    csv_path: str | Path, encoding: str = "utf-8", delimiter: str = ","
) -> Tuple[List[str], List[Tuple[int, List[str]]]]:
    """헤더와 (파일 줄번호, 필드들)을 읽는다. 따옴표가 제대로 걸린 값은 건드리지 않는다."""
    with open(csv_path, encoding=encoding, newline="") as f:
        reader = csv.reader(f, delimiter=delimiter)
        rows = [(reader.line_num, row) for row in reader]
    if not rows:
        return [], []
    return rows[0][1], rows[1:]


def repair_rows(
    header: Sequence[str],
    raw_rows: Sequence[Tuple[int, List[str]]],
    *,
    delimiter: str = ",",
    allow_adjacent: bool = False,
    max_extra: int = MAX_EXTRA_FIELDS,
    passes: int = DEFAULT_PASSES,
    ambiguous_margin: float = AMBIGUOUS_MARGIN,
) -> Tuple[List[RowRecord], List[ColumnProfile]]:
    """정상 행으로 프로파일을 만들고, 초과 필드가 있는 행을 되돌린다.

    필드 수가 헤더보다 **적은** 행은 이 함수의 대상이 아니다(값 누락이나 값 안의
    줄바꿈 등 다른 손상이다). 손대지 않고 `underflow`로 표시해 넘긴다.
    """
    n = len(header)
    clean = [list(row) for _, row in raw_rows if len(row) == n]
    profiles = build_profiles(header, clean)

    records: List[RowRecord] = []
    for round_no in range(max(1, passes)):
        records = []
        extra_corpus: List[List[str]] = []
        for line, row in raw_rows:
            m = len(row)
            if m == n:
                records.append(RowRecord(line, m, _STATUS_OK, list(row), raw=list(row)))
                continue
            if m < n:
                records.append(
                    RowRecord(
                        line, m, _STATUS_UNDERFLOW,
                        note="필드가 모자란다 - 값 안의 줄바꿈이나 잘린 행일 수 있다",
                        raw=list(row),
                    )
                )
                continue
            if m - n > max_extra:
                records.append(
                    RowRecord(
                        line, m, _STATUS_TOO_MANY,
                        note=f"초과 필드가 {m - n}개(상한 {max_extra})",
                        raw=list(row),
                    )
                )
                continue

            seg = segment_row(row, profiles, delimiter, allow_adjacent)
            if seg is None:
                records.append(
                    RowRecord(
                        line, m, _STATUS_UNRESOLVED,
                        note="쉼표를 품은 컬럼이 이웃하지 않는다는 전제 아래 가능한 재결합이 없다",
                        raw=list(row),
                    )
                )
                continue

            merged = [str(header[i]) for i in seg.merged_columns]
            settled = seg.margin >= ambiguous_margin
            records.append(
                RowRecord(
                    line,
                    m,
                    _STATUS_REPAIRED if settled else _STATUS_AMBIGUOUS,
                    seg.values,
                    None if math.isinf(seg.margin) else round(seg.margin, 3),
                    merged,
                    note="" if settled else "2등 해와 점수 차가 거의 없다(어느 컬럼이 깨졌는지 데이터가 정하지 못함)",
                    raw=list(row),
                )
            )
            if settled:
                extra_corpus.append(seg.values)

        if round_no + 1 >= passes or not extra_corpus:
            break
        profiles = build_profiles(header, clean + extra_corpus)

    return records, profiles


def repair_ragged_csv(csv_path: str | Path, encoding: str = "utf-8") -> pd.DataFrame:
    """초과 컬럼이 있는 CSV를 읽어 값 안의 ","로 쪼개진 컬럼을 복구한다.

    Args:
        csv_path: 복구할 CSV 파일 경로.
        encoding: 파일 인코딩.

    Returns:
        복구된 DataFrame.

    Raises:
        CsvRepairError: 초과 컬럼이 있는 행 중 하나라도 복구하지 못했을 때.
            어느 컬럼이 깨졌는지 데이터가 정하지 못한 경우(margin 부족)가
            대표적이고, 전형적으로 쉼표를 품은 컬럼 둘이 붙어 있는 상황이다.
    """
    header, raw_rows = read_rows(csv_path, encoding=encoding)
    if not header:
        return pd.DataFrame()

    records, _ = repair_rows(header, raw_rows)

    unfixed = [r for r in records if r.status not in SETTLED and r.status != _STATUS_UNDERFLOW]
    if unfixed:
        raise CsvRepairError(_unfixed_message(header, unfixed))

    # 복구된 행은 csv.reader가 나눈 문자열 그대로라, 여기서 바로 DataFrame을
    # 만들면 전부 object dtype이 되어 pd.read_csv가 원래 해주던 숫자/날짜 dtype
    # 추론을 잃어버린다(프로파일링 파이프라인이 dtype에 의존). 합쳐진 값 안에
    # 남은 ","는 csv.writer가 다시 따옴표로 감싸주므로, 텍스트로 왕복시켜
    # pd.read_csv에게 dtype 추론을 그대로 맡긴다.
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(r.values if r.values is not None else r.raw for r in records)
    buffer.seek(0)
    return pd.read_csv(buffer)


def _unfixed_message(header: Sequence[str], unfixed: Sequence[RowRecord], show: int = 3) -> str:
    """무엇을 어디까지 알아냈는지까지 담는다.

    "복구 실패"만 남기면 다음에 사람이 할 일이 파일을 처음부터 읽는 것뿐이다.
    최선의 후보와 그게 왜 부족했는지를 같이 주면 거기서부터 확인할 수 있다.
    """
    lines = [
        f"쉼표로 깨진 행 {len(unfixed)}개를 복구하지 못했습니다 (컬럼 {len(header)}개)."
    ]
    for rec in unfixed[:show]:
        lines.append(
            f"  {rec.line}행: 필드 {rec.raw_fields}개 / {rec.note or rec.status}"
        )
        if rec.values is not None:
            lines.append(f"    최선의 후보(margin={rec.margin}): {rec.values}")
        lines.append(f"    원본: {rec.raw}")
    if len(unfixed) > show:
        lines.append(f"  ... 외 {len(unfixed) - show}행")
    lines.append(
        "  행별 판단을 보려면: python tools/repair_csv.py <csv> --report report.json"
    )
    return "\n".join(lines)


__all__ = [
    "AMBIGUOUS_MARGIN",
    "MAX_EXTRA_FIELDS",
    "ColumnProfile",
    "CsvRepairError",
    "RowRecord",
    "SETTLED",
    "Segmentation",
    "build_profiles",
    "kind_of",
    "read_rows",
    "repair_ragged_csv",
    "repair_rows",
    "segment_row",
    "shape_of",
]
