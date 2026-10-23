#ifndef __SERVO_H
#define __SERVO_H

#include "stm32f4xx.h"
#include "stm32f4xx_gpio.h"
#include "stm32f4xx_tim.h"

/* TIM4_CH1 output on PB6, 50 Hz PWM, matching the servo used by example 35. */
#define SERVO_PORT       GPIOB
#define SERVO_PIN        GPIO_Pin_6
#define SERVO_MIN_US     500U
#define SERVO_MAX_US     2500U

/* 舵机角度量程：0 度对应 SERVO_MIN_US，SERVO_MAX_ANGLE 度对应 SERVO_MAX_US。
 * AngleToPulse 按这个量程做线性映射，gripper 等模块也引用这里，避免多处写死。 */
#define SERVO_MAX_ANGLE  270U

#define SERVO_OPEN_DEG   0U
#define SERVO_GRAB_DEG   180U

void Servo_Init(void);
/* Same as Servo_Init(), but the PWM starts directly at `angle` so the servo
 * never sees a wrong pulse at power-on (gripper must boot at its release
 * position instead of 0 degrees). */
void Servo_InitAt(uint16_t angle);
void Servo_SetAngle(uint16_t angle);
/* 停止输出脉冲 = 舵机卸力（不再保持角度）。之后任何 Servo_SetAngle() 都会自动恢复输出。
 * 只用于“夹着东西长时间不动会发热”的兜底场景，正常流程不要调用。 */
void Servo_Release(void);
uint16_t Servo_AngleToPulse(uint16_t angle);

#endif


