#ifndef __GRIPPER_H
#define __GRIPPER_H

#include "stm32f4xx.h"

/*
 * 抓夹（舵机夹爪）独立模块
 *
 * 约定：
 *   - 上电位置 = 夹爪松开位置 GRIPPER_OPEN_DEG
 *   - 每调用一次 Gripper_Grab() 就走 GRIPPER_STEP_DEG 度，逐步收紧
 *   - 走到 GRIPPER_GRAB_DEG 就夹紧；反方向用 Gripper_Release() 逐步松开
 *
 * 用法（配合按键）：
 *     Delay_Init(); LED_Init(); Servo_Init();
 *     Gripper_Init();                     // 上电停在松开位置
 *     ...
 *     if (Gripper_Grab()) { ... }         // 按一下按键 = 抓一步，到夹紧位置返回 0
 *
 * 本模块只管角度和动作时序，不碰按键、不碰 LED，方便单独复用。
 */

#define GRIPPER_STEP_DEG      45U     /* 每次动作改变的角度 */
#define GRIPPER_OPEN_DEG      180U    /* 松开位置（上电位置） */
#define GRIPPER_GRAB_DEG      0U      /* 夹紧位置 */
#define GRIPPER_STEP_WAIT_MS  200U    /* 每走一步后给舵机的到位时间 */

/* 上电：舵机转到松开位置，模块进入松开状态 */
void    Gripper_Init(void);

/* 抓一步：朝夹紧方向走 GRIPPER_STEP_DEG 度。
 * 返回 1 = 本次确实走了一步；返回 0 = 已经在夹紧位置，没有动作（便于调用方判断）。 */
uint8_t Gripper_Grab(void);

/* 松一步：朝松开方向走 GRIPPER_STEP_DEG 度。
 * 返回 1 = 本次确实走了一步；返回 0 = 已经在松开位置。 */
uint8_t Gripper_Release(void);

/* 立刻跳到夹紧位置 / 松开位置（不逐步走），用于需要一步到位的场合 */
void    Gripper_GrabNow(void);
void    Gripper_ReleaseNow(void);

/* 当前夹爪角度与状态 */
uint16_t Gripper_GetAngle(void);
uint8_t  Gripper_IsGrabbed(void);   /* 1 = 已到夹紧位置 */
uint8_t  Gripper_IsOpen(void);      /* 1 = 已到松开位置 */

#endif
