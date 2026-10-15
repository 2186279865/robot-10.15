#ifndef S20_SERVO_H
#define S20_SERVO_H

#include "sys.h"
#include "app_config.h"

/* TIM4_CH1：PB6。例26使用PA2/PA3，例28使用PC6/PC7/PC8，资源不冲突。 */
#define S20_SERVO_PORT GPIOB
#define S20_SERVO_PIN  GPIO_Pin_6

/* 180°限位舵机的可调脉宽端点，单位us。 */
#define S20_SERVO_MIN_US  500U
#define S20_SERVO_MAX_US 2500U
#define S20_SERVO_TARGET_DEG 60U /* 保留为兼容旧配置；往返程序使用0/180° */
#define S20_SERVO_SWEEP_HOLD_MS 1000UL
#define S20_SERVO_OPEN_DEG CLAW_OPEN_DEG
#define S20_SERVO_OPEN_SETTLE_MS CLAW_OPEN_WAIT_MS
#define S20_SERVO_GRAB_DEG CLAW_GRAB_DEG
#define S20_SERVO_GRAB_SETTLE_MS CLAW_GRAB_WAIT_MS

void S20_Servo_Init(void);
void S20_Servo_SetAngle(uint8_t angle);
uint16_t S20_Servo_AngleToPulse(uint8_t angle);

#endif
