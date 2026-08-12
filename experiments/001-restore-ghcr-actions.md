# EXP-001: GHCR Actions 복원 및 실험 브랜치 빌드

## 가설

- 가설: 현재 코드 구조에 맞는 Dockerfile과 `exp/**` push trigger를 복원하면 실험
  브랜치마다 운영 이미지를 덮지 않는 독립 GHCR 이미지를 만들 수 있다.
- 바꾸는 변수(한 가지): GHCR workflow 및 컨테이너 빌드 구성
- 고정 조건: Python 3.11, linux/amd64, 동일 requirements, 동일 GHCR package
- 성공 기준: 테스트와 이미지 build/push가 성공하고, SHA 태그 및
  `exp-001-restore-ghcr-actions` 태그가 생성된다.

## 재현 정보

- baseline SHA: `f5e53fcc62cda44bbf75379b6d6e8ae82c8732bc`
- experiment SHA: `2d3fa7ca5b5588a8a7a042c673c74929f4c7830d`
- 실행 환경: GitHub-hosted `ubuntu-latest`
- 이미지: `ghcr.io/seongwonm/sh-ard-asset-agent`
- 실행 명령: `git push -u origin exp/001-restore-ghcr-actions`

## 결과

- Actions test: 첫 run `#53` 성공(28초)
- Docker build/push: 첫 run은 Buildx builder 없이 `type=gha` cache export를 사용해
  실패. `docker/setup-buildx-action@v4`를 추가하고 Docker Actions를 Node 24 기반
  현행 major로 갱신한 뒤 run `#54` 성공(테스트 29초, build/push 1분 11초).
- 생성된 태그: `2d3fa7ca5b5588a8a7a042c673c74929f4c7830d`,
  `exp-001-restore-ghcr-actions`
- 이상 현상: 최초 복원에 사용한 고정 Action SHA들이 Node 20 deprecation 경고를 냈다.

## 결론

- 결정: `adopt`
- 이유: `exp/**` push에서 테스트, Docker build, GHCR push와 두 태그 생성을 확인했다.
- 다음 실험: `main` 반영 후 운영용 SHA manifest 자동 갱신 확인
