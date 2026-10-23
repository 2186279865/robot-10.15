#include "motion_sequence.h"
#include "emm_port.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static uint32_t now,epoch,count,times[512],acks,abort_elapsed;
static uint8_t frames[512][13],sizes[512],angles[8],angle_count;
static uint32_t angle_times[8];
static uint8_t fail_address,fail_function,fail_left;
static char rx_line[64];
static unsigned rx_index,rx_length;
static uint32_t emitted_slot;
static uint8_t initially_centered;

uint32_t EmmPort_Millis(void){return now;}
uint8_t App_StopRequested(void)
{
    return (uint8_t)(abort_elapsed &&
                     (uint32_t)(now-epoch)>=abort_elapsed);
}
void App_WaitMs(uint32_t ms)
{
    while(ms-- && !App_StopRequested())now++;
}
uint8_t EmmPort_Write(uint8_t channel,const uint8_t *data,uint8_t size)
{
    assert(channel==0U && size<=13U && count<512U);
    assert(data[0]==4U || data[0]==5U || data[0]==1U);
    memcpy(frames[count],data,size);sizes[count]=size;times[count++]=now;
    if(fail_left && data[0]==fail_address && data[1]==fail_function){
        fail_left--;return 0U;
    }
    return 1U;
}
uint8_t Console_GetCommand(uint8_t *b)
{
    uint32_t elapsed=(uint32_t)(now-epoch),slot=elapsed/100UL;
    if(rx_index>=rx_length){
        if(slot==emitted_slot)return 0U;
        emitted_slot=slot;rx_index=0U;
        /* 每100ms一条新帧：前700ms接近，到位后还持续发送偏离坐标，
         * 验证抓取期间只ACK，不让视觉再次启动XY。 */
        if(initially_centered || (elapsed>=1200UL && elapsed<1300UL))
            memcpy(rx_line,"1,0,0,45.6\n",sizeof("1,0,0,45.6\n"));
        else memcpy(rx_line,"1,120,-30,45.6\n",sizeof("1,120,-30,45.6\n"));
        rx_length=(unsigned)strlen(rx_line);
    }
    *b=(uint8_t)rx_line[rx_index++];return 1U;
}
uint8_t Console_Write(const uint8_t *data,uint8_t size)
{
    assert(size==6U && memcmp(data,"ACK,1\n",6U)==0);
    acks++;return 1U;
}
void S20_Servo_SetAngle(uint8_t angle)
{
    assert(angle_count<8U);
    angles[angle_count]=angle;angle_times[angle_count++]=now;
}
static void reset(uint32_t start)
{
    epoch=now=start;count=acks=0U;angle_count=0U;
    abort_elapsed=0U;fail_left=0U;rx_index=rx_length=0U;
    emitted_slot=0xFFFFFFFFUL;initially_centered=0U;
}
static unsigned find_frame(uint8_t address,uint8_t function,
                            uint8_t direction,unsigned start)
{
    unsigned i;
    for(i=start;i<count;i++){
        if(frames[i][0]==address && frames[i][1]==function &&
           (function!=0xF6 || frames[i][2]==direction))return i;
    }
    assert(0);return 0U;
}
static void verify_complete(uint32_t start,uint8_t centered)
{
    unsigned down,down_stop,up,up_stop,i;
    uint32_t last_xy_stop=0U,x_speeds=0U,y_speeds=0U;
    reset(start);initially_centered=centered;
    assert(MotionSequence_Init());
    assert(count==6U && (uint32_t)(now-epoch)==500UL);
    assert(MotionSequence_RunOnce());
    assert(sequence_state==SEQUENCE_DONE);
    down=find_frame(1U,0xF6,0U,6U);
    down_stop=find_frame(1U,0xFE,0U,down+1U);
    up=find_frame(1U,0xF6,1U,down_stop+1U);
    up_stop=find_frame(1U,0xFE,0U,up+1U);
    for(i=6U;i<down;i++){
        if(frames[i][1]==0xFE && frames[i][0]!=1U)
            last_xy_stop=times[i];
        if(frames[i][1]==0xF6){
            assert(frames[i][6]==0U && sizes[i]==8U);
            if(frames[i][0]==4U){
                assert(frames[i][4]==34U && frames[i][2]==0U);
                x_speeds++;
            }else{
                assert(frames[i][0]==5U && frames[i][4]==150U &&
                       frames[i][2]==1U);
                y_speeds++;
            }
        }
    }
    if(!centered)assert(x_speeds>=3U && y_speeds>=3U);
    assert((uint32_t)(times[down]-last_xy_stop)==XY_ARRIVAL_WAIT_MS);
    {
        unsigned xy_stops=0U;
        for(i=6U;i<down;i++)
            if(frames[i][1]==0xFE && frames[i][0]!=1U)xy_stops++;
        assert(xy_stops>=XY_STOP_REPEAT_COUNT);
    }
    assert((uint32_t)(times[down_stop]-times[down])==Z_MOVE_MS);
    assert(angle_count==1U && angles[0]==20U);
    assert((uint32_t)(angle_times[0]-times[down_stop])==Z_BOTTOM_WAIT_MS);
    assert((uint32_t)(times[up]-angle_times[0])==CLAW_GRAB_WAIT_MS);
    assert((uint32_t)(times[up_stop]-times[up])==Z_MOVE_MS);
    assert(acks>50U);
    for(i=down;i<count;i++)assert(frames[i][0]==1U);
    i=count;assert(!MotionSequence_RunOnce() && count==i);
}
static void verify_faults(void)
{
    unsigned i;
    reset(0U);assert(MotionSequence_Init());abort_elapsed=1450UL;
    assert(!MotionSequence_RunOnce() && sequence_state==SEQUENCE_ABORT);
    assert(angle_count==0U);
    for(i=6U;i<count;i++)assert(!(frames[i][0]==1U && frames[i][1]==0xF6));
    reset(0U);assert(MotionSequence_Init());
    fail_address=4U;fail_function=0xFE;fail_left=1U;
    assert(!MotionSequence_RunOnce() && sequence_state==SEQUENCE_FAULT);
    assert(angle_count==0U);
    reset(0U);assert(MotionSequence_Init());abort_elapsed=2000UL;
    assert(!MotionSequence_RunOnce() && sequence_state==SEQUENCE_ABORT);
    assert(angle_count==0U);
    assert(frames[count-1U][0]==1U && frames[count-1U][1]==0xFE);
    for(i=6U;i<count;i++)assert(!(frames[i][0]==1U &&
                                frames[i][1]==0xF6 && frames[i][2]==1U));
}
int main(void)
{
    verify_complete(0U,0U);
    verify_complete(0xFFFFFF00UL,0U);
    verify_complete(0U,1U);
    verify_faults();
    puts("GRAB_DELAY_TESTS_OK: XY-retry/stop/500ms/Z-down/grip/3s/Z-up/ACK/abort/once/wrap");
    return 0;
}
