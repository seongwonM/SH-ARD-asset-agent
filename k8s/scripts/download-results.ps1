<#
.SYNOPSIS
  PVC(/data/results 아래)의 결과를 로컬로 내려받는다. upload-assets.ps1의 반대 방향.

.DESCRIPTION
  디버그 Pod(k8s/pod.yaml)가 없으면 띄우고, 그 Pod를 경유해 kubectl cp로
  /data/results 전체 또는 -Target으로 지정한 하위 경로만 로컬로 복사한다.

  asset-run-job.yaml 결과: /data/results/<asset>.json 여러 개 (Target 생략하고 전체 받기)
  robustness-job.yaml 결과: /data/results/exp{N}_{타임스탬프}/ 폴더(run.log + jsonl)
    (-Target exp3_202608131000 처럼 실험 하나만 지정 가능)

.PARAMETER LocalDir
  결과를 받을 로컬 디렉터리. 없으면 생성한다.

.PARAMETER Target
  PVC 안 /data/results 아래에서 받아올 하위 경로. 생략하면 results 전체를 받는다.

.PARAMETER PodName
  경유할 디버그 Pod 이름. k8s/pod.yaml의 metadata.name과 맞춘다.

.PARAMETER Namespace
  대상 네임스페이스. 생략하면 환경변수 K8S_NAMESPACE, 그다음 .env의
  K8S_NAMESPACE를 차례로 본다. 아무 것도 없으면 kubectl 현재 컨텍스트의
  기본 네임스페이스를 쓴다.

.EXAMPLE
  ./k8s/scripts/download-results.ps1 -LocalDir .\results
  ./k8s/scripts/download-results.ps1 -LocalDir .\results -Target exp3_202608131000
  ./k8s/scripts/download-results.ps1 -LocalDir .\results -Target my_asset.json
#>
param(
    [string]$LocalDir = "results",

    [string]$Target = "",

    [string]$PodYaml = "k8s/pod.yaml",

    [string]$PodName = "sh-ard-asset-agent",

    [string]$Namespace = ""
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot/_common.ps1"

$envVars = Read-DotEnv ".env"
$Namespace = Get-K8sNamespace -EnvVars $envVars -Namespace $Namespace
$nsArgs = Get-K8sNamespaceArgs -Namespace $Namespace

$podPhase = kubectl get pod $PodName @nsArgs -o jsonpath="{.status.phase}" 2>$null
if (-not $podPhase) {
    Write-Host "디버그 Pod($PodName) 생성..."
    kubectl apply @nsArgs -f $PodYaml | Out-Null
}
Write-Host "Pod Ready 대기..."
kubectl wait @nsArgs --for=condition=Ready "pod/$PodName" --timeout=120s

$remotePath = if ($Target) { "/data/results/$Target" } else { "/data/results" }

$exists = kubectl exec @nsArgs $PodName -- sh -c "[ -e '$remotePath' ] && echo yes || echo no"
if ($exists.Trim() -ne "yes") {
    throw "$remotePath 가 PVC에 없습니다. -Target 값을 확인하세요."
}

New-Item -ItemType Directory -Force -Path $LocalDir | Out-Null

Write-Host "다운로드: ${PodName}:${remotePath} -> $LocalDir"
kubectl cp @nsArgs "${PodName}:${remotePath}" $LocalDir

Write-Host "`n완료. 받은 내용:"
Get-ChildItem -Recurse $LocalDir | ForEach-Object { Write-Host "  $($_.FullName)" }
