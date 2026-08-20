<#
.SYNOPSIS
  PVC(/data/robustness_test_results 아래)의 column-poc-batch 결과를 로컬로 내려받는다.
  upload-column-poc.ps1의 반대 방향.

.DESCRIPTION
  디버그 Pod(k8s/pod.yaml)가 없으면 띄우고, 그 Pod를 경유해 kubectl cp로
  /data/robustness_test_results 전체 또는 -Target으로 지정한 파일 하나만
  로컬로 복사한다.

  column-poc-job.yaml(Job: column-poc-batch) 결과: Job이 시작될 때 KST 타임스탬프를
  한 번 찍어 <실행타임스탬프>_<모델명>/ 폴더를 만들고, 그 실행이 처리한 CSV들이
  전부 그 아래 <csv_stem>/ 하위폴더로 들어간다(CSV마다 타임스탬프가 따로 찍히지
  않는다). LLM_MODEL에 모델을 여러 개 적었으면 타임스탬프는 같고 모델명만 다른
  폴더가 모델 수만큼 생긴다 - 같은 실험의 모델별 결과다. 각 <csv_stem>/ 안에는
  결과 문서 5개와 그 실행의 run.log가 같이 있다. 각 <csv_stem>/ 안에는 결과 문서
  5개(result.semantic.columns.json / .rulebase.json / .plan.json / .table.json /
  .llm_calls.json)와 run.log(성공/실패 모두 항상 남음)가 같이 들어있다. 중간에
  죽은 CSV도 파일은 그대로 있고, 완주 여부는 파일 유무가 아니라 meta.status가
  done인지로 구분한다 - 이 스크립트가 받은 뒤에 그걸 확인해서 알려준다.
  (이 구조 이전 버전들 - result.semantic.json 하나였던 것, CSV마다 타임스탬프
  폴더였던 것, 그 이전의 평평한 파일들 - 은 그대로 남아있으니 같이 받힌다.)

.PARAMETER LocalDir
  결과를 받을 로컬 디렉터리. 없으면 생성한다.

.PARAMETER Target
  PVC 안 /data/robustness_test_results 아래에서 받아올 파일 또는 폴더 경로
  (예: 20260818_140500_<모델명> - 그 모델의 실행 전체, 또는 .../my_asset - CSV 하나만).
  생략하면 전체를 받는다.

.PARAMETER PodName
  경유할 디버그 Pod 이름. k8s/pod.yaml의 metadata.name과 맞춘다.

.PARAMETER Namespace
  대상 네임스페이스. 생략하면 환경변수 K8S_NAMESPACE, 그다음 .env의
  K8S_NAMESPACE를 차례로 본다. 아무 것도 없으면 kubectl 현재 컨텍스트의
  기본 네임스페이스를 쓴다.

.EXAMPLE
  ./k8s/scripts/download-column-poc-results.ps1 -LocalDir .\results
  ./k8s/scripts/download-column-poc-results.ps1 -LocalDir .\results -Target 20260818_140500_Qwen-Qwen2.5-32B-Instruct
  ./k8s/scripts/download-column-poc-results.ps1 -LocalDir .\results -Target 20260818_140500_Qwen-Qwen2.5-32B-Instruct/my_asset
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

# 완주하지 못한 실행 찾기. 파일은 어차피 남아 있으므로 파일명이 아니라
# meta.status를 봐야 한다(in_progress = 그 CSV는 중간에 죽은 것).
$unfinished = Get-ChildItem -Recurse $LocalDir -Filter "*.columns.json" -ErrorAction SilentlyContinue |
    Where-Object {
        try { (Get-Content $_.FullName -Raw | ConvertFrom-Json).meta.status -ne "done" }
        catch { $true }  # 읽다 만 JSON도 미완주로 본다
    }
if ($unfinished) {
    Write-Host "`n아직 진행 중이거나 중간에 죽은 CSV가 있습니다(meta.status != done):"
    $unfinished | ForEach-Object { Write-Host "  $($_.FullName)" }
}

# 예전 형식(단일 result.semantic.json)의 부분 결과도 같이 알려준다.
$partials = Get-ChildItem -Recurse $LocalDir -Filter "*.partial.json" -ErrorAction SilentlyContinue
if ($partials) {
    Write-Host "`n예전 형식의 부분 결과(.partial.json)도 있습니다:"
    $partials | ForEach-Object { Write-Host "  $($_.FullName)" }
}
