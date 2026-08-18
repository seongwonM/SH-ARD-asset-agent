<#
.SYNOPSIS
  PVC(/data/robustness_test_results 아래)의 column-poc-batch 결과를 로컬로 내려받는다.
  upload-column-poc.ps1의 반대 방향.

.DESCRIPTION
  디버그 Pod(k8s/pod.yaml)가 없으면 띄우고, 그 Pod를 경유해 kubectl cp로
  /data/robustness_test_results 전체 또는 -Target으로 지정한 파일 하나만
  로컬로 복사한다.

  column-poc-job.yaml(Job: column-poc-batch) 결과: <csv_stem>.semantic.json
  여러 개. 실패한 CSV가 있으면 <csv_stem>.error.log도 같은 디렉터리에
  남아있으니(성공하면 자동 삭제됨) 같이 받힌다.

.PARAMETER LocalDir
  결과를 받을 로컬 디렉터리. 없으면 생성한다.

.PARAMETER Target
  PVC 안 /data/robustness_test_results 아래에서 받아올 파일/경로. 생략하면
  전체를 받는다.

.PARAMETER PodName
  경유할 디버그 Pod 이름. k8s/pod.yaml의 metadata.name과 맞춘다.

.PARAMETER Namespace
  대상 네임스페이스. 생략하면 환경변수 K8S_NAMESPACE, 그다음 .env의
  K8S_NAMESPACE를 차례로 본다. 아무 것도 없으면 kubectl 현재 컨텍스트의
  기본 네임스페이스를 쓴다.

.EXAMPLE
  ./k8s/scripts/download-column-poc-results.ps1 -LocalDir .\results
  ./k8s/scripts/download-column-poc-results.ps1 -LocalDir .\results -Target my_asset.semantic.json
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

$remotePath = if ($Target) { "/data/robustness_test_results/$Target" } else { "/data/robustness_test_results" }

$exists = kubectl exec @nsArgs $PodName -- sh -c "[ -e '$remotePath' ] && echo yes || echo no"
if ($exists.Trim() -ne "yes") {
    throw "$remotePath 가 PVC에 없습니다. -Target 값을 확인하거나 Job이 아직 안 끝났는지 확인하세요."
}

New-Item -ItemType Directory -Force -Path $LocalDir | Out-Null

Write-Host "다운로드: ${PodName}:${remotePath} -> $LocalDir"
kubectl cp @nsArgs "${PodName}:${remotePath}" $LocalDir

Write-Host "`n완료. 받은 내용:"
Get-ChildItem -Recurse $LocalDir | ForEach-Object { Write-Host "  $($_.FullName)" }

$errorLogs = Get-ChildItem -Recurse $LocalDir -Filter "*.error.log" -ErrorAction SilentlyContinue
if ($errorLogs) {
    Write-Host "`n실패한 CSV가 있습니다 (.error.log 참고):"
    $errorLogs | ForEach-Object { Write-Host "  $($_.FullName)" }
}

$partials = Get-ChildItem -Recurse $LocalDir -Filter "*.partial.json" -ErrorAction SilentlyContinue
if ($partials) {
    Write-Host "`n아직 진행 중이거나 중간에 실패한 CSV의 부분 결과(.partial.json)가 있습니다:"
    $partials | ForEach-Object { Write-Host "  $($_.FullName)" }
}
