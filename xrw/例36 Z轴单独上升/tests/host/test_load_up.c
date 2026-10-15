#include "motion_sequence.h"
#include "emm_port.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
static uint32_t now,origin,abort_ms,count,times[32];
static uint8_t frames[32][8],sizes[32],fail_function,fail_left;
uint32_t EmmPort_Millis(void){return now;}
uint8_t App_StopRequested(void)
{return (uint8_t)(abort_ms && (uint32_t)(now-origin)>=abort_ms);}
void App_WaitMs(uint32_t ms)
{while(ms-- && !App_StopRequested())now++;}
uint8_t EmmPort_Write(uint8_t channel,const uint8_t *data,uint8_t n)
{
 assert(channel==0U && data[0]==1U && n<=8U && count<32U);
 memcpy(frames[count],data,n);sizes[count]=n;times[count++]=now;
 if(fail_left && data[1]==fail_function){fail_left--;return 0U;}
 return 1U;
}
static void reset(uint32_t start)
{now=origin=start;count=abort_ms=0U;fail_function=fail_left=0U;}
static void success(uint32_t start)
{
 const uint8_t speed[8]={1U,0xF6,1U,1U,0x2C,0x80,0U,0x6B};
 const uint8_t stop[5]={1U,0xFE,0x98,0U,0x6B};
 reset(start);assert(MotionSequence_Init() && count==2U);
 assert((uint32_t)(now-start)==500UL);
 assert(MotionSequence_RunOnce() && sequence_state==SEQUENCE_DONE && count==4U);
 assert(sizes[2]==8U && memcmp(frames[2],speed,8U)==0);
 assert((uint32_t)(times[3]-times[2])==9961UL);
 assert(sizes[3]==5U && memcmp(frames[3],stop,5U)==0);
 assert(!MotionSequence_RunOnce() && count==4U);
}
int main(void)
{
 success(0U);success(0xFFFFFF00UL);
 reset(0U);assert(MotionSequence_Init());abort_ms=1000UL;
 assert(!MotionSequence_RunOnce() && sequence_state==SEQUENCE_ABORT);
 assert(frames[count-1U][1]==0xFE); /* 等待失败不能跳过停车 */
 reset(0U);assert(MotionSequence_Init());fail_function=0xF6;fail_left=1U;
 assert(!MotionSequence_RunOnce() && sequence_state==SEQUENCE_FAULT);
 assert(frames[count-1U][1]==0xFE);
 puts("Z_LOAD_UP_TESTS_OK: Z01-only/up/300RPM/acc128/9961ms/stop/abort/failure/once/wrap");
 return 0;
}
