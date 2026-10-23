#include "servo.h"

void Servo_Init(void)
{
    Servo_InitAt(SERVO_OPEN_DEG);
}

void Servo_InitAt(uint16_t angle)
{
    GPIO_InitTypeDef gpio;
    TIM_TimeBaseInitTypeDef timer;
    TIM_OCInitTypeDef channel;

    RCC_AHB1PeriphClockCmd(RCC_AHB1Periph_GPIOB, ENABLE);
    RCC_APB1PeriphClockCmd(RCC_APB1Periph_TIM4, ENABLE);

    GPIO_PinAFConfig(GPIOB, GPIO_PinSource6, GPIO_AF_TIM4);
    GPIO_StructInit(&gpio);
    gpio.GPIO_Pin = SERVO_PIN;
    gpio.GPIO_Mode = GPIO_Mode_AF;
    gpio.GPIO_Speed = GPIO_Speed_100MHz;
    gpio.GPIO_OType = GPIO_OType_PP;
    gpio.GPIO_PuPd = GPIO_PuPd_UP;
    GPIO_Init(SERVO_PORT, &gpio);

    /* TIM4 clock is 84 MHz; divide to 1 MHz and use a 20 ms period (50 Hz). */
    TIM_TimeBaseStructInit(&timer);
    timer.TIM_Prescaler = 83;
    timer.TIM_Period = 19999;
    timer.TIM_CounterMode = TIM_CounterMode_Up;
    timer.TIM_ClockDivision = TIM_CKD_DIV1;
    TIM_TimeBaseInit(TIM4, &timer);

    TIM_OCStructInit(&channel);
    channel.TIM_OCMode = TIM_OCMode_PWM1;
    channel.TIM_OutputState = TIM_OutputState_Enable;
    channel.TIM_OCPolarity = TIM_OCPolarity_High;
    if (angle > SERVO_MAX_ANGLE) angle = SERVO_MAX_ANGLE;
    channel.TIM_Pulse = Servo_AngleToPulse(angle);
    TIM_OC1Init(TIM4, &channel);
    TIM_OC1PreloadConfig(TIM4, TIM_OCPreload_Enable);
    TIM_ARRPreloadConfig(TIM4, ENABLE);
    TIM_Cmd(TIM4, ENABLE);
    TIM_SetCounter(TIM4, 0);

    Servo_SetAngle(angle);
}

uint16_t Servo_AngleToPulse(uint16_t angle)
{
    uint32_t pulse;
    if (angle > 270U) angle = 270U;
    pulse = SERVO_MIN_US +
            ((uint32_t)(SERVO_MAX_US - SERVO_MIN_US) * angle) / 270U;
    return (uint16_t)pulse;
}

void Servo_SetAngle(uint16_t angle)
{
    TIM_SetCompare1(TIM4, Servo_AngleToPulse(angle));
}

/* 卸力：把比较值设成 0，占空比 0% → 输出恒低，舵机收不到脉冲就不出力。
 * 定时器本身不关，下一次 Servo_SetAngle() 直接覆盖比较值即可恢复输出。 */
void Servo_Release(void)
{
    TIM_SetCompare1(TIM4, 0U);
}

