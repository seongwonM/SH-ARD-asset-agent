"""
외부 환경용 mock-up 데이터셋 생성기.

사내 데이터를 반출할 수 없으므로, 같은 **구조적 성질**을 갖는 합성 데이터를 만든다.
성능 측정에서 중요한 건 값 자체가 아니라 컬럼의 성질이다:
유일성, 카디널리티, 시간 해상도, 함수 종속, PII 형태.

각 데이터셋은 정답(ground truth)을 함께 낸다. 컬럼별 kind와 식별자 role,
입도 키, PII 등급은 생성 시점에 알고 있으므로, 벤치가 이를 정답지로 쓴다.
LLM이 만든 자연어 요약은 정답이 없지만, **이 구조적 항목들은 채점할 수 있다.**

    python examples/make_mock_data.py --out data/mock --rows 500
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

SEED = 20260812


def _dates(n: int, start: str, unit: str, rng: random.Random) -> list[str]:
    base = datetime.fromisoformat(start)
    if unit == "day":
        return [(base + timedelta(days=rng.randint(0, 120))).strftime("%Y-%m-%d") for _ in range(n)]
    return [
        (base + timedelta(minutes=rng.randint(0, 120 * 24 * 60))).strftime("%Y-%m-%d %H:%M:%S")
        for _ in range(n)
    ]


# ---------------------------------------------------------------------------
# 데이터셋 정의
# ---------------------------------------------------------------------------


def process_log(rows: int, rng: random.Random):
    """장비 공정 로그. 시간축 + 측정값 + 판정. equipment_id -> line_id 함수 종속."""
    line_of = {f"C{i:02d}": f"L{(i % 3) + 1}" for i in range(1, 9)}
    equipment = [rng.choice(list(line_of)) for _ in range(rows)]
    power = [round(rng.gauss(500, 18), 1) for _ in range(rows)]
    df = pd.DataFrame(
        {
            "run_id": [f"P{i:06d}" for i in range(rows)],
            "equipment_id": equipment,
            "line_id": [line_of[e] for e in equipment],
            "run_at": _dates(rows, "2026-03-01", "day", rng),
            "power_value": power,
            "chamber_temp": [round(rng.gauss(215, 4), 2) for _ in range(rows)],
            "verdict": ["정상" if 470 <= p <= 530 else "이상" for p in power],
        }
    )
    truth = {
        "kinds": {
            "run_id": "identifier",
            "equipment_id": "identifier",
            # line_id는 spatial 후보이지만 조인 키로 쓰이므로 identifier가 맞다.
            # 다운스트림에서 linkage(연결 가능 키)로 잡히는 쪽이 유용하다.
            "line_id": "identifier",
            "run_at": "temporal",
            "power_value": "numeric",
            "chamber_temp": "numeric",
            "verdict": "categorical",
        },
        "identifier_roles": {"run_id": "primary", "equipment_id": "reference"},
        "grain_keys": ["run_id"],
        "time_resolution": {"run_at": "Day"},
        "functional_dependencies": [["equipment_id", "line_id"]],
        "pii": {},
        "categories": {"verdict": ["정상", "이상"]},
    }
    meta = {
        "source_description": "반도체 식각 장비별 공정 실행 시점과 측정된 장비 출력값, 챔버 온도, 판정 결과를 기록한 테이블이다.",
        "domain": "semiconductor",
        "column_descriptions": [
            {"컬럼명": "equipment_id", "표준용어": "설비ID", "표준용어_내용": "설비를 식별하는 표준 용어", "설명": "공정 수행 장비"},
            {"컬럼명": "power_value", "표준용어": "설비출력", "표준용어_내용": "설비의 실제 출력값", "설명": "RF Generator 실측 출력", "추가설명": "단위는 W"},
            {"컬럼명": "chamber_temp", "설명": "챔버 내부 온도", "추가설명": "단위는 섭씨"},
            {"컬럼명": "verdict", "설명": "공정 결과 판정"},
        ],
    }
    return df, truth, meta


def maintenance_history(rows: int, rng: random.Random):
    """정비 이력. 자유 텍스트 + 시각 해상도(분)."""
    types = ["정기점검", "예방정비", "긴급수리", "부품교체"]
    # 자유 텍스트는 조합으로 만든다. 고정 문장 4개를 반복하면 카디널리티가
    # 낮아 실제로는 범주형이 되어버린다 - 정답 라벨과 데이터가 어긋난다.
    _parts_a = ["챔버 내부 세정", "RF 매칭 네트워크 튜닝", "펌프 오일 교체", "가스 라인 MFC 캘리브레이션",
                "히터 단선 점검", "쿨링 라인 플러싱", "게이트 밸브 시트 교체"]
    _parts_b = ["오링 교체", "파라미터 재조정", "진공도 확인", "유량 오차 보정",
                "출력 편차 재측정", "누설 시험", "인터록 동작 확인"]
    _parts_c = ["작업 후 정상 판정", "재발 감시 필요", "부품 발주 요청", "익일 재점검 예정", "이상 없음"]
    actions = [
        f"{a}을(를) 수행하고 {b}을(를) 실시함. {c}."
        for a in _parts_a for b in _parts_b for c in _parts_c
    ]
    df = pd.DataFrame(
        {
            "maintenance_id": [f"M{i:06d}" for i in range(rows)],
            "equipment_id": [f"C{rng.randint(1, 8):02d}" for _ in range(rows)],
            "maintenance_at": _dates(rows, "2026-03-01", "minute", rng),
            "maintenance_type": [rng.choice(types) for _ in range(rows)],
            "duration_min": [rng.randint(15, 480) for _ in range(rows)],
            "action_detail": [rng.choice(actions) for _ in range(rows)],
        }
    )
    truth = {
        "kinds": {
            "maintenance_id": "identifier",
            "equipment_id": "identifier",
            "maintenance_at": "temporal",
            "maintenance_type": "categorical",
            "duration_min": "numeric",
            "action_detail": "free_text",
        },
        "identifier_roles": {"maintenance_id": "primary", "equipment_id": "reference"},
        "grain_keys": ["maintenance_id"],
        "time_resolution": {"maintenance_at": "Minute"},
        "functional_dependencies": [],
        "pii": {},
        "categories": {"maintenance_type": types},
    }
    meta = {
        "source_description": "제조 장비별 정비 시점과 정비 유형, 소요 시간, 조치 내용을 기록한 테이블이다.",
        "domain": "semiconductor",
        "column_descriptions": [
            {"컬럼명": "equipment_id", "표준용어": "설비ID", "설명": "정비 대상 장비"},
            {"컬럼명": "duration_min", "설명": "정비 소요 시간", "추가설명": "단위는 분"},
        ],
    }
    return df, truth, meta


def operator_roster(rows: int, rng: random.Random):
    """작업자 명부. PII 탐지 대상이 있는 유일한 데이터셋."""
    surnames = ["김", "이", "박", "최", "정", "강", "조", "윤", "장", "임", "한", "오"]
    givens = ["민준", "서연", "도윤", "하윤", "지호", "수아", "예준", "지우", "시우", "서준",
              "하은", "채원", "유진", "건우", "다인", "ราน".replace("ราน", "소율")]
    shifts = ["주간", "야간", "심야"]
    names = [rng.choice(surnames) + rng.choice(givens) for _ in range(rows)]
    df = pd.DataFrame(
        {
            "operator_id": [f"OP{i:05d}" for i in range(rows)],
            "operator_name": names,
            "contact_email": [f"user{i:05d}@corp.example.com" for i in range(rows)],
            "mobile": [f"010-{rng.randint(1000,9999)}-{rng.randint(1000,9999)}" for _ in range(rows)],
            "line_id": [f"L{rng.randint(1,3)}" for _ in range(rows)],
            "shift": [rng.choice(shifts) for _ in range(rows)],
            "hired_on": _dates(rows, "2020-01-01", "day", rng),
        }
    )
    truth = {
        "kinds": {
            "operator_id": "identifier",
            "operator_name": "free_text",
            "contact_email": "identifier",
            "mobile": "identifier",
            "line_id": "identifier",
            "shift": "categorical",
            "hired_on": "temporal",
        },
        "identifier_roles": {"operator_id": "primary"},
        "grain_keys": ["operator_id"],
        "time_resolution": {"hired_on": "Day"},
        "functional_dependencies": [],
        # PII 정답. 벤치가 재현율/정밀도를 잰다.
        "pii": {"operator_name": "direct", "contact_email": "direct", "mobile": "direct"},
        "categories": {"shift": shifts},
    }
    meta = {
        "source_description": "생산 라인 작업자의 소속과 근무조, 입사일을 관리하는 명부이다.",
        "domain": "semiconductor",
        "column_descriptions": [
            {"컬럼명": "line_id", "표준용어": "라인ID", "설명": "소속 생산 라인"},
            {"컬럼명": "shift", "설명": "근무조"},
        ],
    }
    return df, truth, meta


def wide_sensor(rows: int, rng: random.Random):
    """넓은 테이블. 배치 효율과 비용 스케일링 측정용."""
    data = {"sample_id": [f"S{i:06d}" for i in range(rows)]}
    kinds = {"sample_id": "identifier"}
    for i in range(1, 25):
        col = f"sensor_{i:02d}"
        data[col] = [round(rng.gauss(50 + i, 3), 3) for _ in range(rows)]
        kinds[col] = "numeric"
    df = pd.DataFrame(data)
    truth = {
        "kinds": kinds,
        "identifier_roles": {"sample_id": "primary"},
        "grain_keys": ["sample_id"],
        "time_resolution": {},
        "functional_dependencies": [],
        "pii": {},
        "categories": {},
    }
    meta = {
        "source_description": "샘플별 센서 24종의 측정값을 기록한 테이블이다.",
        "domain": "semiconductor",
        "column_descriptions": [],
    }
    return df, truth, meta


DATASETS = {
    "process_log": process_log,
    "maintenance_history": maintenance_history,
    "operator_roster": operator_roster,
    "wide_sensor": wide_sensor,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/mock")
    ap.add_argument("--rows", type=int, default=500)
    ap.add_argument("--only", nargs="*", help="일부만 생성")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    names = args.only or list(DATASETS)

    for name in names:
        rng = random.Random(SEED + sum(map(ord, name)))
        df, truth, meta = DATASETS[name](args.rows, rng)
        df.to_csv(out / f"{name}.csv", index=False, encoding="utf-8")
        (out / f"{name}_metadata.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # 정답지는 벤치만 읽는다. 파이프라인 입력으로는 절대 들어가지 않는다.
        (out / f"{name}_truth.json").write_text(
            json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"{name}: {len(df)}행 x {len(df.columns)}열")

    print(f"\n{out}/ 에 {len(names)}개 데이터셋 생성 (seed 고정)")


if __name__ == "__main__":
    main()
