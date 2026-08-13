<#
.SYNOPSIS
  .env의 LLM_* 값으로 k8s Secret(기본 sh-ard-asset-agent-secret)을 만들거나 갱신한다.

.DESCRIPTION
  pod.yaml / robustness-job.yaml / asset-run-job.yaml이 모두
  envFrom.secretRef.name: sh-ard-asset-agent-secret 로 이 값을 주입받는데,
  이 Secret을 실제로 만드는 스크립트가 레포에 없었다(기존 레포에만 있었고
  마이그레이션되지 않음). 이 스크립트가 그 자리를 채운다.

  .env 전체를 그대로 올리지 않고 LLM_* 화이트리스트만 담는다 — .env에는
  Secret에 넣으면 안 되는 값(예: GH_PAT_TOKEN)이 같이 있을 수 있어서다.

.PARAMETER EnvFile
  읽어올 .env 경로.

.PARAMETER SecretName
  생성/갱신할 Secret 이름.

.EXAMPLE
  ./k8s/scripts/create-secret-from-env.ps1
  ./k8s/scripts/create-secret-from-env.ps1 -EnvFile .env.prod -SecretName sh-ard-asset-agent-secret
#>
param(
    [string]$EnvFile = ".env",
    [string]$SecretName = "sh-ard-asset-agent-secret"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $EnvFile)) {
    throw "$EnvFile 가 없습니다. LLM_API_ENDPOINT / LLM_API_KEY / LLM_MODEL 등을 담은 .env를 먼저 준비하세요 (.env.example 참고)."
}

# .env 파싱 규칙은 src/agent/config.py의 load_dotenv_file()과 동일하게 맞춘다:
# 인라인 주석(" #" 이후) 제거, 앞뒤 따옴표 제거.
$envVars = @{}
Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) { return }
    $parts = $line.Split("=", 2)
    $key = $parts[0].Trim()
    $value = ($parts[1] -split ' #')[0].Trim()
    $value = $value.Trim([char[]]@("'", '"'))
    $envVars[$key] = $value
}

# Secret에 올릴 키 화이트리스트. 새 키를 추가할 땐 여기와 k8s/*.yaml의
# envFrom 사용처를 같이 확인할 것.
$secretKeys = @(
    "LLM_API_ENDPOINT",
    "LLM_API_KEY",
    "LLM_MODEL",
    "LLM_MODEL1",
    "LLM_MODEL2",
    "LLM_MODEL3",
    "LLM_STRUCTURED_MODE",
    "LLM_GUIDED_BACKEND",
    "LLM_REQUESTS_PER_MINUTE",
    "LLM_MAX_CONCURRENCY",
    "LLM_MAX_HTTP_RETRIES"
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

Write-Host "Secret '$SecretName' 생성/갱신 ($($literalArgs.Count)개 키)..."
# create --dry-run + apply 조합으로 이미 있으면 갱신, 없으면 새로 만든다(멱등).
kubectl create secret generic $SecretName @literalArgs --dry-run=client -o yaml | kubectl apply -f -

Write-Host "완료. 키 목록만 확인하려면:"
Write-Host "  kubectl get secret $SecretName -o jsonpath='{.data}' | ConvertFrom-Json | Get-Member -MemberType NoteProperty"
