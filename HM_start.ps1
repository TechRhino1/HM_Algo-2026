$env:JARVIS_CONFIRM_LIVE = "1"
Write-Host "====================================================================="
Write-Host "          Starting HM Algo 2.0 Live Trading Terminal"
Write-Host "====================================================================="
$py312 = "C:\Users\musu9\AppData\Local\Programs\Python\Python312\python.exe"
if (Test-Path $py312) {
    & $py312 "$PSScriptRoot\HM_start.py" $args
} else {
    py -3.12 "$PSScriptRoot\HM_start.py" $args
}
