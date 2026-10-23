#include "stm32f4xx.h"
#include "delay.h"
#include "led.h"
#include "tft.h"

/* Independent display test; uses the same startup, clock and delay as 10.15. */
static void test_page(void)
{
    TFT_Clear(TFT_BLACK);
    TFT_FillRect(0U, 0U, 128U, 1U, TFT_WHITE);
    TFT_FillRect(0U, 159U, 128U, 1U, TFT_WHITE);
    TFT_FillRect(0U, 0U, 1U, 160U, TFT_WHITE);
    TFT_FillRect(127U, 0U, 1U, 160U, TFT_WHITE);
    TFT_Text(8U, 6U, "10.17 TFT TEST", TFT_YELLOW, TFT_BLACK);
    TFT_Text(8U, 26U, "ST7735S 128x160", TFT_WHITE, TFT_BLACK);
    TFT_Text(8U, 46U, "STM32F407 SPI", TFT_CYAN, TFT_BLACK);
    TFT_FillRect(8U, 70U, 36U, 24U, TFT_RED);
    TFT_FillRect(46U, 70U, 36U, 24U, TFT_GREEN);
    TFT_FillRect(84U, 70U, 36U, 24U, TFT_BLUE);
    TFT_Text(8U, 96U, " R    G    B", TFT_WHITE, TFT_BLACK);
    TFT_Text(8U, 116U, "RUNNING...", TFT_GREEN, TFT_BLACK);
    TFT_FillRect(7U, 139U, 114U, 12U, TFT_WHITE);
    TFT_FillRect(8U, 140U, 112U, 10U, TFT_BLACK);
}

int main(void)
{
    static const uint16_t colors[] = {
        TFT_RED, TFT_GREEN, TFT_BLUE, TFT_WHITE, TFT_BLACK
    };
    uint16_t progress;
    uint8_t i;
    g_tft_stage = 1U;
    SystemCoreClockUpdate();
    LED_Init();
    LED1_OFF;
    Delay_Init();
    g_tft_stage = 2U;
    /* A distinct signature, before touching the display or PB1. */
    for (i = 0U; i < 3U; ++i)
    {
        LED1_ON;
        Delay_ms(200U);
        LED1_OFF;
        Delay_ms(200U);
    }
    TFT_Init();
    g_tft_stage = 8U;
    while (1)
    {
        for (i = 0U; i < 5U; ++i)
        {
            TFT_Clear(colors[i]);
            Delay_ms(800U);
        }
        test_page();
        for (progress = 0U; progress < 112U; ++progress)
        {
            TFT_FillRect(8U + progress, 140U, 1U, 10U, TFT_GREEN);
            Delay_ms(30U);
        }
        Delay_ms(1500U);
    }
}
