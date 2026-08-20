<#
.SYNOPSIS
  로컬 코드(run.py / src / prompts / skills)를 PVC(/data/column_semantic_poc)로 업로드한다.

.DESCRIPTION
  평소에는 이 스크립트가 필요 없다. k8s/column-poc-job.yaml은 기본적으로
  이미지 안의 /app 코드를 쓰고, 이미지는 push할 때마다 CI가 자동으로 굽는다.

  이 스크립트는 **이미지를 다시 굽지 않고** 코드를 바꿔 돌려보고 싶을 때만 쓴다
  (폐쇄망에서 빌드 파이프라인을 기다리기 어려운 경우). Job은 PVC에 run.py가
  있으면 이미지 대신 그쪽을 쓰므로, 실험이 끝나면 반드시 정리해야 한다:

      kubectl exec sh-ard-asset-agent -- rm -rf /data/column_semantic_poc

  정리하지 않으면 PVC에 남은 낡은 코드가 이후 모든 Job에서 조용히 계속 쓰인다.

  PVC는 클러스터 내부 Pod에서만 마운트되므로, 로컬 kubectl로는 직접 쓸 수
  없다. k8s/pod.yaml의 디버그 Pod(sleep infinity)가 없으면 띄우고, 그 Pod를
  경유해 kubectl cp로 넣는다.

.PARAMETER LocalDir
  업로드할 로컬 레포 루트. 기본은 현재 디렉터리.

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
    [string]$LocalDir = ".",

    [string]$PodYaml = "k8s/pod.yaml",

    [string]$PodName = "sh-ard-asset-agent",

    [string]$Namespace = ""
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot/_common.ps1"

$envVars = Read-DotEnv ".env"
$Namespace = Get-K8sNamespace -EnvVars $envVars -Namespace $Namespace
$nsArgs = Get-K8sNamespaceArgs -Namespace $Namespace

# run.py(진입점) / src(구현) / prompts(고정 단계) / skills(보완)가 한 세트다. 넷 중 하나만 낡아도
# 원인을 알기 어려운 실패가 나므로 항상 셋을 같이 올린다.
$required = @("run.py", "src", "prompts", "skills")
foreach ($item in $required) {
    if (-not (Test-Path (Join-Path $LocalDir $item))) {
        throw "$LocalDir/$item 가 없습니다. 레포 루트에서 실행했는지 확인하세요(-LocalDir로 지정 가능)."
    }
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

foreach ($item in $required) {
    $source = Join-Path $LocalDir $item
    Write-Host "$source -> ${PodName}:${remoteDir}/$item"
    kubectl cp @nsArgs $source "${PodName}:${remoteDir}/$item"
}

Write-Host "`n업로드 완료. 확인:"
kubectl exec @nsArgs $PodName -- ls -la $remoteDir
foreach ($item in $required) {
    kubectl exec @nsArgs $PodName -- test -e "$remoteDir/$item"
    if ($LASTEXITCODE -ne 0) {
        throw "$remoteDir/$item 확인 실패 - 업로드가 제대로 되지 않았습니다."
    }
}
Write-Host "run.py / src / prompts / skills 확인됨. kubectl apply -f k8s/column-poc-job.yaml 로 배치를 실행하면"
Write-Host "이미지 코드 대신 이 PVC 코드가 쓰인다. 실험이 끝나면 다음으로 되돌릴 것:"
Write-Host "  kubectl exec $PodName -- rm -rf $remoteDir"
