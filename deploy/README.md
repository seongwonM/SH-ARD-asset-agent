# GHCR 이미지 배포

`.github/workflows/build-push.yml`은 `main`과 `exp/**` 브랜치 push를 테스트한 뒤
`ghcr.io/seongwonm/sh-ard-asset-agent`에 이미지를 발행한다.

## 태그 규칙

- 모든 브랜치: 전체 Git 커밋 SHA — 재현과 고정 실행에 사용
- `main`: `main`, `latest`, 전체 SHA
- `exp/001-example`: `exp-001-example`, 전체 SHA

브랜치 태그는 해당 브랜치의 최신 성공 이미지를 가리키는 가변 별칭이다. 실험 결과를
기록하거나 k8s에서 정확히 재현할 때는 항상 전체 SHA 태그를 사용한다.

## manifest 자동 갱신

`main`이든 `exp/**`든, push된 브랜치의 `k8s/pod.yaml`, `k8s/robustness-job.yaml`,
`k8s/column-poc-job.yaml` 이미지 태그를 그 브랜치에서 방금 빌드한 SHA로 갱신해
같은 브랜치에 다시 커밋한다. 그래서 어느 브랜치에서든 `kubectl apply -f k8s/*.yaml`을
그대로 쓰면 그 브랜치의 최신 코드가 담긴 이미지가 돈다 — 별도로 이미지 태그를
맞춰줄 필요가 없다.

과거에는 `main`만 갱신하고 실험 브랜치는 `kubectl set image`로 일회성 교체가
필요했지만, 지금은 그 워크어라운드가 필요 없다.

## 로컬 빌드

```bash
docker build -f deploy/Dockerfile \
  -t ghcr.io/seongwonm/sh-ard-asset-agent:local .

docker run --rm ghcr.io/seongwonm/sh-ard-asset-agent:local \
  python run.py --help

# skills/*.md가 실제로 이미지에 들어갔는지 확인 - .dockerignore가 루트 문서만
# 제외하도록 되어 있는데, 이 규칙이 넓어지면 프롬프트가 통째로 빠진다.
docker run --rm ghcr.io/seongwonm/sh-ard-asset-agent:local \
  python -c "import pathlib; print(sorted(p.name for p in pathlib.Path('skills').glob('*.md')))"
```
