# One-time setup for /reel-fx on Windows. Prints READY, or NEED_FFMPEG / NEED_PYTHON.
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -or -not (Get-Command ffprobe -ErrorAction SilentlyContinue)) { Write-Output 'NEED_FFMPEG'; exit 1 }
$pyExe = $null; $pyArgs = @()
foreach ($v in @('3.13', '3.12', '3.11', '3.10')) {
  if (Get-Command py -ErrorAction SilentlyContinue) { & py "-$v" -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { $pyExe = 'py'; $pyArgs = @("-$v"); break } }
}
if (-not $pyExe -and (Get-Command python -ErrorAction SilentlyContinue)) {
  & python -c "import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)" 2>$null
  if ($LASTEXITCODE -eq 0) { $pyExe = 'python' }
}
if (-not $pyExe) { Write-Output 'NEED_PYTHON (3.10 to 3.13)'; exit 1 }
if (-not (Test-Path '.venv\Scripts\python.exe')) { & $pyExe @pyArgs -m venv .venv }
$vp = '.venv\Scripts\python.exe'
& $vp -m pip install -q --upgrade pip
& $vp -m pip install -q -r requirements.txt
& $vp -m playwright install chromium | Out-Null
$m = 'https://storage.googleapis.com/mediapipe-models'
$files = @{
  'selfie_multiclass_256x256.tflite' = "$m/image_segmenter/selfie_multiclass_256x256/float32/latest/selfie_multiclass_256x256.tflite";
  'blaze_face_short_range.tflite'    = "$m/face_detector/blaze_face_short_range/float16/latest/blaze_face_short_range.tflite"
}
foreach ($f in $files.Keys) { $p = Join-Path 'models' $f; if (-not (Test-Path $p)) { Invoke-WebRequest -Uri $files[$f] -OutFile $p -UseBasicParsing } }
& $vp -c "import mediapipe, numpy, PIL, playwright"; if ($LASTEXITCODE -ne 0) { exit 1 }
& $vp -c "import faster_whisper"; if ($LASTEXITCODE -ne 0) { exit 1 }
Write-Output 'READY'
