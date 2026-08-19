.PHONY: help install test lint run batch robustness check clean exp-new exp-list exp-remove

help:
	@echo "install      의존성 설치"
	@echo "test         전체 테스트 (vLLM 불필요, 가짜 LLM으로 전 구간 실행)"
	@echo "lint         포맷/린트"
	@echo "check        LLM 엔드포인트 점검 (연결 + JSON 응답)"
	@echo "run          CSV 한 개 해석 (CSV=path [OUT=path])"
	@echo "batch        폴더 안 CSV 전부 해석 (DATA=dir [OUT=dir])"
	@echo "robustness   같은 CSV 반복 실행해 흔들림 측정 (DATA=dir OUT=file [REPS=n])"
	@echo "exp-new      새 실험 worktree 생성 (NAME=001-short-name)"
	@echo "exp-list     현재 worktree 목록"
	@echo "exp-remove   종료한 실험 worktree/브랜치 제거 (NAME=...)"

install:
	pip install -r requirements.txt
	pip install -r requirements-dev.txt

test:
	python -m pytest -q

lint:
	ruff check src experiments tests || true
	ruff format --check src experiments tests || true

check:
	python experiments/check_endpoint.py

run:
	@test -n "$(CSV)" || (echo "사용법: make run CSV=./data.csv [OUT=result.json]" && exit 2)
	python run.py "$(CSV)" $(if $(OUT),--output "$(OUT)",)

batch:
	@test -n "$(DATA)" || (echo "사용법: make batch DATA=./data [OUT=./results]" && exit 2)
	python experiments/run_batch.py --data-dir "$(DATA)" --out "$(or $(OUT),./results)"

robustness:
	@test -n "$(DATA)" || (echo "사용법: make robustness DATA=./data OUT=./results/robustness.jsonl [REPS=5]" && exit 2)
	python experiments/run_robustness.py --data-dir "$(DATA)" \
		--output "$(or $(OUT),./results/robustness.jsonl)" --reps "$(or $(REPS),5)"

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache

exp-new:
	@test -n "$(NAME)" || (echo "사용법: make exp-new NAME=001-short-name" && exit 2)
	@case "$(NAME)" in *[!a-z0-9-]*|-*|*-) echo "NAME은 소문자/숫자/하이픈만 쓰고 하이픈으로 시작·종료하지 마세요"; exit 2;; esac
	git fetch origin --prune
	git worktree add -b "exp/$(NAME)" ".worktrees/$(NAME)" origin/main
	git -C ".worktrees/$(NAME)" branch --unset-upstream
	@echo "생성 완료: cd .worktrees/$(NAME)"

exp-list:
	git worktree list

exp-remove:
	@test -n "$(NAME)" || (echo "사용법: make exp-remove NAME=001-short-name" && exit 2)
	@case "$(NAME)" in *[!a-z0-9-]*|-*|*-) echo "NAME은 소문자/숫자/하이픈만 쓰고 하이픈으로 시작·종료하지 마세요"; exit 2;; esac
	@test "$$(git -C ".worktrees/$(NAME)" rev-parse --abbrev-ref HEAD)" = "exp/$(NAME)" || (echo "worktree와 브랜치 이름이 일치하지 않습니다" && exit 2)
	@test -z "$$(git -C ".worktrees/$(NAME)" status --porcelain)" || (echo "커밋되지 않은 변경이 있어 정리하지 않습니다" && exit 2)
	@if git worktree 2>&1 | grep -q "worktree remove"; then git worktree remove ".worktrees/$(NAME)"; else rm -rf -- ".worktrees/$(NAME)" && git worktree prune; fi
	git branch -d "exp/$(NAME)"
