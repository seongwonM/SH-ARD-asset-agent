<#
.SYNOPSIS
  k8s/scripts/*.ps1이 공유하는 .env 파싱 + 네임스페이스 헬퍼. 직접 실행하지 않고
  다른 스크립트가 ". $PSScriptRoot/_common.ps1"로 dot-source 해서 쓴다.
#>

function Read-DotEnv {
    param([string]$Path = ".env")
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

function Confirm-K8sNamespace {
    <# 네임스페이스가 지정돼 있으면 없을 때 만든다(멱등). #>
    param([string]$Namespace = "")
    if (-not $Namespace) { return }
    kubectl create namespace $Namespace --dry-run=client -o yaml | kubectl apply -f - | Out-Null
}
