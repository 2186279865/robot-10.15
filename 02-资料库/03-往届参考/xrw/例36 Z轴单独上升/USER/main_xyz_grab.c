#include "sys.h"
#include "delay.h"
#include "led.h"
#include "emm_port.h"
#include "motion_sequence.h"

/* PA0按一次：Z01上升20圈 -> 顶部等待 -> 下降20圈。
 * 不控制X/Y、视觉或舵机；上电不自动转，长按不重复。 */
int main(void)
{
    NVIC_PriorityGroupConfig(NVIC_PriorityGroup_2);
    SystemCoreClockUpdate();
    delay_init((uint8_t)(SystemCoreClock/1000000UL));
    LED_Init();
    EmmPort_Init();
    {
        GPIO_InitTypeDef key;
        RCC_AHB1PeriphClockCmd(RCC_AHB1Periph_GPIOA,ENABLE);
        GPIO_StructInit(&key);
        key.GPIO_Pin=GPIO_Pin_0;key.GPIO_Mode=GPIO_Mode_IN;
        key.GPIO_PuPd=GPIO_PuPd_DOWN;
        GPIO_Init(GPIOA,&key);
    }
    (void)MotionSequence_Init();
    while(1){
        MotionSequence_Task((uint8_t)GPIO_ReadInputDataBit(GPIOA,GPIO_Pin_0));
        if(sequence_state==SEQUENCE_FAULT || sequence_state==SEQUENCE_ABORT){
            LED2=!LED2;delay_ms(100);
        }else{
            LED2=(sequence_state==SEQUENCE_WAIT || sequence_state==SEQUENCE_DONE)?1:0;
            delay_ms(1);
        }
    }
}
