.PHONY: help install test trace lint clean

help:
	@echo "install  의존성 설치"
	@echo "test     전체 테스트 (vLLM 불필요, mock으로 실행)"
	@echo "trace    에이전트 실행 궤적 출력"
	@echo "lint     포맷/린트"

install:
	pip install -r requirements.txt
	pip install -r requirements-dev.txt

test:
	python -m pytest -q

trace:
	PYTHONPATH=src:tests python tests/test_agent.py

lint:
	ruff check src skills tests || true
	ruff format --check src skills tests || true

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache
