.PHONY: help install test trace mock check bench bench-offline lint clean

help:
	@echo "install     의존성 설치"
	@echo "test        전체 테스트 (vLLM 불필요, mock으로 실행)"
	@echo "trace       에이전트 실행 궤적 출력"
	@echo "mock        벤치용 mock 데이터셋 생성"
	@echo "check       vLLM 엔드포인트 점검 (structured output 강제 여부)"
	@echo "bench-offline  LLM 없이 하네스 오버헤드 측정"
	@echo "bench       실제 vLLM으로 성능 측정"
	@echo "lint        포맷/린트"

install:
	pip install -r requirements.txt
	pip install -r requirements-dev.txt

test:
	python -m pytest -q

trace:
	PYTHONPATH=src:tests python tests/test_agent.py

mock:
	python examples/make_mock_data.py --out data/mock --rows 500

check:
	python bench/check_endpoint.py

bench-offline: mock
	python bench/run_bench.py --offline --reps 3

bench: mock
	python bench/run_bench.py --reps 5

lint:
	ruff check src skills tests || true
	ruff format --check src skills tests || true

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache
