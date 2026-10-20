#ifndef __KEY_H
#define __KEY_H

#include "stm32f4xx.h"
#include "delay.h"

/*
 * PA0 按键模块（内部下拉、按下为高电平，消抖逻辑照抄例36）
 *
 * 用法：
 *     Key_Init();
 *     ...
 *     if (Key_Poll(ms)) { ... }   // ms 由主循环按 KEY_TASK_MS 累加
 *     Key_TaskDelay();
 */

#define KEY_PORT        GPIOA
#define KEY_PIN         GPIO_Pin_0
#define KEY_DEBOUNCE_MS 20U     /* 与例36 的 KEY_DEBOUNCE_MS 一致 */
#define KEY_TASK_MS     1U      /* 与例36 的 delay_ms(1) 一致 */

void    Key_Init(void);
uint8_t Key_Poll(uint32_t now); /* 1 = 本次产生了一次有效按键（松手后的按下） */
void    Key_TaskDelay(void);    /* 主循环节奏延时，KEY_TASK_MS 毫秒 */

#endif
