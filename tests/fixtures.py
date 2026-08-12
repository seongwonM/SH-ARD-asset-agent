"""테스트 공용 픽스처. 여기의 MockDeps를 각 테스트가 상속해 변형한다."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from agent.contract import SkillDeps

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"

# 의도적으로 함정을 넣은 표본
#   run_id      유일 -> primary 정당
#   equipment_id 반복 -> primary 주장은 데이터가 반증
#   run_at      날짜만 -> Hour 해상도 주장은 데이터가 반증
#   power_value 정수 -> datetime 오분류 유발 가능
#   verdict     2종 -> 표본에 없는 카테고리 설명은 데이터가 반증
DF = pd.DataFrame(
    [
        {"run_id": "P001", "equipment_id": "C03", "run_at": "2026-07-07", "power_value": 501, "verdict": "정상"},
        {"run_id": "P002", "equipment_id": "C03", "run_at": "2026-07-09", "power_value": 523, "verdict": "이상"},
        {"run_id": "P003", "equipment_id": "C04", "run_at": "2026-07-10", "power_value": 478, "verdict": "이상"},
        {"run_id": "P004", "equipment_id": "C03", "run_at": "2026-07-13", "power_value": 501, "verdict": "정상"},
    ]
)

SOURCE_DESC = "제조 장비별 공정 실행 시점과 측정된 장비 출력값을 기록한 테이블이다."


def col_of(user_msg: str) -> str:
    """프롬프트에서 대상 컬럼명을 뽑는다."""
    lines = [l for l in user_msg.splitlines() if l.startswith("- 컬럼명:")]
    return lines[0].split(":", 1)[1].strip() if lines else ""


class MockDeps(SkillDeps):
    """정상 동작 mock. 1차 시도에 오류를 내고 재시도에서 교정한다."""

    def __init__(self):
        self.calls = 0

    def dataframe(self, ref: str):
        return DF

    async def structured(self, messages, model_cls, stage):
        self.calls += 1
        user = messages[-1]["content"]
        retry = "직전 출력의 문제" in user or "직전 문제" in user
        name = model_cls.__name__

        if name == "TemporalOut":
            return model_cls(
                meaning="각 공정 실행이 발생한 날짜이다.",
                resolution="Day" if retry else "Hour",  # 1차: 데이터가 반증할 주장
                role="event",
                confidence=0.95,
            )
        if name == "MeasureOut":
            return model_cls(
                meaning="공정 중 측정된 장비 출력값이다.",
                unit="" if retry else "kPa",  # 1차: 근거 없는 단위
                unit_evidence="not_found" if retry else "column_name",
                usage="출력 변동 모니터링",
                confidence=0.93,
            )
        if name == "IdentifierOut":
            col = col_of(user)
            if col == "run_id":
                return model_cls(meaning="개별 공정 실행을 식별하는 값이다.", role="primary",
                                 refers_to="", confidence=0.96)
            # 1차: equipment_id를 primary로 주장 -> uniqueness probe가 반증
            return model_cls(
                meaning="공정을 수행한 장비를 식별하는 값이다.",
                role="reference" if retry else "primary",
                refers_to="",
                confidence=0.9,
            )
        if name == "CategoryOut":
            return model_cls(
                meaning="공정 결과의 정상 여부 구분이다.",
                observed_values=(
                    ["정상: 기준을 만족", "이상: 기준을 벗어남"]
                    if retry
                    else ["정상: 기준을 만족", "이상: 기준을 벗어남", "보류: 판정 유예"]  # '보류'는 없는 값
                ),
                confidence=0.9,
            )
        if name == "GenericOut":
            return model_cls(meaning="분류가 명확하지 않은 컬럼이다.", usage="", confidence=0.6)
        if name == "GrainOut":
            return model_cls(
                grain="한 행은 한 장비에서 수행된 공정 실행 1건이다.",
                key_columns=["run_id"] if retry else ["equipment_id"],  # 1차: 유일하지 않은 키
                confidence=0.94,
            )
        if name == "ContextOut":
            return model_cls(
                topic="장비 공정 출력",
                summary="이 데이터셋은 제조 장비에서 수행된 공정 실행 건별로 실행 시점과 측정된 출력값, "
                        "결과 판정을 기록한다. 공정 실행 단위로 장비 출력의 변동을 확인할 수 있다.",
                key_points=["공정 실행 이력", "장비 출력 측정"],
                use_cases=["공정별 출력 변동 분석", "이상 판정 건 확인"],
                search_terms=["공정 로그", "장비 출력", "설비 출력", "power_value", "run_id"],
                confidence=0.92,
            )
        raise AssertionError(f"예상치 못한 모델: {name}")


def make_state(**kw):
    from agent.state import new_state

    base = dict(
        asset_id="table-001",
        asset_name="process_log",
        data_ref="runtime://table-001",
        source_description=SOURCE_DESC,
    )
    base.update(kw)
    return new_state(**base)


def print_trace(result):
    for h in result["history"]:
        if h.get("phase") == "plan":
            if h.get("chosen"):
                print(f"  [plan] {h['note']}")
        else:
            mark = "OK  " if h.get("ok") else "FAIL"
            info = (
                f"slots={h.get('slots')} claims={h.get('claims_verified',0)} tries={h.get('attempts')}"
                if h.get("ok")
                else h.get("reason", "")[:80]
            )
            print(f"    [{mark}] {h['skill']}::{h.get('target','')} {info}")
