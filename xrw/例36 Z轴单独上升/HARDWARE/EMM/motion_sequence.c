#include "motion_sequence.h"
#include "app_config.h"
#include "emm_port.h"
#include "emm_protocol.h"

volatile uint8_t sequence_state;
static uint8_t ready,key_raw,key_stable,release_seen;
static uint32_t key_changed,segment_begin;
static uint8_t enable_motor(uint8_t a){uint8_t f[6];Emm_BuildEnable(f,a,1U);return EmmPort_Write(0U,f,6U);}
static uint8_t position_z(uint8_t direction)
{
 uint8_t frame[13];
 int32_t pulses=(int32_t)Z_TRAVEL_PULSES;
 if(direction)pulses=-pulses;
 Emm_BuildPosition(frame,Z_ADDRESS,pulses,Z_SPEED_RPM,Z_ACCEL,0U,0U);
 /* 相对FD只发送一次，不周期重发，否则可能叠加圈数。 */
 if(App_StopRequested() || !EmmPort_Write(0U,frame,13U))return 0U;
 segment_begin=EmmPort_Millis();return 1U;
}
static uint8_t stop_motor(uint8_t a){uint8_t f[5];Emm_BuildStop(f,a);return EmmPort_Write(0U,f,5U);}
static uint8_t wait_ms(uint32_t n){App_WaitMs(n);return(uint8_t)!App_StopRequested();}
static uint8_t fail_sequence(void)
{
 uint8_t i;
 ready=0U;
 sequence_state=App_StopRequested()?SEQUENCE_ABORT:SEQUENCE_FAULT;
 for(i=0U;i<3U;i++)if(stop_motor(Z_ADDRESS))break;
 return 0U; /* 等待被中止也必须尝试FE，不能因短路跳过停车。 */
}
uint8_t MotionSequence_Init(void)
{
 ready=0U;sequence_state=SEQUENCE_WAIT;
 key_raw=key_stable=1U;release_seen=0U;
 if(!stop_motor(Z_ADDRESS)||!enable_motor(Z_ADDRESS))return fail_sequence();
 if(!wait_ms(MOTOR_ENABLE_WAIT_MS))return fail_sequence();
 key_changed=EmmPort_Millis();ready=1U;return 1U;
}
uint8_t MotionSequence_RunOnce(void)
{
 /* 只由消抖后的按键事件调用，上电main不直接调用。 */
 if(!ready || (sequence_state!=SEQUENCE_WAIT && sequence_state!=SEQUENCE_DONE))
  return 0U;
 if(!position_z(Z_UP_DIRECTION))return fail_sequence();
 sequence_state=SEQUENCE_Z_UP;return 1U;
}
void MotionSequence_Task(uint8_t key_pressed)
{
 uint32_t now=EmmPort_Millis();
 uint8_t changed=0U;
 if(!ready)return;
 if(App_StopRequested()){(void)fail_sequence();return;}
 key_pressed=key_pressed?1U:0U;
 if(key_raw!=key_pressed){key_raw=key_pressed;key_changed=now;}
 if(key_stable!=key_raw && (uint32_t)(now-key_changed)>=KEY_DEBOUNCE_MS){
  key_stable=key_raw;changed=1U;
 }
 if(sequence_state==SEQUENCE_Z_UP){
  release_seen=0U;
  if((uint32_t)(now-segment_begin)>=Z_POSITION_WAIT_MS){
   segment_begin=now;sequence_state=SEQUENCE_GRAB; /* 本例只作顶部稳定等待，无舵机 */
  }
  return;
 }
 if(sequence_state==SEQUENCE_GRAB){
  release_seen=0U;
  if((uint32_t)(now-segment_begin)>=Z_BOTTOM_WAIT_MS){
   if(!position_z(Z_DOWN_DIRECTION)){(void)fail_sequence();return;}
   sequence_state=SEQUENCE_Z_DOWN;
  }
  return;
 }
 if(sequence_state==SEQUENCE_Z_DOWN){
  release_seen=0U;
  if((uint32_t)(now-segment_begin)>=Z_POSITION_WAIT_MS)
   sequence_state=SEQUENCE_DONE;
  return;
 }
 if(!key_stable){release_seen=1U;return;}
 /* 长按一次；运动期间按键丢弃，完成后必须松开再按，不自动循环。 */
 if(changed && release_seen){
  release_seen=0U;
  (void)MotionSequence_RunOnce();
 }
}
