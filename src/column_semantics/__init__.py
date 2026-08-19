"""CSV 컬럼 의미 해석 파이프라인.

레이어는 세 개뿐이다.

    core/       DataFrame -> 사실. 순수 계산. 네트워크/파일/환경변수를 모르고,
                입력이 같으면 출력이 같다. 테스트에 LLM이 필요 없는 구간.
    adapters/   바깥 세계와 닿는 부분. CSV 파일, LLM 엔드포인트, skills 폴더,
                환경변수. 여기만 교체하면 다른 실행 환경에 붙는다.
    pipeline/   core의 사실과 adapters의 LLM을 조립해 해석을 만든다.

의존 방향은 pipeline -> (core, adapters) 한 방향이다. core는 adapters를
import하지 않는다 - 이 규칙이 깨지면 "LLM 없이 프로파일링만 검증"이 불가능해진다.
"""

__all__ = ["__version__"]

__version__ = "0.2.0"
