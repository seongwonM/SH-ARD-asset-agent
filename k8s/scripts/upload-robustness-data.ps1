<#
.SYNOPSIS
  로컬 데이터 폴더(CSV + metadata)를 PVC(/data/robustness_test)로 업로드한다.

.DESCRIPTION
  PVC는 클러스터 내부 Pod에서만 마운트되므로, 로컬 kubectl로는 직접 쓸 수
  없다. k8s/pod.yaml의 디버그 Pod(sleep infinity)가 없으면 띄우고, 그 Pod를
  경유해 kubectl cp로 파일을 넣는다.

  **기존 내용을 지우고 새로 넣는다.** 배치는 대상 폴더의 *.csv를 전부 도는데,
  덧씌우기만 하면 예전 실험에서 올린 CSV가 남아 조용히 같이 돌아간다 - 결과
  폴더에 낯선 이름이 하나 더 있는 것으로만 드러나서 알아채기 어렵다. 지우지
  않고 얹으려면 -KeepExisting을 준다.

  결과 폴더(/data/robustness_test_results)는 다른 경로라 건드리지 않는다.

  이 경로를 읽는 곳:
    k8s/column-poc-job.yaml   DATA_DIR=/data/robustness_test  (메인 실험 경로)
    k8s/robustness-job.yaml   --data-dir

.PARAMETER LocalDir
  업로드할 로컬 디렉터리. <asset>.csv + <asset>_metadata.json(선택) 파일들.
  기본값이 없으면 하이픈/언더스코어 표기를 둘 다 찾아본다.

.PARAMETER Target
  PVC 안의 대상 하위 디렉터리 이름. 기본 "robustness_test"가 두 Job이 공유하는
  위치다. 별도 디렉터리를 쓰고 싶으면 이 값과 해당 yaml의 경로를 같이 바꾼다.

.PARAMETER KeepExisting
  PVC에 이미 있는 파일을 지우지 않고 같은 이름만 덮어쓴다.

.PARAMETER PodName
  파일을 경유해 넣을 디버그 Pod 이름. k8s/pod.yaml의 metadata.name과 맞춘다.

.PARAMETER Namespace
  대상 네임스페이스. 생략하면 환경변수 K8S_NAMESPACE, 그다음 .env의
  K8S_NAMESPACE를 차례로 본다. 아무 것도 없으면 kubectl 현재 컨텍스트의
  기본 네임스페이스를 쓴다.

.EXAMPLE
  ./k8s/scripts/upload-robustness-data.ps1

.EXAMPLE
  ./k8s/scripts/upload-robustness-data.ps1 -LocalDir .\data\robustness-test -KeepExisting
#>
param(
    [string]$LocalDir = "",

    [string]$Target = "robustness_test",

    [switch]$KeepExisting,

    [string]$PodYaml = "k8s/pod.yaml",

    [string]$PodName = "sh-ard-asset-agent",

    [string]$Namespace = ""
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot/_common.ps1"

$envVars = Read-DotEnv
$Namespace = Get-K8sNamespace -EnvVars $envVars -Namespace $Namespace
$nsArgs = Get-K8sNamespaceArgs -Namespace $Namespace
Show-K8sTarget -Namespace $Namespace

# rm -rf에 들어갈 값이라 이름 모양을 먼저 막는다. 빈 값이나 "..", "/"가 섞이면
# 지우려던 것보다 위를 지운다.
if ($Target -notmatch '^[A-Za-z0-9._-]+$') {
    throw "Target '$Target' 은 폴더 이름 하나여야 합니다(영문/숫자/._- 만)."
}

if (-not $LocalDir) {
    # 폴더 이름을 하이픈으로 쓰기도 하고 언더스코어로 쓰기도 한다. 없는 쪽을
    # 기본값으로 잡아 "그런 폴더 없습니다"로 끝나지 않게 둘 다 본다.
    $candidates = @("data/robustness-test", "data/robustness_test")
    $LocalDir = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $LocalDir) {
        throw "업로드할 폴더를 찾지 못했습니다($($candidates -join ', ')). -LocalDir로 지정하세요."
    }
}
if (-not (Test-Path $LocalDir)) {
    throw "$LocalDir 가 없습니다."
}

$csvFiles = @(Get-ChildItem -Path $LocalDir -Filter "*.csv" -File)
$metaFiles = @(Get-ChildItem -Path $LocalDir -Filter "*_metadata.json" -File)
$files = $csvFiles + $metaFiles

if ($csvFiles.Count -eq 0) {
    throw "$LocalDir 에 *.csv 가 없습니다. 배치는 이 폴더의 CSV를 순회합니다."
}

# 올리지 않는 파일이 있으면 말해준다. 조용히 빼면 "왜 저 파일은 안 올라갔지"를
# 클러스터에서 찾게 된다. (FileInfo는 값 비교가 안 되므로 이름으로 맞춘다)
$uploadNames = @($files.Name)
$skipped = @(Get-ChildItem -Path $LocalDir -File | Where-Object { $uploadNames -notcontains $_.Name })
if ($skipped.Count -gt 0) {
    Write-Host "건너뛴 파일 $($skipped.Count)개 (csv / *_metadata.json 만 올린다): $($skipped.Name -join ', ')"
}

# metadata는 <csv이름>_metadata.json 규칙이다. 짝이 안 맞는 것은 이름을 잘못
# 지었을 가능성이 높으니 올리기 전에 보여준다.
$metaNames = @($metaFiles.Name)
foreach ($csv in $csvFiles) {
    $expected = "$($csv.BaseName)_metadata.json"
    if ($metaNames -notcontains $expected) {
        Write-Host "  (metadata 없음: $($csv.Name) -> $expected)"
    }
}

Write-Host "업로드 대상: $LocalDir (CSV $($csvFiles.Count)개, metadata $($metaFiles.Count)개)"

if ($Namespace) {
    Write-Host "네임스페이스 '$Namespace' 확인/생성..."
    Confirm-K8sNamespace -Namespace $Namespace
}

Write-Host "PVC 확인/생성..."
kubectl apply @nsArgs -f k8s/data-pvc.yaml | Out-Null

$podPhase = kubectl get pod $PodName @nsArgs --ignore-not-found -o jsonpath="{.status.phase}"
if (-not $podPhase) {
    Write-Host "디버그 Pod($PodName) 생성..."
    kubectl apply @nsArgs -f $PodYaml | Out-Null
}
Write-Host "Pod Ready 대기..."
kubectl wait @nsArgs --for=condition=Ready "pod/$PodName" --timeout=120s
if ($LASTEXITCODE -ne 0) {
    # 여기서 끊지 않으면 없는 파드에 대고 cp/exec를 계속 시도하다가
    # 서로 다른 에러가 줄줄이 나서 진짜 원인(네임스페이스/VPN)이 묻힌다.
    throw "Pod($PodName)가 Ready가 되지 않았습니다. 위 [대상] 줄의 namespace가 맞는지, 클러스터에 연결돼 있는지 확인하세요."
}

$remoteDir = "/data/$Target"

Write-Host "`n현재 $remoteDir 내용:"
kubectl exec @nsArgs $PodName -- sh -c "ls -1 '$remoteDir' 2>/dev/null || echo '(없음)'"

if ($KeepExisting) {
    Write-Host "-KeepExisting: 기존 파일을 지우지 않고 같은 이름만 덮어씁니다."
    kubectl exec @nsArgs $PodName -- mkdir -p $remoteDir
} else {
    Write-Host "기존 $remoteDir 삭제 후 새로 생성..."
    kubectl exec @nsArgs $PodName -- rm -rf $remoteDir
    kubectl exec @nsArgs $PodName -- mkdir -p $remoteDir
}

$total = $files.Count
$i = 0
# kubectl cp는 인자에 ':'가 있으면 원격 경로(<pod>:<path>)로 읽는다. Windows
# 절대경로 C:\... 를 그대로 넘기면 드라이브 문자 C를 파드 이름으로 보고, 양쪽 다
# 원격이라고 판단해 "one of src or dest must be a local file specification"으로
# 죽는다. 그래서 파일이 있는 폴더로 옮겨 **파일 이름만** 넘긴다 - 상대경로에는
# ':'가 없다. (kubectl 자체의 오래된 문제라 옵션으로는 못 피한다.)
Push-Location $LocalDir
try {
    foreach ($f in $files) {
        $i++
        Write-Host "[$i/$total] $($f.Name) -> ${PodName}:${remoteDir}/$($f.Name)"
        kubectl cp @nsArgs $f.Name "${PodName}:${remoteDir}/$($f.Name)"
        if ($LASTEXITCODE -ne 0) {
            throw "$($f.Name) 업로드 실패. PVC가 가득 찼거나 Pod가 죽었는지 확인하세요."
        }
    }
}
finally {
    Pop-Location
}

Write-Host "`n업로드 완료 ($total 개). 확인:"
kubectl exec @nsArgs $PodName -- ls -la $remoteDir

# 배치가 도는 것은 원격의 CSV 개수다. 여기서 세어보지 않으면 몇 개가 조용히
# 빠졌을 때 결과 폴더 수가 모자란 것으로만 드러난다.
$remoteCsvRaw = kubectl exec @nsArgs $PodName -- sh -c "ls -1 '$remoteDir'/*.csv 2>/dev/null | wc -l"
$remoteCsv = ($remoteCsvRaw | Select-Object -First 1).ToString().Trim()
if ([int]$remoteCsv -ne $csvFiles.Count) {
    throw "원격 CSV가 $remoteCsv 개인데 로컬은 $($csvFiles.Count) 개입니다. 업로드가 온전하지 않습니다."
}
Write-Host "원격 CSV $remoteCsv 개 확인됨."
Write-Host "이제 배치를 돌린다: kubectl apply -f k8s/column-poc-job.yaml"
