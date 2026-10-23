#include "sys.h"
#include "delay.h"
#include "led.h"

/* ============ PA0 按键诊断程序（临时用，定位按键到底接在哪个引脚）============
 *
 * 玩法：按住按键不放，看板载 LED(PA1) 闪几下。
 *       闪 1 下 = PA0    闪 2 下 = PA8    闪 3 下 = PA15
 *       闪 4 下 = PC0    闪 5 下 = PB0    闪 6 下 = PB1
 *       完全不闪 = 这些引脚都没检测到电平变化（按键/接线/共地问题）
 *
 * 每个引脚的逻辑：只要读到低电平就记一次，然后等它回到高电平才允许再记，
 * 所以按住不放也只会计 1 次，不会一直加。
 * 上电时会先把 6 个引脚各闪一遍编号（1~6 下），确认 LED 本身是好的。
 */

#define NPIN 6U

static GPIO_TypeDef *const ports[NPIN] = {GPIOA, GPIOA, GPIOA, GPIOC, GPIOB, GPIOB};
static const uint16_t      pins[NPIN]  = {GPIO_Pin_0, GPIO_Pin_8, GPIO_Pin_15,
                                          GPIO_Pin_0, GPIO_Pin_0, GPIO_Pin_1};

static uint8_t done[NPIN];

static void Blink(uint8_t times)
{
    uint8_t i;
    for (i = 0U; i < times; i++)
    {
        LED2 = 0;               /* 低电平点亮 */
        delay_ms(180);
        LED2 = 1;
        delay_ms(180);
    }
    delay_ms(700);
}

static void Diag_Init(void)
{
    GPIO_InitTypeDef g;
    uint8_t i;

    RCC_AHB1PeriphClockCmd(RCC_AHB1Periph_GPIOA | RCC_AHB1Periph_GPIOB | RCC_AHB1Periph_GPIOC,
                           ENABLE);

    for (i = 0U; i < NPIN; i++)
    {
        GPIO_StructInit(&g);
        g.GPIO_Pin   = pins[i];
        g.GPIO_Mode  = GPIO_Mode_IN;
        g.GPIO_OType = GPIO_OType_PP;
        g.GPIO_PuPd  = GPIO_PuPd_UP;    /* 内部上拉：按键接 GND 时按下为低 */
        g.GPIO_Speed = GPIO_Speed_2MHz;
        GPIO_Init(ports[i], &g);

        done[i] = 0U;
    }
}

int main(void)
{
    uint8_t i;

    NVIC_PriorityGroupConfig(NVIC_PriorityGroup_2);
    SystemCoreClockUpdate();
    delay_init((uint8_t)(SystemCoreClock/1000000UL));

    LED_Init();
    Diag_Init();

    LED2 = 1;

    /* 自检：先依次闪 1~6 下，确认 LED 和程序都在跑 */
    for (i = 0U; i < NPIN; i++)
    {
        Blink((uint8_t)(i + 1U));
    }

    while (1)
    {
        for (i = 0U; i < NPIN; i++)
        {
            if (done[i] == 0U)
            {
                if (GPIO_ReadInputDataBit(ports[i], pins[i]) == 0U)
                {
                    done[i] = 1U;
                    Blink((uint8_t)(i + 1U));   /* 闪编号，告诉你是哪个引脚 */
                }
            }
            else
            {
                /* 已记录过，等它回到高电平，允许下次再触发 */
                if (GPIO_ReadInputDataBit(ports[i], pins[i]) != 0U)
                {
                    done[i] = 0U;
                }
            }
        }

        delay_ms(20);
    }
}
