# 실험 운영 규칙

이 저장소는 `main`을 안정적인 기준선으로 두고, 코드 가설마다 별도 브랜치와
`git worktree`를 만든다. 한 실험이 실행 중이어도 다른 폴더에서 다음 가설을
수정할 수 있다.

## 핵심 구분

- **실험(experiment)**: 코드나 프롬프트가 달라지는 하나의 가설. 브랜치 하나를 쓴다.
- **실행(run)**: 같은 코드에서 seed, 모델, 데이터, 반복 횟수만 달리한 측정. 새 브랜치를
  만들지 않고 같은 실험 브랜치에서 여러 번 실행한다.
- **기준선(baseline)**: 비교 기준이 되는 `main`의 특정 커밋. 움직이는 브랜치 이름만
  적지 말고 반드시 커밋 SHA를 기록한다.

## 이름 규칙

- 브랜치: `exp/<번호>-<짧은-가설>`
- worktree: `.worktrees/<번호>-<짧은-가설>`
- 실험 기록: `experiments/<번호>-<짧은-가설>.md`

예: `exp/001-profile-kind-threshold`

번호는 `001`부터 순서대로 증가시킨다. 이름에는 구현 내용보다 검증할 가설을 담는다.

## 실험 시작

기준 작업 폴더에서:

```bash
git checkout main
git pull --ff-only
make exp-new NAME=001-profile-kind-threshold
cd .worktrees/001-profile-kind-threshold
cp experiments/_template.md experiments/001-profile-kind-threshold.md
```

템플릿에 가설, 바꾸는 변수 하나, 고정 조건, 성공 기준을 먼저 적는다. 그다음 코드를
수정하고 테스트한다.

```bash
make test
git add <수정한 파일> experiments/001-profile-kind-threshold.md
git commit -m "exp(001): profile kind 임계값 가설"
git push -u origin exp/001-profile-kind-threshold
git rev-parse HEAD
make bench
```

실행 전에 반드시 커밋하고 SHA를 기록한다. 실행 중 코드를 수정하면 어떤 코드가 결과를
만들었는지 알 수 없어진다.

## 병렬로 진행하기

첫 번째 worktree에서 벤치마크가 도는 동안 기준 작업 폴더로 돌아와 다음 worktree를
만든다.

```bash
make exp-new NAME=002-probe-uniqueness-threshold
cd .worktrees/002-probe-uniqueness-threshold
```

각 worktree는 브랜치, index, 수정 파일이 독립적이지만 Git 객체와 원격 설정은 공유한다.
같은 브랜치를 두 worktree에서 동시에 checkout하지 않는다.

성능 실험은 실행 자원도 통제한다. 같은 vLLM endpoint/GPU에 두 벤치를 동시에 보내면
latency, QPS, TPS 비교가 오염된다. **코드 수정은 병렬**, 같은 서버를 쓰는 성능 측정은
한 번에 하나가 기본이다. 동시 부하 자체가 가설일 때만 병렬 실행한다.

## 결과 기록

`results/`의 원시 출력은 Git에 넣지 않는다. 대신 실험 기록에는 다음을 남긴다.

- baseline SHA와 experiment SHA
- 데이터 이름/버전 또는 해시
- 모델, endpoint, structured mode, 주요 환경값
- 실행 명령과 반복 횟수
- baseline과 experiment의 핵심 지표
- 관찰한 이상 현상
- `adopt` / `reject` / `inconclusive` 결론과 이유

한 실험에서 여러 run을 수행했다면 평균만 쓰지 말고 각 run 값과 분산도 남긴다.

## 종료

1. 결과를 실험 기록에 채우고 커밋·push한다.
2. `adopt`면 PR로 `main`에 병합하고 `main`에서 전체 테스트를 다시 실행한다.
3. `reject`/`inconclusive`도 원격 브랜치와 결과 기록을 남겨 같은 실패를 반복하지 않는다.
4. 병합 또는 보존 확인 후 로컬 worktree를 정리한다.

```bash
cd /Users/a11793/projects/asset-context-agent
make exp-remove NAME=001-profile-kind-threshold
```

`exp-remove`는 수정 파일이 남아 있으면 worktree를 지우지 않는다. 브랜치가 병합되지
않았다면 worktree만 정리되고 마지막 `git branch -d`가 안전하게 실패한다. 기각 실험
브랜치를 로컬에서도 지우려면 결과 push를 확인한 뒤 별도로
`git branch -D exp/<이름>`을 실행한다.

실험 브랜치를 push하면 GitHub Actions가 같은 코드를 GHCR에도 발행한다. 예를 들어
`exp/001-profile-kind-threshold`의 최신 이미지는
`ghcr.io/seongwonm/sh-ard-asset-agent:exp-001-profile-kind-threshold`로 찾을 수 있다.
정확한 재현에는 움직이는 브랜치 태그 대신 커밋 SHA 태그를 사용한다.

## 도구를 더 붙이는 시점

지금은 Git + worktree + 기존 벤치마크로 시작한다. 아래 상황이 오면 확장한다.

- 데이터/대형 artifact 버전 관리가 필요하면 DVC
- run이 수십~수백 개로 늘고 지표 검색·차트·팀 공유가 필요하면 MLflow
- 같은 파라미터 조합을 자동 병렬 탐색해야 하면 별도 sweep 도구

브랜치는 코드 가설을 관리하고, 추적 도구는 run과 artifact를 관리한다. 둘은 대체 관계가
아니다.
