.PHONY: help install test trace mock check bench bench-offline lint clean exp-new exp-list exp-remove

help:
	@echo "install     의존성 설치"
	@echo "test        전체 테스트 (vLLM 불필요, mock으로 실행)"
	@echo "trace       에이전트 실행 궤적 출력"
	@echo "mock        벤치용 mock 데이터셋 생성"
	@echo "check       vLLM 엔드포인트 점검 (structured output 강제 여부)"
	@echo "bench-offline  LLM 없이 하네스 오버헤드 측정"
	@echo "bench       실제 vLLM으로 성능 측정"
	@echo "lint        포맷/린트"
	@echo "exp-new     새 실험 worktree 생성 (NAME=001-short-name)"
	@echo "exp-list    현재 worktree 목록"
	@echo "exp-remove  종료한 실험 worktree/브랜치 제거 (NAME=...)"

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

exp-new:
	@test -n "$(NAME)" || (echo "사용법: make exp-new NAME=001-short-name" && exit 2)
	@case "$(NAME)" in *[!a-z0-9-]*|-*|*-) echo "NAME은 소문자/숫자/하이픈만 쓰고 하이픈으로 시작·종료하지 마세요"; exit 2;; esac
	git fetch origin --prune
	git worktree add -b "codex/exp/$(NAME)" ".worktrees/$(NAME)" origin/main
	git -C ".worktrees/$(NAME)" branch --unset-upstream
	@echo "생성 완료: cd .worktrees/$(NAME)"

exp-list:
	git worktree list

exp-remove:
	@test -n "$(NAME)" || (echo "사용법: make exp-remove NAME=001-short-name" && exit 2)
	@case "$(NAME)" in *[!a-z0-9-]*|-*|*-) echo "NAME은 소문자/숫자/하이픈만 쓰고 하이픈으로 시작·종료하지 마세요"; exit 2;; esac
	@test "$$(git -C ".worktrees/$(NAME)" rev-parse --abbrev-ref HEAD)" = "codex/exp/$(NAME)" || (echo "worktree와 브랜치 이름이 일치하지 않습니다" && exit 2)
	@test -z "$$(git -C ".worktrees/$(NAME)" status --porcelain)" || (echo "커밋되지 않은 변경이 있어 정리하지 않습니다" && exit 2)
	@if git worktree 2>&1 | grep -q "worktree remove"; then git worktree remove ".worktrees/$(NAME)"; else rm -rf -- ".worktrees/$(NAME)" && git worktree prune; fi
	git branch -d "codex/exp/$(NAME)"
