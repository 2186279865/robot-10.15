#include "motion_sequence.h"
#include "emm_port.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
static uint32_t now,count,frame_times[128],abort_at,trigger_count;
static uint8_t frames[128][13],sizes[128],stop_requested;
static uint8_t fail_address,fail_function,fail_count;
static uint8_t angles[16];
static uint32_t angle_times[16],angle_count;

uint32_t EmmPort_Millis(void){return now;}
uint8_t App_StopRequested(void){return stop_requested;}
void App_WaitMs(uint32_t ms)
{
    while(ms-- && !stop_requested){
        now++;
        if(abort_at && now>=abort_at)stop_requested=1U;
    }
}
uint8_t EmmPort_Write(uint8_t channel,const uint8_t *data,uint8_t n)
{
    assert(channel==0 && n<=13 && count<128);
    assert((n==4 && data[0]==0 && data[1]==0xFF) || data[0]==Z_ADDRESS ||
           (X_AXIS_ENABLED && data[0]==X_ADDRESS) ||
           (Y_AXIS_ENABLED && data[0]==Y_ADDRESS));
    /* 禁用轴的任何帧（含初始化/故障停车）都会让测试失败。 */
    memcpy(frames[count],data,n);sizes[count]=n;frame_times[count++]=now;
    if(fail_count && data[0]==fail_address && data[1]==fail_function){
        fail_count--;return 0;
    }
    if(data[1]==0xFF){
        const uint8_t expected[4]={0,0xFF,0x66,0x6B};
        assert(n==4 && memcmp(data,expected,4)==0 && !stop_requested);
        trigger_count++;
    }
    return 1;
}
void S20_Servo_SetAngle(uint8_t angle)
{
    assert(angle==140 || angle==20);assert(angle_count<16);
    angles[angle_count]=angle;angle_times[angle_count++]=now;
}
static void reset(uint32_t start)
{
    now=start;count=angle_count=trigger_count=0;abort_at=0;stop_requested=0;
    fail_address=fail_function=fail_count=0;
}
static void stop_frame(uint32_t i,uint8_t address)
{
    uint8_t expected[5]={address,0xFE,0x98,0,0x6B};
    assert(sizes[i]==5 && memcmp(frames[i],expected,5)==0);
}
static void speed_frame(uint32_t i,uint8_t address,uint8_t dir,uint16_t rpm,
                        uint8_t sync)
{
    uint8_t expected[8]={address,0xF6,dir,0,0,0,0,0x6B};
    expected[3]=(uint8_t)(rpm>>8);expected[4]=(uint8_t)rpm;
    expected[6]=sync;
    assert(sizes[i]==8 && memcmp(frames[i],expected,8)==0);
}
static void test_full_cycle(uint32_t start)
{
    uint32_t i,before,base,next_start,next_stop,z_down_index,xy_time=0,trigger;
    uint8_t enable[6]={0,0xF3,0xAB,1,0,0x6B};
    uint8_t addresses[3],n=0;
    uint32_t enabled_axes=1U+X_AXIS_ENABLED+Y_AXIS_ENABLED;
#if X_AXIS_ENABLED
    addresses[n++]=X_ADDRESS;
    xy_time=X_MOVE_MS;
#endif
#if Y_AXIS_ENABLED
    addresses[n++]=Y_ADDRESS;
    if(Y_MOVE_MS>xy_time)xy_time=Y_MOVE_MS;
#endif
    addresses[n++]=Z_ADDRESS;
    reset(start);assert(MotionSequence_Init());
    assert(sequence_state==SEQUENCE_WAIT && count==enabled_axes*2U);
    assert(n==enabled_axes);
    for(i=0;i<enabled_axes;i++)stop_frame(i,addresses[i]);
    for(i=enabled_axes;i<enabled_axes*2U;i++){
        enable[0]=frames[i-enabled_axes][0];
        assert(sizes[i]==6 && memcmp(frames[i],enable,6)==0);
    }
    assert((uint32_t)(now-start)==500);
    base=count;
    assert(MotionSequence_RunOnce());
    assert(sequence_state==SEQUENCE_DONE && count==base+enabled_axes*2U+4U);
    trigger=base+X_AXIS_ENABLED+Y_AXIS_ENABLED+1U;
    assert(trigger_count==1 && sizes[trigger]==4 && frames[trigger][1]==0xFF);
    assert(sizes[trigger-1U]==5 && frames[trigger-1U][0]==1 &&
           frames[trigger-1U][1]==0xFE && frames[trigger-1U][3]==1);
    assert((uint32_t)(frame_times[trigger]-frame_times[base])==
           (X_AXIS_ENABLED+Y_AXIS_ENABLED+1U)*SYNC_PREPARE_GAP_MS);
    next_start=base;next_stop=trigger+1U;
#if X_AXIS_ENABLED
    speed_frame(next_start,4,0,34,1);
    stop_frame(next_stop,4);
    assert((uint32_t)(frame_times[next_stop]-frame_times[trigger])==X_MOVE_MS);
    next_start++;next_stop++;
#endif
#if Y_AXIS_ENABLED
    speed_frame(next_start,5,1,150,1);
    stop_frame(next_stop,5);
    assert((uint32_t)(frame_times[next_stop]-frame_times[trigger])==Y_MOVE_MS);
    next_start++;next_stop++;
#endif
    z_down_index=next_stop;
    speed_frame(z_down_index,1,0,500,0);stop_frame(z_down_index+1U,1);
    assert((uint32_t)(frame_times[z_down_index]-frame_times[z_down_index-1U])==500);
    assert((uint32_t)(frame_times[z_down_index+1U]-frame_times[z_down_index])==5400);
    assert(angle_count==2 && angles[0]==140 && angles[1]==20);
    assert((uint32_t)(angle_times[1]-frame_times[z_down_index+1U])==500);
    speed_frame(z_down_index+2U,1,1,500,0);stop_frame(z_down_index+3U,1);
    assert((uint32_t)(frame_times[z_down_index+2U]-angle_times[1])==3000);
    assert((uint32_t)(frame_times[z_down_index+3U]-frame_times[z_down_index+2U])==5400);
    assert((uint32_t)(now-start)==16300UL+xy_time+
           (X_AXIS_ENABLED+Y_AXIS_ENABLED+1U)*SYNC_PREPARE_GAP_MS);
    before=count;assert(!MotionSequence_RunOnce() && count==before);
}
static void check_failure_stops(void)
{
    uint32_t n=1U+X_AXIS_ENABLED+Y_AXIS_ENABLED,i=count-n;
    assert(count>=n);
#if X_AXIS_ENABLED
    stop_frame(i++,4);
#endif
#if Y_AXIS_ENABLED
    stop_frame(i++,5);
#endif
    stop_frame(i,1);
    assert(sequence_state==SEQUENCE_FAULT || sequence_state==SEQUENCE_ABORT);
}
static void test_failures_and_aborts(void)
{
    uint32_t i,before;
    reset(0);assert(MotionSequence_Init());
    fail_address=Y_AXIS_ENABLED?5U:4U;fail_function=0xF6;fail_count=1;
    assert(!MotionSequence_RunOnce());check_failure_stops();
    assert(trigger_count==0); /* Y准备失败：不触发已准备的X */
    assert(angle_count==1); /* XY失败禁止下降或抓紧 */
    for(i=0;i<count;i++)assert(!(frames[i][0]==1 && frames[i][1]==0xF6));
    before=count;assert(!MotionSequence_RunOnce() && count==before);
    reset(0);assert(MotionSequence_Init());
    fail_address=4;fail_function=0xF6;fail_count=1;
    assert(!MotionSequence_RunOnce());check_failure_stops();
    assert(trigger_count==0 && angle_count==1);
    reset(0);assert(MotionSequence_Init());
    fail_address=1;fail_function=0xFE;fail_count=1;
    assert(!MotionSequence_RunOnce());check_failure_stops();
    assert(trigger_count==0 && angle_count==1); /* Z安全准备失败不启动XY */
    reset(0);assert(MotionSequence_Init());
    fail_address=0;fail_function=0xFF;fail_count=1;
    assert(!MotionSequence_RunOnce());check_failure_stops();
    assert(trigger_count==0 && angle_count==1);
    reset(0);assert(MotionSequence_Init());abort_at=1505;
    assert(!MotionSequence_RunOnce());check_failure_stops();
    assert(trigger_count==0 && sequence_state==SEQUENCE_ABORT);
    reset(0);fail_address=1;fail_function=0xF3;fail_count=1;
    assert(!MotionSequence_Init());check_failure_stops();
    assert(!MotionSequence_RunOnce());
    reset(0);assert(MotionSequence_Init());
    fail_address=X_AXIS_ENABLED?4U:5U;fail_function=0xFE;fail_count=2;
    assert(!MotionSequence_RunOnce());check_failure_stops();
    assert(angle_count==1);
    reset(0);assert(MotionSequence_Init());abort_at=2000;
    assert(!MotionSequence_RunOnce());check_failure_stops();
    assert(sequence_state==SEQUENCE_ABORT && angle_count==1);
    reset(0);assert(MotionSequence_Init());abort_at=10000;
    assert(!MotionSequence_RunOnce());check_failure_stops(); /* Z下降中中止 */
    assert(sequence_state==SEQUENCE_ABORT && angle_count==1);
    reset(0);assert(MotionSequence_Init());abort_at=15000;
    assert(!MotionSequence_RunOnce());check_failure_stops(); /* 夹紧等待中中止 */
    assert(angle_count==2); /* 不自动松开、不继续抬升 */
    for(i=0;i<count;i++)assert(!(frames[i][0]==1 && frames[i][1]==0xF6 && frames[i][2]==1));
}
int main(void)
{
    test_full_cycle(0);test_full_cycle(0xFFFFFF00UL);
    test_failures_and_aborts();
    puts("FROM32_GRAB_TESTS_OK: sync-frames/common-timing/Z-stop/grip/lift/once/failure/abort/wrap");
#if !Y_AXIS_ENABLED
    puts("Y05_DISABLED_VERIFIED: no Y frames in initialization/run/stop/failure/abort");
#endif
#if !X_AXIS_ENABLED
    puts("X04_DISABLED_VERIFIED: no X frames in initialization/run/stop/failure/abort");
#endif
    return 0;
}
