# GHCR 이미지 배포

`.github/workflows/build-push.yml`은 `main`과 `exp/**` 브랜치 push를 테스트한 뒤
`ghcr.io/seongwonm/sh-ard-asset-agent`에 이미지를 발행한다.

## 태그 규칙

- 모든 브랜치: 전체 Git 커밋 SHA — 재현과 고정 실행에 사용
- `main`: `main`, `latest`, 전체 SHA
- `exp/001-example`: `exp-001-example`, 전체 SHA

브랜치 태그는 해당 브랜치의 최신 성공 이미지를 가리키는 가변 별칭이다. 실험 결과를
기록하거나 k8s에서 정확히 재현할 때는 항상 전체 SHA 태그를 사용한다.

## main과 실험 이미지의 차이

`main` 빌드만 `k8s/pod.yaml`, `k8s/robustness-job.yaml`, `k8s/asset-run-job.yaml`의
이미지 태그를 빌드한 SHA로 갱신해 다시 커밋한다. `exp/**` 빌드는 GHCR에 이미지를
발행하지만 운영 manifest를 수정하지 않는다.

실험 이미지를 내부 환경에서 직접 확인하려면 manifest를 커밋하지 말고 일회성으로
이미지만 교체한다.

```bash
kubectl set image pod/sh-ard-asset-agent \
  sh-ard-asset-agent=ghcr.io/seongwonm/sh-ard-asset-agent:<experiment-sha>
```

## 로컬 빌드

```bash
docker build -f deploy/Dockerfile \
  -t ghcr.io/seongwonm/sh-ard-asset-agent:local .

docker run --rm ghcr.io/seongwonm/sh-ard-asset-agent:local \
  python -c "from agent.runner import TableAssetContextRunner; print('ok')"
```
