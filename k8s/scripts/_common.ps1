<#
.SYNOPSIS
  k8s/scripts/*.ps1이 공유하는 .env 파싱 + 네임스페이스 헬퍼. 직접 실행하지 않고
  다른 스크립트가 ". $PSScriptRoot/_common.ps1"로 dot-source 해서 쓴다.
#>

function Get-RepoRoot {
    <# k8s/scripts/ 기준 두 단계 위가 레포 루트다. #>
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

function Read-DotEnv {
    <#
    경로를 주지 않으면 **레포 루트의 .env**를 본다. 예전에는 상대경로 ".env"라
    레포 루트에서 실행하지 않으면 조용히 빈 해시를 돌려줬고, 그러면 네임스페이스가
    소리 없이 기본값(default)으로 떨어져 "왜 파드를 못 찾지"가 된다.
    #>
    param([string]$Path = "")
    if (-not $Path) { $Path = Join-Path (Get-RepoRoot) ".env" }
    $envVars = @{}
    if (-not (Test-Path $Path)) { return $envVars }
    Get-Content $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) { return }
        $parts = $line.Split("=", 2)
        $key = $parts[0].Trim()
        $value = ($parts[1] -split ' #')[0].Trim()
        $value = $value.Trim([char[]]@("'", '"'))
        $envVars[$key] = $value
    }
    return $envVars
}

function Get-K8sNamespace {
    <#
    우선순위: -Namespace 파라미터 > 실제 환경변수 K8S_NAMESPACE > .env의 K8S_NAMESPACE.
    아무 것도 없으면 빈 문자열(= kubectl 현재 컨텍스트의 기본 네임스페이스 사용).
    #>
    param(
        [hashtable]$EnvVars = @{},
        [string]$Namespace = ""
    )
    if ($Namespace) { return $Namespace }
    if ($env:K8S_NAMESPACE) { return $env:K8S_NAMESPACE }
    if ($EnvVars.ContainsKey("K8S_NAMESPACE") -and $EnvVars["K8S_NAMESPACE"]) { return $EnvVars["K8S_NAMESPACE"] }
    return ""
}

function Get-K8sNamespaceArgs {
    param([string]$Namespace = "")
    if ($Namespace) { return @("-n", $Namespace) }
    return @()
}

function Show-K8sTarget {
    <#
    어느 컨텍스트/네임스페이스로 나가는지 먼저 찍는다. 이게 없으면 K8S_NAMESPACE를
    아무도 안 준 채 default로 나가는 것을 "파드가 없다"는 에러로만 알게 된다 -
    그 에러는 진짜 없는 것과 구분이 안 된다.
    #>
    param([string]$Namespace = "")
    $context = kubectl config current-context
    if ($Namespace) {
        Write-Host "대상: context=$context namespace=$Namespace"
    } else {
        $fromContext = kubectl config view --minify -o jsonpath="{..namespace}"
        if (-not $fromContext) { $fromContext = "default" }
        Write-Host "대상: context=$context namespace=$fromContext (컨텍스트 기본값)"
        Write-Host "  K8S_NAMESPACE가 .env에도 환경변수에도 없습니다. 다른 네임스페이스를"
        Write-Host "  쓴다면 .env에 K8S_NAMESPACE=<이름>을 넣거나 -Namespace로 주세요."
    }
}

function Confirm-K8sNamespace {
    <# 네임스페이스가 지정돼 있으면 없을 때 만든다(멱등). #>
    param([string]$Namespace = "")
    if (-not $Namespace) { return }
    kubectl create namespace $Namespace --dry-run=client -o yaml | kubectl apply -f - | Out-Null
}
