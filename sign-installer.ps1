# MeetingScribe installer signing script.
# Signs the newest MeetingScribe-Setup-*.exe (or the -Exe path given)
# with the local code-signing cert "CN=MeetingScribe Local Dev".
# Cert backup: assets\MeetingScribe-Local.cer / .pfx (pfx password: MeetingScribe2026)
param(
    [string]$Exe = ''
)
$ErrorActionPreference = 'Stop'
if ($Exe -eq '') {
    $Exe = Get-ChildItem 'D:\Workspace\Meeting\installer-output\MeetingScribe-Setup-*.exe' |
           Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
}
$cert = Get-ChildItem Cert:\CurrentUser\My | Where-Object { $_.Subject -eq 'CN=MeetingScribe Local Dev' } | Select-Object -First 1
if (-not $cert) { Write-Output 'NO_CERT: create the certificate first'; exit 1 }
$sig = Set-AuthenticodeSignature -FilePath $Exe -Certificate $cert -HashAlgorithm SHA256
Write-Output "SIGNED $Exe STATUS=$($sig.Status)"
