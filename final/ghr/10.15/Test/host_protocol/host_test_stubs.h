/* host_test_stubs.h - 主机侧自测用的桩声明
 *
 * 只在 HOST_PROTOCOL_HOSTTEST 下被 control_inline.h 包含。
 * 目的：让 Drive/Include/host_protocol_inline.h 与 control_inline.h 这两份
 * 真实固件源码，能在 PC 上用普通 C 编译器编译并跑起来，逐字节验证协议行为，
 * 而不需要 STM32 的外设头文件，也不需要上板。
 */

#ifndef HOST_TEST_STUBS_H
#define HOST_TEST_STUBS_H

#include <stdint.h>

uint32_t HostTest_Millis(void);
uint8_t  HostTest_EnableAt(uint8_t addr, uint8_t on);
uint8_t  HostTest_ZeroHereAt(uint8_t addr);
uint8_t  HostTest_ResetStallAt(uint8_t addr);
uint8_t  HostTest_MoveRelativeAt2(uint8_t addr, uint8_t dir, int32_t pulses,
                                  uint16_t rpm, uint8_t accel, uint32_t wait_ms);
uint8_t  HostTest_RunSpeedAt2(uint8_t addr, uint8_t dir, uint16_t rpm,
                              uint8_t accel);
uint8_t  HostTest_StopAt(uint8_t addr);
void     HostTest_ServoSetAngle(uint16_t deg);
void     HostTest_ServoInitAt(uint16_t deg);

#define Stepper_Millis()                        HostTest_Millis()
#define Stepper_EnableAt(a, on)                 HostTest_EnableAt((a), (on))
#define Stepper_ZeroHereAt(a)                   HostTest_ZeroHereAt(a)
#define Stepper_ResetStallAt(a)                 HostTest_ResetStallAt(a)
#define Stepper_MoveRelativeAt2(a, d, p, r, c, w) \
        HostTest_MoveRelativeAt2((a), (d), (p), (r), (c), (w))
#define Stepper_RunSpeedAt2(a, d, r, c)         HostTest_RunSpeedAt2((a), (d), (r), (c))
#define Stepper_StopAt(a)                       HostTest_StopAt(a)
#define Servo_SetAngle(deg)                     HostTest_ServoSetAngle(deg)
#define Servo_InitAt(deg)                       HostTest_ServoInitAt(deg)

#endif /* HOST_TEST_STUBS_H */
