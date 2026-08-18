<#
.SYNOPSIS
  column_semantic_poc 코드를 PVC(/data/column_semantic_poc)로 업로드한다.

.DESCRIPTION
  column_semantic_poc는 deploy/Dockerfile에 COPY되어 있지 않아 GHCR 이미지
  안에 없다. k8s/column-poc-job.yaml(Job: column-poc-batch)이 이 코드를
  /data/column_semantic_poc에서 찾으므로, Job을 돌리기 전에 최초 1회
  (그리고 로컬에서 run.py/skills를 고칠 때마다) 이 스크립트로 PVC에 올려야 한다.
  지금까지는 이 kubectl cp 단계가 문서(주석)로만 있고 실행이 누락되기 쉬웠다.

  PVC는 클러스터 내부 Pod에서만 마운트되므로, 로컬 kubectl로는 직접 쓸 수
  없다. k8s/pod.yaml의 디버그 Pod(sleep infinity)가 없으면 띄우고, 그 Pod를
  경유해 kubectl cp로 column_semantic_poc/ 디렉터리 전체를 넣는다.

.PARAMETER LocalDir
  업로드할 로컬 디렉터리. 기본은 레포 루트의 column_semantic_poc.

.PARAMETER PodName
  파일을 경유해 넣을 디버그 Pod 이름. k8s/pod.yaml의 metadata.name과 맞춘다.

.PARAMETER Namespace
  대상 네임스페이스. 생략하면 환경변수 K8S_NAMESPACE, 그다음 .env의
  K8S_NAMESPACE를 차례로 본다. 아무 것도 없으면 kubectl 현재 컨텍스트의
  기본 네임스페이스를 쓴다.

.EXAMPLE
  ./k8s/scripts/upload-column-poc.ps1
#>
param(
    [string]$LocalDir = "column_semantic_poc",

    [string]$PodYaml = "k8s/pod.yaml",

    [string]$PodName = "sh-ard-asset-agent",

    [string]$Namespace = ""
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot/_common.ps1"

$envVars = Read-DotEnv ".env"
$Namespace = Get-K8sNamespace -EnvVars $envVars -Namespace $Namespace
$nsArgs = Get-K8sNamespaceArgs -Namespace $Namespace

if (-not (Test-Path $LocalDir)) {
    throw "$LocalDir 가 없습니다. 레포 루트에서 실행했는지 확인하세요."
}
if (-not (Test-Path (Join-Path $LocalDir "run.py"))) {
    throw "$LocalDir/run.py 가 없습니다. -LocalDir로 column_semantic_poc 경로를 지정하세요."
}
if (-not (Test-Path (Join-Path $LocalDir "csv_repair.py"))) {
    throw "$LocalDir/csv_repair.py 가 없습니다. run.py가 import하는 필수 파일입니다."
}

if ($Namespace) {
    Write-Host "네임스페이스 '$Namespace' 확인/생성..."
    Confirm-K8sNamespace -Namespace $Namespace
}

Write-Host "PVC 확인/생성..."
kubectl apply @nsArgs -f k8s/data-pvc.yaml | Out-Null

$podPhase = kubectl get pod $PodName @nsArgs -o jsonpath="{.status.phase}" 2>$null
if (-not $podPhase) {
    Write-Host "디버그 Pod($PodName) 생성..."
    kubectl apply @nsArgs -f $PodYaml | Out-Null
}
Write-Host "Pod Ready 대기..."
kubectl wait @nsArgs --for=condition=Ready "pod/$PodName" --timeout=120s

$remoteDir = "/data/column_semantic_poc"
Write-Host "기존 $remoteDir 정리..."
kubectl exec @nsArgs $PodName -- rm -rf $remoteDir
kubectl exec @nsArgs $PodName -- mkdir -p $remoteDir

Write-Host "$LocalDir -> ${PodName}:${remoteDir}"
kubectl cp @nsArgs $LocalDir/. "${PodName}:${remoteDir}"

Write-Host "`n업로드 완료. 확인:"
kubectl exec @nsArgs $PodName -- ls -la $remoteDir
kubectl exec @nsArgs $PodName -- test -f "$remoteDir/run.py"
if ($LASTEXITCODE -ne 0) {
    throw "$remoteDir/run.py 확인 실패 - 업로드가 제대로 되지 않았습니다."
}
kubectl exec @nsArgs $PodName -- test -f "$remoteDir/csv_repair.py"
if ($LASTEXITCODE -ne 0) {
    throw "$remoteDir/csv_repair.py 확인 실패 - 업로드가 제대로 되지 않았습니다."
}
Write-Host "run.py / csv_repair.py 확인됨. 이제 kubectl apply -f k8s/column-poc-job.yaml 로 배치를 실행할 수 있습니다."
