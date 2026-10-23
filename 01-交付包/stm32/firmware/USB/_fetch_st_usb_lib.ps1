$ErrorActionPreference = 'Stop'
$repo = 'jys0305/STM32_USB-Host-Device_Lib_V2.2.0'
$ref  = 'master'
$base = 'https://raw.githubusercontent.com/' + $repo + '/' + $ref + '/'
$root = 'D:\study\project\robot-competition\ghr\10.15\USB'
$L    = 'Libraries'
$P    = 'Project/USB_Device_Examples/VCP'

$map = [ordered]@{
  "$L/STM32_USB_OTG_Driver/inc/usb_bsp.h"          = 'STM32_USB_OTG_Driver/inc/usb_bsp.h'
  "$L/STM32_USB_OTG_Driver/inc/usb_core.h"         = 'STM32_USB_OTG_Driver/inc/usb_core.h'
  "$L/STM32_USB_OTG_Driver/inc/usb_dcd.h"          = 'STM32_USB_OTG_Driver/inc/usb_dcd.h'
  "$L/STM32_USB_OTG_Driver/inc/usb_dcd_int.h"      = 'STM32_USB_OTG_Driver/inc/usb_dcd_int.h'
  "$L/STM32_USB_OTG_Driver/inc/usb_defines.h"      = 'STM32_USB_OTG_Driver/inc/usb_defines.h'
  "$L/STM32_USB_OTG_Driver/inc/usb_otg.h"          = 'STM32_USB_OTG_Driver/inc/usb_otg.h'
  "$L/STM32_USB_OTG_Driver/inc/usb_regs.h"         = 'STM32_USB_OTG_Driver/inc/usb_regs.h'
  "$L/STM32_USB_OTG_Driver/src/usb_core.c"         = 'STM32_USB_OTG_Driver/src/usb_core.c'
  "$L/STM32_USB_OTG_Driver/src/usb_dcd.c"          = 'STM32_USB_OTG_Driver/src/usb_dcd.c'
  "$L/STM32_USB_OTG_Driver/src/usb_dcd_int.c"      = 'STM32_USB_OTG_Driver/src/usb_dcd_int.c'
  "$L/STM32_USB_Device_Library/Core/inc/usbd_core.h"   = 'STM32_USB_Device_Library/Core/inc/usbd_core.h'
  "$L/STM32_USB_Device_Library/Core/inc/usbd_def.h"    = 'STM32_USB_Device_Library/Core/inc/usbd_def.h'
  "$L/STM32_USB_Device_Library/Core/inc/usbd_ioreq.h"  = 'STM32_USB_Device_Library/Core/inc/usbd_ioreq.h'
  "$L/STM32_USB_Device_Library/Core/inc/usbd_req.h"    = 'STM32_USB_Device_Library/Core/inc/usbd_req.h'
  "$L/STM32_USB_Device_Library/Core/inc/usbd_usr.h"    = 'STM32_USB_Device_Library/Core/inc/usbd_usr.h'
  "$L/STM32_USB_Device_Library/Core/src/usbd_core.c"   = 'STM32_USB_Device_Library/Core/src/usbd_core.c'
  "$L/STM32_USB_Device_Library/Core/src/usbd_ioreq.c"  = 'STM32_USB_Device_Library/Core/src/usbd_ioreq.c'
  "$L/STM32_USB_Device_Library/Core/src/usbd_req.c"    = 'STM32_USB_Device_Library/Core/src/usbd_req.c'
  "$L/STM32_USB_Device_Library/Class/cdc/inc/usbd_cdc_core.h"  = 'STM32_USB_Device_Library/Class/cdc/inc/usbd_cdc_core.h'
  "$L/STM32_USB_Device_Library/Class/cdc/src/usbd_cdc_core.c"  = 'STM32_USB_Device_Library/Class/cdc/src/usbd_cdc_core.c'
  "$P/src/usb_bsp.c"        = 'App/usb_bsp.c'
  "$P/src/usbd_desc.c"      = 'App/usbd_desc.c'
  "$P/src/usbd_usr.c"       = 'App/usbd_usr.c'
  "$P/src/usbd_cdc_vcp.c"   = 'App/usbd_cdc_vcp.c'
  "$P/inc/usb_conf.h"       = 'App/usb_conf.h'
  "$P/inc/usbd_conf.h"      = 'App/usbd_conf.h'
  "$P/inc/usbd_desc.h"      = 'App/usbd_desc.h'
  "$P/inc/usbd_cdc_vcp.h"   = 'App/usbd_cdc_vcp.h'
}

$ok = 0; $fail = @()
foreach ($src in $map.Keys) {
  $dst = Join-Path $root $map[$src]
  $dir = Split-Path $dst -Parent
  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
  try {
    Invoke-WebRequest -Uri ($base + $src) -OutFile $dst -TimeoutSec 40 -ErrorAction Stop
    $len = (Get-Item $dst).Length
    if ($len -lt 200) { throw "too small: $len bytes" }
    $ok++
    "  ok  {0,7}  {1}" -f $len, $map[$src]
  } catch {
    $fail += "$src -> $($_.Exception.Message)"
    "  FAIL {0}" -f $src
  }
}
"downloaded=$ok failed=$($fail.Count)"
$fail | ForEach-Object { "  !! $_" }

# license pdf
try {
  Invoke-WebRequest -Uri ($base + 'MCD-ST%20Liberty%20SW%20License%20Agreement%20V2.pdf') -OutFile (Join-Path $root 'MCD-ST_Liberty_SW_License_Agreement_V2.pdf') -TimeoutSec 60 -ErrorAction Stop
  "license pdf: {0} bytes" -f (Get-Item (Join-Path $root 'MCD-ST_Liberty_SW_License_Agreement_V2.pdf')).Length
} catch { "license pdf FAILED: $($_.Exception.Message)" }
