#include "s20_servo.h"

void S20_Servo_Init(void)
{
    GPIO_InitTypeDef g;
    TIM_TimeBaseInitTypeDef t;
    TIM_OCInitTypeDef oc;

    RCC_AHB1PeriphClockCmd(RCC_AHB1Periph_GPIOB, ENABLE);
    RCC_APB1PeriphClockCmd(RCC_APB1Periph_TIM4, ENABLE);

    GPIO_PinAFConfig(GPIOB, GPIO_PinSource6, GPIO_AF_TIM4);
    GPIO_StructInit(&g);
    g.GPIO_Pin = S20_SERVO_PIN;
    g.GPIO_Mode = GPIO_Mode_AF;
    g.GPIO_Speed = GPIO_Speed_100MHz;
    g.GPIO_OType = GPIO_OType_PP;
    g.GPIO_PuPd = GPIO_PuPd_UP;
    GPIO_Init(S20_SERVO_PORT, &g);

    /*
     * TIM4时钟84MHz，PSC=83后为1MHz；
     * ARR=19999得到20ms周期，即50Hz。
     */
    TIM_TimeBaseStructInit(&t);
    t.TIM_Prescaler = 83;
    t.TIM_Period = 19999;
    t.TIM_CounterMode = TIM_CounterMode_Up;
    t.TIM_ClockDivision = TIM_CKD_DIV1;
    TIM_TimeBaseInit(TIM4, &t);

    TIM_OCStructInit(&oc);
    oc.TIM_OCMode = TIM_OCMode_PWM1;
    oc.TIM_OutputState = TIM_OutputState_Enable;
    oc.TIM_OCPolarity = TIM_OCPolarity_High;
    oc.TIM_Pulse = S20_Servo_AngleToPulse(S20_SERVO_OPEN_DEG);
    TIM_OC1Init(TIM4, &oc);
    TIM_OC1PreloadConfig(TIM4, TIM_OCPreload_Enable);
    TIM_ARRPreloadConfig(TIM4, ENABLE);
    TIM_Cmd(TIM4, ENABLE);

    /* 首个PWM即为配置的松爪角度140度，不先输出旧角度。 */
    S20_Servo_SetAngle(S20_SERVO_OPEN_DEG);
}

uint16_t S20_Servo_AngleToPulse(uint8_t angle)
{
    uint32_t pulse;
    if (angle > 180U) angle = 180U;
    pulse = S20_SERVO_MIN_US +
            ((uint32_t)(S20_SERVO_MAX_US - S20_SERVO_MIN_US) * angle) / 180U;
    return (uint16_t)pulse;
}

void S20_Servo_SetAngle(uint8_t angle)
{
    TIM_SetCompare1(TIM4, S20_Servo_AngleToPulse(angle));
}
