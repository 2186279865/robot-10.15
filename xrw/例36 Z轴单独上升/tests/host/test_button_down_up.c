#include "motion_sequence.h"
#include "emm_port.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
static uint32_t now,origin,times[32],count;
static uint8_t frames[32][13],sizes[32],abort_requested,fail_fd;
uint32_t EmmPort_Millis(void){return now;}
uint8_t App_StopRequested(void){return abort_requested;}
void App_WaitMs(uint32_t ms){now+=ms;}
uint8_t EmmPort_Write(uint8_t channel,const uint8_t *data,uint8_t n)
{
 assert(channel==0U && data[0]==1U && n<=13U && count<32U);
 memcpy(frames[count],data,n);sizes[count]=n;times[count++]=now;
 if(fail_fd && data[1]==0xFD){fail_fd=0U;return 0U;}return 1U;
}
static void key_for(uint8_t pressed,uint32_t ms)
{
 while(ms--){now++;MotionSequence_Task(pressed);}
}
static void reset(uint32_t start)
{now=origin=start;count=abort_requested=fail_fd=0U;}
static void expected(unsigned i,uint8_t dir)
{
 uint8_t frame[13]={1U,0xFD,0U,1U,0x2C,0x80,0U,0U,0xFA,0U,0U,0U,0x6B};
 frame[2]=dir;
 assert(sizes[i]==13U && memcmp(frames[i],frame,13U)==0);
}
static void full(uint32_t start)
{
 uint32_t down;
 reset(start);assert(MotionSequence_Init() && count==2U);
 key_for(1U,100U);assert(count==2U);
 key_for(0U,30U);key_for(1U,5U);key_for(0U,5U);
 assert(count==2U);key_for(1U,30U);
 assert(sequence_state==SEQUENCE_Z_UP && count==3U);expected(2U,1U);
 down=times[2U];
 key_for(1U,200U);key_for(0U,30U);key_for(1U,30U);
 assert(count==3U); /* 忙中按下不叠加 */
 now=down+Z_POSITION_WAIT_MS;MotionSequence_Task(1U);
 assert(sequence_state==SEQUENCE_GRAB && count==3U);
 key_for(1U,Z_BOTTOM_WAIT_MS);
 assert(sequence_state==SEQUENCE_Z_DOWN && count==4U);expected(3U,0U);
 assert((uint32_t)(times[3]-times[2])==Z_POSITION_WAIT_MS+Z_BOTTOM_WAIT_MS);
 key_for(1U,Z_POSITION_WAIT_MS);
 assert(sequence_state==SEQUENCE_DONE && count==4U);
 key_for(1U,100U);assert(count==4U);
 key_for(0U,30U);key_for(1U,30U);
 assert(count==5U && sequence_state==SEQUENCE_Z_UP);expected(4U,1U);
 abort_requested=1U;MotionSequence_Task(1U);
 assert(sequence_state==SEQUENCE_ABORT && frames[count-1U][1]==0xFE);
}
static void failures(void)
{
 reset(0U);assert(MotionSequence_Init());key_for(0U,30U);
 fail_fd=1U;key_for(1U,30U);
 assert(sequence_state==SEQUENCE_FAULT && frames[count-1U][1]==0xFE);
 reset(0U);assert(MotionSequence_Init());key_for(0U,30U);key_for(1U,30U);
 key_for(0U,Z_POSITION_WAIT_MS);fail_fd=1U;key_for(0U,Z_BOTTOM_WAIT_MS);
 assert(sequence_state==SEQUENCE_FAULT && frames[count-1U][1]==0xFE);
}
int main(void)
{
 full(0U);full(0xFFFFFF00UL);failures();
 puts("Z_BUTTON_CYCLE_TESTS_OK: no-auto/PA0/20-up+20-down/FD-once/300RPM/acc128/7s-wait/debounce/busy/fault/abort/wrap");
 return 0;
}
