/* key_inline.h - PA0 按键模块的实现（供本工程内联编译用）
 *
 * 配置与消抖逻辑照抄例36（Z轴单独上升，该例按键实测好用）：
 *   - GPIO_PuPd_DOWN 内部下拉，按键另一端接 3.3V => 按下读到 1，松开读到 0；
 *   - 20ms 消抖（KEY_DEBOUNCE_MS），1ms 轮询（KEY_TASK_MS）；
 *   - 必须"松开过一次"之后的下一次按下才算数，所以长按只触发一次、不连发。
 *
 * 接口声明见 key.h，本文件只提供实现。
 * 以后若把 Drive\Source\key.c 正式加进 Keil 工程，请删掉 main.c 里的
 * #include "key_inline.h"，避免函数重复定义。
 */

#include "key.h"

static uint8_t  key_raw;        /* 本次采样到的原始电平 */
static uint8_t  key_stable;     /* 消抖后的稳定电平 */
static uint8_t  release_seen;   /* 已经出现过一次松开 */
static uint32_t key_changed;    /* 原始电平上次变化的时刻(ms) */
static uint32_t key_press_ms;   /* 稳定按下时刻(ms)，用于限制最快触发间隔 */

static uint8_t Key_Read(void)
{
    return (uint8_t)GPIO_ReadInputDataBit(KEY_PORT, KEY_PIN);
}

void Key_Init(void)
{
    GPIO_InitTypeDef key;

    RCC_AHB1PeriphClockCmd(RCC_AHB1Periph_GPIOA, ENABLE);
    GPIO_StructInit(&key);
    key.GPIO_Pin   = KEY_PIN;
    key.GPIO_Mode  = GPIO_Mode_IN;
    key.GPIO_PuPd  = GPIO_PuPd_DOWN;    /* 与例36 完全一致：下拉、高电平有效 */
    key.GPIO_Speed = GPIO_Speed_2MHz;
    GPIO_Init(KEY_PORT, &key);

    key_raw      = Key_Read();
    key_stable   = key_raw;
    release_seen = 0U;
    key_changed  = 0U;
    /* 取负的消抖时间，保证上电后第一次按下不会被"最小触发间隔"挡掉 */
    key_press_ms = 0U - KEY_DEBOUNCE_MS;
}

/* 每隔 KEY_TASK_MS 调用一次。返回 1 表示产生了一次新的有效按键（松手后的按下）。 */
uint8_t Key_Poll(uint32_t now)
{
    uint8_t changed = 0U;
    uint8_t pressed;

    pressed = Key_Read();

    if (key_raw != pressed)                 /* 原始电平变了，重新计消抖时间 */
    {
        key_raw     = pressed;
        key_changed = now;
    }

    if ((key_stable != key_raw) && ((uint32_t)(now - key_changed) >= KEY_DEBOUNCE_MS))
    {
        key_stable = key_raw;               /* 稳定保持 20ms，确认电平翻转 */
        changed    = 1U;
    }

    /* 未按下：记录"已经松手"，允许下一次按下触发 */
    if (key_stable == 0U)
    {
        release_seen = 1U;
        return 0U;
    }

    /* 已按下：只在"上次是松开状态"且消抖刚确认按下时触发一次，长按不连发 */
    if (changed && release_seen && ((uint32_t)(now - key_press_ms) >= KEY_DEBOUNCE_MS))
    {
        release_seen = 0U;
        key_press_ms = now;
        return 1U;
    }

    return 0U;
}

/* 供主循环使用的推荐节奏：延时一个 KEY_TASK_MS，保持 1ms 步进 */
void Key_TaskDelay(void)
{
    Delay_ms(KEY_TASK_MS);
}
