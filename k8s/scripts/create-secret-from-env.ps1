<#
.SYNOPSIS
  .env의 LLM_* 값으로 k8s Secret(기본 sh-ard-asset-agent-secret)을 만들거나 갱신한다.

.DESCRIPTION
  pod.yaml / column-poc-job.yaml / robustness-job.yaml이 모두
  envFrom.secretRef.name: sh-ard-asset-agent-secret 로 이 값을 주입받는다.
  이 스크립트가 그 Secret을 만든다 - 없으면 Job이 환경변수 없이 떠서
  "필수 환경변수가 없습니다"로 즉시 죽는다.

  .env 전체를 그대로 올리지 않고 LLM_* 화이트리스트만 담는다 — .env에는
  Secret에 넣으면 안 되는 값(예: 토큰)이 같이 있을 수 있어서다. 새 키를 코드가
  읽기 시작하면 아래 $secretKeys에도 같이 넣어야 한다.

.PARAMETER EnvFile
  읽어올 .env 경로.

.PARAMETER SecretName
  생성/갱신할 Secret 이름.

.PARAMETER Namespace
  대상 네임스페이스. 생략하면 환경변수 K8S_NAMESPACE, 그다음 .env의
  K8S_NAMESPACE를 차례로 본다. 아무 것도 없으면 kubectl 현재 컨텍스트의
  기본 네임스페이스를 쓴다. 지정된 네임스페이스가 없으면 먼저 만든다.

.EXAMPLE
  ./k8s/scripts/create-secret-from-env.ps1
  ./k8s/scripts/create-secret-from-env.ps1 -EnvFile .env.prod -SecretName sh-ard-asset-agent-secret
#>
param(
    [string]$EnvFile = ".env",
    [string]$SecretName = "sh-ard-asset-agent-secret",
    [string]$Namespace = ""
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot/_common.ps1"

# 기본값(".env")은 현재 폴더 기준이라 레포 루트가 아닌 곳에서 부르면 못 찾는다.
# -EnvFile을 명시했을 때는 그 경로를 그대로 존중한다.
if ($EnvFile -eq ".env" -and -not (Test-Path $EnvFile)) {
    $EnvFile = Join-Path (Get-RepoRoot) ".env"
}
if (-not (Test-Path $EnvFile)) {
    throw "$EnvFile 가 없습니다. LLM_API_ENDPOINT / LLM_API_KEY / LLM_MODEL 등을 담은 .env를 먼저 준비하세요 (.env.example 참고)."
}

$envVars = Read-DotEnv $EnvFile
$Namespace = Get-K8sNamespace -EnvVars $envVars -Namespace $Namespace
$nsArgs = Get-K8sNamespaceArgs -Namespace $Namespace
Show-K8sTarget -Namespace $Namespace

# Secret에 올릴 키 화이트리스트. 새 키를 추가할 땐 여기와 k8s/*.yaml의
# envFrom 사용처를 같이 확인할 것.
$secretKeys = @(
    "LLM_API_ENDPOINT",
    "LLM_API_KEY",
    # 쉼표로 여러 개를 적으면 배치가 모델마다 한 번씩 돈다 (예: modelA,modelB).
    "LLM_MODEL",
    "LLM_REQUESTS_PER_MINUTE",
    "LLM_MAX_CONCURRENCY",
    # 호출 하나가 얼마나 버티는가. 여기 있는 이유는 환경(개발/폐쇄망)마다 다른
    # 값을 쓰는데, yaml에 박으면 환경을 옮길 때마다 커밋이 생기기 때문이다.
    # 실제로 무엇으로 돌았는지는 결과 문서의 meta.llm_call과 run.log의 [CONFIG]
    # 줄에 남으므로, 값이 레포에 없어도 실행 기록에서 되짚을 수 있다.
    "LLM_TIMEOUT_SECONDS",
    "LLM_MAX_RETRIES",
    "LLM_RETRY_BACKOFF_SECONDS",
    "LLM_HTTP_RETRIES",
    # 강건성 테스트 반복 횟수(column-poc-job의 셸이 읽는다). 없으면 1이다.
    # LLM_ 접두사가 아닌 유일한 키라, 이 목록은 접두사 규칙이 아니라 명시 목록이다.
    "ITERATIONS"
)

$literalArgs = @()
foreach ($key in $secretKeys) {
    if ($envVars.ContainsKey($key) -and $envVars[$key]) {
        $literalArgs += "--from-literal=$key=$($envVars[$key])"
    }
}

if ($literalArgs.Count -eq 0) {
    throw "$EnvFile 에서 LLM_* 값을 찾지 못했습니다."
}

if ($Namespace) {
    Write-Host "네임스페이스 '$Namespace' 확인/생성..."
    Confirm-K8sNamespace -Namespace $Namespace
}

Write-Host "Secret '$SecretName' 생성/갱신 ($($literalArgs.Count)개 키, namespace=$(if ($Namespace) { $Namespace } else { '<default>' }))..."
# create --dry-run + apply 조합으로 이미 있으면 갱신, 없으면 새로 만든다(멱등).
kubectl create secret generic $SecretName @literalArgs @nsArgs --dry-run=client -o yaml | kubectl apply @nsArgs -f -

Write-Host "완료. 키 목록만 확인하려면:"
Write-Host "  kubectl get secret $SecretName $($nsArgs -join ' ') -o jsonpath='{.data}' | ConvertFrom-Json | Get-Member -MemberType NoteProperty"
