#ifndef __STEPPER_H
#define __STEPPER_H

#include "stm32f4xx.h"

/*
 * EMM V5 步进闭环驱动模块（协议：EMM_V5_串口协议与工程指令总结.md）
 *
 * 帧格式：  [地址] [功能码] [参数…] [0x6B]，多字节参数大端
 * 通信：    USART2 / PA2(TX) / 115200 8N1，只发不收（与例35、例36 一致）
 * 时基：    独立 TIM2 1ms 计数（不占用 delay.c 的 SysTick，也不产生 STEP 脉冲）
 *
 * 多电机：所有驱动器挂在同一条 PA2 总线上，靠【地址】区分。
 *         单机操作用 Stepper_*At(addr, ...)；
 *         一键驱动全部电机用 Stepper_MoveAll / Stepper_EnableAll / Stepper_StopAll。
 *
 * 用法：
 *     Stepper_Init();
 *     Stepper_StopAll();                       // 全部先停车
 *     Stepper_EnableAll(1U);                   // 全部使能
 *     Stepper_MoveAll(STEPPER_DIR_POSITIVE, STEPPER_TURNS_TO_PULSES(1U)); // 全部走 1 圈
 */

/* ---- 电机地址（与例35/例36 的分配一致）---- */
#define STEPPER_ADDR_Z     1U
#define STEPPER_ADDR_TRAY  3U
#define STEPPER_ADDR_X     4U
#define STEPPER_ADDR_Y     5U

/* 当前工程实际挂了多少台：按这几个地址逐个发送 */
#define STEPPER_MOTOR_1    1U
#define STEPPER_MOTOR_2    2U
#define STEPPER_MOTOR_3    3U
#define STEPPER_MOTOR_4    4U
#define STEPPER_MOTOR_COUNT 4U

/* ---- 运动参数 ---- */
#define STEPPER_SPEED_RPM   300U    /* 位置模式速度（RPM） */
#define STEPPER_ACCEL       128U    /* 0 = 直接启动；1~255 = 加减速档位 */
#define STEPPER_PULSES_PER_REV 3200UL /* 1.8° 电机 16 细分；32 细分改 6400 */
#define STEPPER_TURNS_TO_PULSES(t) ((int32_t)(STEPPER_PULSES_PER_REV * (t)))

/* 发完 FD 后等待到位的时间。位置模式下驱动器自行走完，这里只是等它。
 * 单次发送后统一等这么久，所以"全部电机"只用等一次，不叠加。
 * 走多圈/带载时如果没到位就继续下一步，把这里调大。 */
#define STEPPER_MOVE_WAIT_MS 6000UL
#define STEPPER_TURNS_PER_PRESS 1U  /* 每次按键走多少圈（main 使用） */

/* 方向约定（与例36 一致：CCW 为正/向上） */
#define STEPPER_DIR_POSITIVE 0U
#define STEPPER_DIR_NEGATIVE 1U

/* 上电初始化：配 USART2(PA2) + TIM2 1ms 时基 */
void    Stepper_Init(void);

/* ---- 单个电机 ---- */
uint8_t Stepper_EnableAt(uint8_t addr, uint8_t on);              /* F3 */
uint8_t Stepper_ZeroHereAt(uint8_t addr);                        /* 0A 6D */
uint8_t Stepper_MoveRelativeAt(uint8_t addr, uint8_t dir, int32_t pulses,
                               uint32_t wait_ms);                /* FD, raF=0 */
uint8_t Stepper_MoveAbsoluteAt(uint8_t addr, uint8_t dir, int32_t pulses,
                               uint32_t wait_ms);                /* FD, raF=1 */
uint8_t Stepper_RunSpeedAt(uint8_t addr, uint8_t dir, uint16_t rpm); /* F6 */
uint8_t Stepper_StopAt(uint8_t addr);                            /* FE 98 */

/* ---- 全部电机（按 STEPPER_MOTOR_1..COUNT 逐个发送）---- */
uint8_t Stepper_EnableAll(uint8_t on);
uint8_t Stepper_ZeroAll(void);
uint8_t Stepper_MoveAll(uint8_t dir, int32_t pulses, uint32_t wait_ms);
uint8_t Stepper_StopAll(void);
uint8_t Stepper_RunSpeedAll(uint8_t dir, uint16_t rpm);   /* 全部速度模式持续转 */
/* 全部电机按 dir 方向转 run_ms 毫秒后自动 FE 停车（按一下走一段，好观察） */
uint8_t Stepper_NudgeAll(uint8_t dir, uint16_t rpm, uint32_t run_ms);

/* 发一帧原始字节（调试/扩展用），len 为帧长 */
uint8_t Stepper_SendFrame(const uint8_t *frame, uint8_t len);

/* 独立 TIM2 毫秒计数（不占用 SysTick），可用于自己算运动时间 */
uint32_t Stepper_Millis(void);

/* 串口自检（回环）：把 PA2 与 PA3 用杜邦线短接，然后调用本函数。
 * 内部把 PA2 临时切回普通 IO 并开内部上拉，先判断 PA2 上是否有外部接线，
 * 再发测试字节并等回读。返回值：
 *   1 = 收到回显，串口收发和外接回环线都正常
 *   2 = 没有回显，但 PA2 被外部拉低（有线连着，但没形成回环或线接错）
 *   0 = PA2 上没有检测到任何外部接线
 * 结束后恢复 PA2 的复用功能，可继续正常发送。 */
uint8_t Stepper_LoopbackTest(void);

/* 取一帧驱动返回字节 / 查询接收是否溢出（只发不收时可用于调试） */
uint8_t  Stepper_Read(uint8_t *byte);
uint8_t  Stepper_RxError(void);

#endif
