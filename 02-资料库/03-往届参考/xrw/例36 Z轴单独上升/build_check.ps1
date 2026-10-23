param(
    [string]$CompilerRoot = 'D:\ARM\ARMCC507\bin',
    [string]$Project = 'USER\PWM.uvprojx'
)
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
$projectPath=Join-Path $root $Project
[xml]$xml=Get-Content -Raw -LiteralPath $projectPath
$userDir=Split-Path $projectPath
$target=$xml.Project.Targets.Target
$includes=@($target.TargetOption.TargetArmAds.Cads.VariousControls.IncludePath.Split(';') |
    ForEach-Object {'-I'+[IO.Path]::GetFullPath((Join-Path $userDir $_))})
$objDir=Join-Path $root 'OBJ'
New-Item -ItemType Directory -Force -Path $objDir | Out-Null
$objects=@()
foreach($file in $target.Groups.Group.Files.File) {
    $src=[IO.Path]::GetFullPath((Join-Path $userDir $file.FilePath))
    $obj=Join-Path $objDir ([IO.Path]::GetFileNameWithoutExtension($src)+'.o')
    if($file.FileName -like '*.c') {
        & (Join-Path $CompilerRoot 'armcc.exe') --cpu Cortex-M4.fp -g -O0 `
            -DSTM32F40_41xxx -DUSE_STDPERIPH_DRIVER @includes -c $src -o $obj
    } elseif($file.FileName -like '*.s') {
        & (Join-Path $CompilerRoot 'armasm.exe') --cpu Cortex-M4.fp $src -o $obj
    } else { continue }
    if($LASTEXITCODE -ne 0) { throw "Compile failed: $src" }
    $objects += $obj
}
$name=$target.TargetOption.TargetCommonOption.OutputName
$axf=Join-Path $objDir "$name.axf"
& (Join-Path $CompilerRoot 'armlink.exe') @objects --ro_base=0x08000000 --rw_base=0x20000000 `
    --entry=Reset_Handler '--first=startup_stm32f40_41xxx.o(RESET)' --map `
    --list (Join-Path $objDir "$name.map") -o $axf
if($LASTEXITCODE -ne 0) { throw 'Link failed' }
& (Join-Path $CompilerRoot 'fromelf.exe') --i32combined --output `
    (Join-Path $objDir "$name.hex") $axf
if($LASTEXITCODE -ne 0) { throw 'HEX generation failed' }
Write-Output "BUILD_OK: $name"
