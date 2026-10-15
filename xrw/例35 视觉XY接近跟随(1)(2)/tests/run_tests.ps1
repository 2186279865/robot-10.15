param([string]$VsRoot='D:\CCCC\visual')
$ErrorActionPreference='Stop'
$emmRoot=Join-Path (Split-Path $PSScriptRoot) 'HARDWARE\EMM'
$hostRoot=Join-Path $PSScriptRoot 'host'
$outRoot=Join-Path $PSScriptRoot 'out'
New-Item -ItemType Directory -Force -Path $outRoot | Out-Null
$setup=Join-Path $VsRoot 'VC\Auxiliary\Build\vcvars64.bat'
if(!(Test-Path -LiteralPath $setup)){throw 'Visual C compiler unavailable'}
$compile='cl /nologo /utf-8 /W4 /DEMM_HOST_TEST /I "'+$hostRoot+
    '" /I "'+$emmRoot+'" "'+$emmRoot+'\motion_sequence.c" "'+
    $emmRoot+'\emm_protocol.c" "'+$hostRoot+'\test_z_position_grab.c" /Fe:sequence_tests.exe'
Push-Location $outRoot
try {
    & cmd /c ('"'+$setup+'" >nul && '+$compile+' && sequence_tests.exe')
    if($LASTEXITCODE -ne 0){throw 'XYZ sequence tests failed'}
} finally {Pop-Location}
