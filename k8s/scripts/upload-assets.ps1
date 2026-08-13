<#
.SYNOPSIS
  로컬 CSV(+ metadata.json) 자산을 PVC(/data/<Target>)로 업로드한다.

.DESCRIPTION
  PVC는 클러스터 내부 Pod에서만 마운트되므로, 로컬 kubectl로는 직접 쓸 수
  없다. k8s/pod.yaml의 디버그 Pod(sleep infinity)가 없으면 띄우고, 그 Pod를
  경유해 kubectl cp로 파일을 넣는다.

  asset-run-job.yaml과 robustness-job.yaml 둘 다 <asset>.csv +
  <asset>_metadata.json(선택) 규칙을 /data/robustness_test에서 읽는다
  (asset-run-job.yaml의 ASSET_DATA_DIR, robustness-job.yaml의 --data-dir).

.PARAMETER LocalDir
  업로드할 로컬 디렉터리. <asset>.csv + <asset>_metadata.json(선택) 파일들.

.PARAMETER Target
  PVC 안의 대상 하위 디렉터리 이름. 기본 "robustness_test"가 두 Job이 공유하는
  위치다. 별도 디렉터리를 쓰고 싶으면 이 값과 해당 yaml의 경로를 같이 바꾼다.

.PARAMETER PodName
  파일을 경유해 넣을 디버그 Pod 이름. k8s/pod.yaml의 metadata.name과 맞춘다.

.PARAMETER Namespace
  대상 네임스페이스. 생략하면 환경변수 K8S_NAMESPACE, 그다음 .env의
  K8S_NAMESPACE를 차례로 본다. 아무 것도 없으면 kubectl 현재 컨텍스트의
  기본 네임스페이스를 쓴다.

.EXAMPLE
  ./k8s/scripts/upload-assets.ps1 -LocalDir .\data\robustness_test
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$LocalDir,

    [string]$Target = "robustness_test",

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
    throw "$LocalDir 가 없습니다."
}

$files = @(Get-ChildItem -Path $LocalDir -Filter "*.csv") + @(Get-ChildItem -Path $LocalDir -Filter "*_metadata.json") + @(Get-ChildItem -Path $LocalDir -Filter "*_truth.json")
if ($files.Count -eq 0) {
    throw "$LocalDir 에 csv/metadata.json/truth.json 파일이 없습니다."
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

$remoteDir = "/data/$Target"
kubectl exec @nsArgs $PodName -- mkdir -p $remoteDir

$total = $files.Count
$i = 0
foreach ($f in $files) {
    $i++
    Write-Host "[$i/$total] $($f.Name) -> ${PodName}:${remoteDir}/$($f.Name)"
    kubectl cp @nsArgs $f.FullName "${PodName}:${remoteDir}/$($f.Name)"
}

Write-Host "`n업로드 완료 ($total 개). 확인:"
kubectl exec @nsArgs $PodName -- ls -la $remoteDir
