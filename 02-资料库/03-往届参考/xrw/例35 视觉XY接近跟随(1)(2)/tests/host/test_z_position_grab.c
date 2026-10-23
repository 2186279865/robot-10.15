#include "motion_sequence.h"
#include "emm_port.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static uint32_t now,epoch,frame_time[512],fd_begin,down_arrived,up_arrived;
static uint32_t grip_time,acks,last_vision_slot,done_count,busy_rx_bytes;
static uint8_t frames[512][13],lengths[512],fd_count,grips;
static unsigned count;
static uint8_t mode,reply_index,reply_phase,fd_active,direction,abort_now;
static uint8_t approach_first;
static uint32_t return_begin;
static uint8_t opens;
static uint32_t release_time;
static unsigned vision_index,vision_length;
static char vision_line[32];
static uint8_t done_ids[8],vision_id=1U;
static uint32_t second_tracking_begin;
static uint32_t clearance_begin;
static uint32_t boot_begin,first_tracking_begin;
static unsigned tray_fd_count,tray_clear_count;
static uint32_t tray_started;
static uint32_t tray_targets[8];
static uint8_t last_id_change;

uint32_t EmmPort_Millis(void){return now;}
uint8_t App_StopRequested(void)
{
    return (uint8_t)(abort_now || (mode==3U && fd_active &&
                     (uint32_t)(now-fd_begin)>=100UL) ||
                     (mode==5U && sequence_state==SEQUENCE_RETURN &&
                     (uint32_t)(now-return_begin)>=100UL) ||
                     (mode==8U && fd_count==3U && sequence_state==SEQUENCE_PLACE_DOWN &&
                      (uint32_t)(now-fd_begin)>=100UL) ||
                     (mode==9U && sequence_state==SEQUENCE_RELEASE &&
                      (uint32_t)(now-release_time)>=100UL) ||
                     (mode==13U && fd_count>=4U && clearance_begin &&
                      sequence_state==SEQUENCE_X_CLEARANCE &&
                      (uint32_t)(now-clearance_begin)>=100UL) ||
                     (mode==15U && boot_begin && fd_count==0U &&
                      sequence_state==SEQUENCE_X_CLEARANCE &&
                      (uint32_t)(now-boot_begin)>=100UL) ||
                     (mode==17U && sequence_state==SEQUENCE_TRAY_POSITION &&
                      tray_fd_count &&
                      (uint32_t)(now-tray_started)>=100UL));
}
void App_WaitMs(uint32_t ms)
{
    while(ms-- && !App_StopRequested())now++;
}
uint8_t Console_GetCommand(uint8_t *b)
{
    uint32_t slot=(uint32_t)(now-epoch)/100UL;
    uint32_t approach_elapsed;
    if(sequence_state==SEQUENCE_XY && !completed_cycles && !first_tracking_begin)
        first_tracking_begin=now;
    approach_elapsed=first_tracking_begin?
                     1500UL+(uint32_t)(now-first_tracking_begin):
                     (uint32_t)(now-epoch);
    if(vision_index>=vision_length){
        if(slot==last_vision_slot)return 0U;
        if(approach_first==3U && approach_elapsed>=1800UL &&
           approach_elapsed<2400UL)return 0U; /* 视觉断流 */
        last_vision_slot=slot;vision_index=0U;
        if(completed_cycles){
            /* 第二轮编号3，X只需400ms：若净运动记录未清零就会多回程。 */
            if(sequence_state==SEQUENCE_XY && !second_tracking_begin)
                second_tracking_begin=now;
            if(second_tracking_begin &&
               (uint32_t)(now-second_tracking_begin)>=400UL &&
               (uint32_t)(now-second_tracking_begin)<600UL)
                memcpy(vision_line,"3,0,0,45.6\n",sizeof("3,0,0,45.6\n"));
            else memcpy(vision_line,"3,-120,60,45.6\n",sizeof("3,-120,60,45.6\n"));
        }else if(approach_first==4U && approach_elapsed>=1900UL &&
           approach_elapsed<2200UL)
            memcpy(vision_line,"1,0,-60,45.6\n",sizeof("1,0,-60,45.6\n"));
        else if(approach_first==2U && approach_elapsed>=1900UL &&
           approach_elapsed<2200UL)
            memcpy(vision_line,"1,-120,60,45.6\n",sizeof("1,-120,60,45.6\n"));
        else if(approach_first==3U && approach_elapsed>=2400UL &&
                approach_elapsed<2600UL)
            memcpy(vision_line,"1,0,0,45.6\n",sizeof("1,0,0,45.6\n"));
        else if(approach_first && approach_elapsed<2200UL)
            memcpy(vision_line,"1,120,-60,45.6\n",sizeof("1,120,-60,45.6\n"));
        else if(approach_first && approach_elapsed>=2300UL)
            memcpy(vision_line,"1,120,-60,45.6\n",sizeof("1,120,-60,45.6\n"));
        else memcpy(vision_line,"1,0,0,45.6\n",sizeof("1,0,0,45.6\n"));
        if(vision_id!=1U && !completed_cycles)
            snprintf(vision_line,sizeof(vision_line),"%u,0,0,45.6\n",vision_id);
        if(last_id_change && !completed_cycles){
            if(sequence_state!=SEQUENCE_XY)
                memcpy(vision_line,"6,120,-60,45.6\n",sizeof("6,120,-60,45.6\n"));
            else if(approach_elapsed<2200UL)
                memcpy(vision_line,"1,120,-60,45.6\n",sizeof("1,120,-60,45.6\n"));
            else
                memcpy(vision_line,"5,0,0,45.6\n",sizeof("5,0,0,45.6\n"));
        }
        vision_length=(unsigned)strlen(vision_line);
    }
    *b=(uint8_t)vision_line[vision_index++];
    if(sequence_state!=SEQUENCE_XY)busy_rx_bytes++;
    return 1U;
}
uint8_t Console_TakeRxError(void){return 0U;}
void Console_FlushRx(void){vision_index=vision_length;}
uint8_t Console_Write(const uint8_t *data,uint8_t n)
{
    char expected[16];
    uint8_t id=completed_cycles?3U:vision_id;
    if(n>=4U && memcmp(data,"ACK,",4U)==0){
        assert(sequence_state==SEQUENCE_XY);
        if(last_id_change)id=(now-first_tracking_begin<700UL)?1U:5U;
        snprintf(expected,sizeof(expected),"ACK,%u\n",id);
        assert(n==strlen(expected) && memcmp(data,expected,n)==0);
        acks++;return 1U;
    }
    assert(sequence_state==SEQUENCE_DONE && done_count<8U);
    if(last_id_change)id=5U;
    snprintf(expected,sizeof(expected),"DONE,%u\n",id);
    assert(n==strlen(expected) && memcmp(data,expected,n)==0);
    assert(clearance_begin && (uint32_t)(now-clearance_begin)>=
           X_CLEARANCE_MOVE_MS+X_CLEARANCE_SETTLE_MS);
    if(mode==11U)return 0U;
    done_ids[done_count++]=id;return 1U;
}
void S20_Servo_SetAngle(uint16_t angle)
{
    if(angle==CLAW_GRAB_DEG){grips++;grip_time=now;}
    else{
        assert(angle==CLAW_OPEN_DEG);opens++;
        if(sequence_state==SEQUENCE_RELEASE)release_time=now;
    }
}
uint8_t EmmPort_Write(uint8_t channel,const uint8_t *data,uint8_t n)
{
    assert(channel==0U && count<512U && n<=13U);
    assert(data[0]==1U || data[0]==3U || data[0]==4U || data[0]==5U);
    memcpy(frames[count],data,n);lengths[count]=n;frame_time[count++]=now;
    if(data[0]==TRAY_ADDRESS){
        if(data[1]==0x0A){
            const uint8_t expected[4]={3U,0x0A,0x6D,0x6B};
            assert(n==4U && memcmp(data,expected,4U)==0);
            tray_clear_count++;
        }else if(data[1]==0xFD){
            assert(sequence_state==SEQUENCE_TRAY_POSITION && fd_count==2U+(4U*completed_cycles));
            assert(n==13U && data[3]==0U && data[4]==30U &&
                   data[5]==0U && data[10]==1U && data[11]==0U);
            assert(tray_fd_count<8U);
            tray_targets[tray_fd_count++]=((uint32_t)data[6]<<24)|
                         ((uint32_t)data[7]<<16)|((uint32_t)data[8]<<8)|data[9];
            tray_started=now;
            if(mode==16U)return 0U; /* 托盘帧发送失败，不继续下降放置 */
        }
    }
    if(sequence_state==SEQUENCE_X_CLEARANCE && data[1]==0xF6){
        assert(data[0]==X_ADDRESS && data[2]==X_CLEARANCE_DIRECTION &&
               data[4]==X_CLEARANCE_SPEED_RPM && data[6]==0U);
        if(!fd_count){
            if(!boot_begin)boot_begin=now;
            if(mode==14U)return 0U;
        }else{
            if(!clearance_begin)clearance_begin=now;
            if(mode==12U)return 0U;
        }
    }
    if(sequence_state==SEQUENCE_RETURN && data[1]==0xF6){
        if(!return_begin)return_begin=now;
        if(mode==4U || (mode==6U && data[0]==5U))
            return 0U; /* 回程X/Y任一启动发送失败 */
    }
    if(data[0]==1U && data[1]==0xFD){
        fd_count++;fd_active=1U;fd_begin=now;direction=data[2];
        reply_index=reply_phase=0U;
        if(mode==2U || (mode==7U && fd_count==3U) ||
           (mode==10U && fd_count==4U))return 0U; /* FD本地发送失败 */
    }
    return 1U;
}
void EmmPort_Flush(uint8_t c)
{
    (void)c;assert(0); /* 不允许再调用反馈接收接口 */
}
uint8_t EmmPort_RxError(uint8_t c){(void)c;assert(0);return 0U;}
uint8_t EmmPort_Read(uint8_t c,uint8_t *b)
{
    (void)c;(void)b;assert(0);
    return 1U;
}
static void reset(uint32_t start,uint8_t test_mode)
{
    now=epoch=start;count=fd_count=grips=0U;acks=done_count=busy_rx_bytes=0U;
    mode=test_mode;fd_active=abort_now=reply_index=reply_phase=0U;
    approach_first=0U;
    vision_index=vision_length=0U;last_vision_slot=0xFFFFFFFFUL;
    down_arrived=up_arrived=grip_time=0U;
    return_begin=0U;
    opens=0U;
    release_time=0U;
    second_tracking_begin=0U;vision_id=1U;
    clearance_begin=0U;
    boot_begin=first_tracking_begin=0U;
    tray_fd_count=tray_clear_count=0U;tray_started=0U;last_id_change=0U;
}
static void verify_success(uint32_t start,uint8_t moving)
{
    uint8_t down[13]={1,0xFD,0,0x08,0xFC,0,0,1,0xF4,0,0,0,0x6B};
    uint8_t up[13]={1,0xFD,1,0x08,0xFC,0,0,1,0xF4,0,0,0,0x6B};
    unsigned i,down_index=0U,up_index=0U,place_down=0U,place_up=0U,fd_index=0U;
    unsigned clearance_index=0U,clearance_stop=0U;
    uint32_t last_return_stop=0U;
    reset(start,0U);approach_first=moving;assert(MotionSequence_Init());
    assert(MotionSequence_RunOnce() && sequence_state==SEQUENCE_DONE);
    assert(fd_count==4U && grips==1U && opens==2U);
    for(i=0U;i<count;i++){
        if(frames[i][0]==1U && frames[i][1]==0xFD){
            assert(lengths[i]==13U);
            if(fd_index==0U){assert(memcmp(frames[i],down,13U)==0);down_index=i;}
            else if(fd_index==1U){assert(memcmp(frames[i],up,13U)==0);up_index=i;}
            else if(fd_index==2U){assert(memcmp(frames[i],down,13U)==0);place_down=i;}
            else{assert(fd_index==3U && memcmp(frames[i],up,13U)==0);place_up=i;}
            fd_index++;
        }
        /* Z只能FD，绝不发送时间控制F6；无广播和同步触发。 */
        assert(!(frames[i][0]==1U && frames[i][1]==0xF6));
    }
    for(i=place_up+1U;i<count;i++){
        if(frames[i][0]==X_ADDRESS && frames[i][1]==0xF6 && !clearance_index)
            clearance_index=i;
        if(frames[i][0]==X_ADDRESS && frames[i][1]==0xFE)clearance_stop=i;
        assert(frames[i][0]!=Z_ADDRESS);
        if(frames[i][0]==Y_ADDRESS)assert(frames[i][1]==0xFE);
    }
    assert(clearance_index && clearance_stop>clearance_index);
    assert((uint32_t)(frame_time[clearance_index]-frame_time[place_up])==
           Z_POSITION_WAIT_MS);
    assert((uint32_t)(frame_time[clearance_stop]-frame_time[clearance_index])==
           X_CLEARANCE_MOVE_MS);
    assert((uint32_t)(grip_time-frame_time[down_index])==
           Z_POSITION_WAIT_MS+Z_BOTTOM_WAIT_MS);
    assert((uint32_t)(frame_time[up_index]-grip_time)==CLAW_GRAB_WAIT_MS);
    assert((uint32_t)(now-frame_time[up_index])>=
           Z_POSITION_WAIT_MS+RETURN_SETTLE_MS);
    assert((uint32_t)(release_time-frame_time[place_down])==
           Z_POSITION_WAIT_MS+Z_BOTTOM_WAIT_MS);
    assert((uint32_t)(frame_time[place_up]-release_time)==CLAW_RELEASE_WAIT_MS);
    assert((uint32_t)(now-frame_time[place_up])==
           Z_POSITION_WAIT_MS+X_CLEARANCE_MOVE_MS+X_CLEARANCE_SETTLE_MS);
    for(i=place_down;i<=place_up;i++)assert(frames[i][0]==Z_ADDRESS);
    assert(acks>=1U && done_count==1U && done_ids[0]==1U &&
           completed_cycles==1U && busy_rx_bytes>50U);
    for(i=down_index+1U;i<up_index;i++)assert(frames[i][1]!=0xFE);
    if(moving){
        unsigned x_f6=0U,y_f6=0U,x_stop=0U,y_stop=0U;
        unsigned return_frames=0U;
        for(i=0U;i<count;i++){
            if(frames[i][0]==4U && frames[i][1]==0xF6){
                if(i>=clearance_index)continue; /* 最后的负向短移单独验证 */
                if(i<down_index)x_f6++;
                else{
                    assert(i>up_index && i<place_down &&
                           (uint32_t)(frame_time[i]-frame_time[up_index])>=Z_POSITION_WAIT_MS);
                    /* 初始负向偏移较大，测试净去程为负，故回程方向00。 */
                    assert(frames[i][2]==0U);
                    assert(frames[i][4]==34U && frames[i][6]==0U);
                    return_frames++;
                }
            }
            if(frames[i][0]==5U && frames[i][1]==0xF6){
                if(i<down_index)y_f6++;
                else{
                    assert(i>up_index && i<place_down &&
                           (uint32_t)(frame_time[i]-frame_time[up_index])>=Z_POSITION_WAIT_MS);
                    assert(frames[i][2]==(moving==2U?1U:0U));
                    assert(frames[i][4]==150U && frames[i][6]==0U);
                    return_frames++;
                }
            }
            if(frames[i][1]==0xFE && i &&
               (uint32_t)(frame_time[i]-first_tracking_begin)>=400UL && i<down_index){
                if(frames[i][0]==4U)x_stop++;
                if(frames[i][0]==5U)y_stop++;
            }
        }
        assert(x_f6>=2U && y_f6>=2U &&
               x_stop>=1U && y_stop>=1U);
        assert(return_frames>=2U);
        for(i=down_index;i<=up_index;i++)assert(frames[i][0]==1U);
        for(i=up_index+1U;i<place_down;i++){
            if(frames[i][1]==0xFE &&
               (frames[i][0]==X_ADDRESS || frames[i][0]==Y_ADDRESS))
                last_return_stop=frame_time[i];
        }
        assert((uint32_t)(frame_time[place_down]-last_return_stop)==
               RETURN_SETTLE_MS+TRAY_POSITION_WAIT_MS);
        /* 根据真实发送帧在模拟时钟中积分，验证回程抵消净去程。
         * 同方向F6重发不另加位移，换向与断流后的FE能终止计时。 */
        {
            int64_t net[2]={0,0},amount;
            uint32_t stamp[2]={0U,0U},elapsed;
            uint16_t rpm[2]={0U,0U};
            uint8_t active[2]={0U,0U},dir[2]={0U,0U};
            unsigned axis;
            for(i=0U;i<count;i++){
                if(frames[i][0]!=X_ADDRESS && frames[i][0]!=Y_ADDRESS)continue;
                axis=frames[i][0]==4U?0U:1U;
                if(active[axis]){
                    elapsed=(uint32_t)(frame_time[i]-stamp[axis]);
                    amount=(int64_t)elapsed*rpm[axis];
                    net[axis]+=dir[axis]?-amount:amount;
                }
                stamp[axis]=frame_time[i];
                if(frames[i][1]==0xF6){
                    rpm[axis]=(uint16_t)(((uint16_t)frames[i][3]<<8)|frames[i][4]);
                    dir[axis]=frames[i][2];active[axis]=1U;
                }else if(frames[i][1]==0xFE)active[axis]=0U;
            }
            assert(net[0]==-(int64_t)X_CLEARANCE_SPEED_RPM*X_CLEARANCE_MOVE_MS &&
                   net[1]==0 && !active[0] && !active[1]);
        }
    }
    assert(!MotionSequence_RunOnce());
}
static void verify_two_cycles(void)
{
    unsigned before,i,second_start,second_fd;
    int64_t net[2]={0,0},amount;
    uint32_t stamps[2]={0,0},elapsed;
    uint16_t rpm[2]={0,0};
    uint8_t running[2]={0,0},dir[2]={0,0};
    unsigned axis;
    reset(0U,0U);approach_first=1U;
    assert(MotionSequence_Init() && MotionSequence_RunOnce());
    before=count;
    assert(MotionSequence_PrepareNext());
    assert(count==before && sequence_state==SEQUENCE_WAIT &&
           cycle_target_id==0U && completed_cycles==1U);
    second_start=count;
    clearance_begin=0U;
    assert(MotionSequence_RunOnce());
    assert(fd_count==8U && grips==2U && opens==4U &&
           completed_cycles==2U && done_count==2U &&
           done_ids[0]==1U && done_ids[1]==3U);
    assert(second_tracking_begin!=0U);
    second_fd=count;
    for(i=second_start;i<count;i++)
        if(frames[i][0]==1U && frames[i][1]==0xFD){second_fd=i;break;}
    assert(second_fd<count);
    for(i=second_start;i<count;i++){
        if(frames[i][0]!=X_ADDRESS && frames[i][0]!=Y_ADDRESS)continue;
        axis=frames[i][0]==4U?0U:1U;
        if(running[axis]){
            elapsed=(uint32_t)(frame_time[i]-stamps[axis]);
            amount=(int64_t)elapsed*rpm[axis];
            net[axis]+=dir[axis]?-amount:amount;
        }
        stamps[axis]=frame_time[i];
        if(frames[i][1]==0xF6){
            rpm[axis]=(uint16_t)(((uint16_t)frames[i][3]<<8)|frames[i][4]);
            dir[axis]=frames[i][2];running[axis]=1U;
            if(i<second_fd)assert(frames[i][2]==(axis==0U?1U:0U));
        }else if(frames[i][1]==0xFE)running[axis]=0U;
    }
    assert(net[0]==0 && net[1]==0 && !running[0] && !running[1]);
    assert(tray_clear_count==1U && tray_fd_count==2U &&
           tray_targets[0]==0U && tray_targets[1]==1067U);
    assert(!MotionSequence_RunOnce()); /* 未PrepareNext不重复执行 */
}
static void verify_clearance_fault(uint8_t test_mode)
{
    reset(0U,test_mode);approach_first=1U;
    assert(MotionSequence_Init());
    assert(!MotionSequence_RunOnce());
    assert(fd_count==4U && opens==2U && grips==1U && done_count==0U &&
           completed_cycles==0U);
    assert(sequence_state==(test_mode==13U?SEQUENCE_ABORT:SEQUENCE_FAULT));
    assert(frames[count-1U][0]==Z_ADDRESS && frames[count-1U][1]==0xFE);
    assert(!MotionSequence_PrepareNext());
}
static void verify_boot_only_once(void)
{
    unsigned i,start=0U,stop=0U,boot_commands=0U;
    reset(0U,0U);assert(MotionSequence_Init() && count==7U);
    assert(!boot_begin && !fd_count && !acks);
    assert(MotionSequence_RunOnce());
    assert(boot_begin && first_tracking_begin);
    for(i=4U;i<count && (uint32_t)(frame_time[i]-epoch)<
                              (uint32_t)(first_tracking_begin-epoch);i++){
        assert(frames[i][0]!=Z_ADDRESS);
        if(frames[i][1]==0xF6){
            assert(frames[i][0]==X_ADDRESS && frames[i][2]==1U &&
                   frames[i][4]==X_CLEARANCE_SPEED_RPM);
            if(!boot_commands)start=i;
            boot_commands++;
        }
        if(frames[i][0]==X_ADDRESS && frames[i][1]==0xFE)stop=i;
    }
    assert(boot_commands>=1U && stop>start);
    assert((uint32_t)(frame_time[stop]-frame_time[start])==X_CLEARANCE_MOVE_MS);
    assert((uint32_t)(first_tracking_begin-frame_time[stop])==X_CLEARANCE_SETTLE_MS);
    assert(MotionSequence_PrepareNext());
    boot_begin=0U;
    assert(MotionSequence_RunOnce());
    assert(boot_begin==0U); /* 第二轮没有再次执行上电短移 */
}
static void verify_boot_fault(uint8_t test_mode)
{
    reset(0U,test_mode);assert(MotionSequence_Init());
    assert(!MotionSequence_RunOnce() && fd_count==0U &&
           done_count==0U && acks==0U);
    assert(sequence_state==(test_mode==15U?SEQUENCE_ABORT:SEQUENCE_FAULT));
    assert(!MotionSequence_PrepareNext());
    assert(frames[count-1U][1]==0xFE);
}
static void verify_ids(void)
{
    const uint8_t ids[6]={1U,2U,3U,4U,5U,6U};
    const uint32_t targets[6]={0U,533U,1067U,1600U,2133U,2667U};
    unsigned i;
    for(i=0U;i<sizeof(ids);i++){
        reset(0U,0U);vision_id=ids[i];
        assert(MotionSequence_Init() && MotionSequence_RunOnce());
        assert(done_count==1U && done_ids[0]==ids[i] &&
               cycle_target_id==ids[i]);
        assert(tray_clear_count==1U && tray_fd_count==1U &&
               tray_targets[0]==targets[i]);
        assert((uint32_t)(frame_time[count-1U]-tray_started)>=TRAY_POSITION_WAIT_MS);
    }
}
static void verify_tray_failure(uint8_t test_mode)
{
    reset(0U,test_mode);vision_id=6U;
    assert(MotionSequence_Init());
    assert(!MotionSequence_RunOnce() && fd_count==2U && grips==1U && opens==1U);
    assert(done_count==0U && tray_fd_count==1U);
    assert(sequence_state==(test_mode==17U?SEQUENCE_ABORT:SEQUENCE_FAULT));
    assert(frames[count-2U][0]==TRAY_ADDRESS && frames[count-2U][1]==0xFE);
    assert(frames[count-1U][0]==Z_ADDRESS && frames[count-1U][1]==0xFE);
    assert(!MotionSequence_PrepareNext());
}
static void verify_invalid_id(void)
{
    reset(0U,0U);vision_id=12U;
    assert(MotionSequence_Init() && !MotionSequence_RunOnce());
    assert(sequence_state==SEQUENCE_FAULT && fd_count==0U &&
           tray_fd_count==0U && done_count==0U && grips==0U);
}
static void verify_last_id(void)
{
    reset(0U,0U);approach_first=1U;last_id_change=1U;
    assert(MotionSequence_Init() && MotionSequence_RunOnce());
    assert(cycle_target_id==5U && done_ids[0]==5U &&
           tray_fd_count==1U && tray_targets[0]==2133U);
}
static void verify_return_fault(uint8_t test_mode)
{
    reset(0U,test_mode);approach_first=1U;
    assert(MotionSequence_Init());
    assert(!MotionSequence_RunOnce());
    assert(!MotionSequence_PrepareNext() && done_count==0U);
    assert(fd_count==2U && grips==1U && opens==1U);
    assert(sequence_state==(test_mode==5U?SEQUENCE_ABORT:SEQUENCE_FAULT));
    assert(frames[count-1U][0]==1U && frames[count-1U][1]==0xFE);
    assert(!MotionSequence_RunOnce());
    assert(!MotionSequence_PrepareNext() && done_count==0U);
}
static void verify_fault(uint8_t test_mode)
{
    reset(0U,test_mode);assert(MotionSequence_Init());
    assert(!MotionSequence_RunOnce());
    assert(fd_count==1U && grips==0U);
    assert(sequence_state==(test_mode==3U?SEQUENCE_ABORT:SEQUENCE_FAULT));
    assert(frames[count-1U][0]==1U && frames[count-1U][1]==0xFE);
    assert(!MotionSequence_RunOnce());
}
static void verify_place_fault(uint8_t test_mode)
{
    unsigned before;
    reset(0U,test_mode);approach_first=1U;
    assert(MotionSequence_Init());
    assert(!MotionSequence_RunOnce());
    assert(grips==1U && fd_count==(test_mode==10U?4U:3U));
    assert(opens==((test_mode==9U || test_mode==10U)?2U:1U));
    assert(sequence_state==((test_mode==8U || test_mode==9U)?
                           SEQUENCE_ABORT:SEQUENCE_FAULT));
    assert(frames[count-1U][0]==Z_ADDRESS && frames[count-1U][1]==0xFE);
    before=count;assert(!MotionSequence_RunOnce() && count==before);
    assert(!MotionSequence_PrepareNext() && done_count==0U);
}
int main(void)
{
    verify_success(0U,0U);verify_success(0xFFFFFF00UL,0U);
    verify_success(0U,1U);verify_success(0xFFFFFF00UL,1U);
    verify_success(0U,2U);verify_success(0xFFFFFF00UL,2U);
    verify_success(0U,3U);
    verify_success(0U,4U); /* X先到位：两个轴回程时长不同，各自停车 */
    verify_fault(2U); /* 本地发送失败不能继续抓紧或抬升 */
    verify_fault(3U); /* 软件中止：停车，不继续抓取 */
    verify_return_fault(4U);verify_return_fault(5U);verify_return_fault(6U);
    verify_place_fault(7U);verify_place_fault(8U);
    verify_place_fault(9U);verify_place_fault(10U);
    verify_two_cycles();verify_ids();
    verify_last_id();verify_invalid_id();
    verify_tray_failure(16U);verify_tray_failure(17U);
    verify_boot_only_once();verify_boot_fault(14U);verify_boot_fault(15U);
    verify_clearance_fault(12U);verify_clearance_fault(13U);
    reset(0U,11U);assert(MotionSequence_Init());
    assert(!MotionSequence_RunOnce() && sequence_state==SEQUENCE_FAULT &&
           done_count==0U && completed_cycles==0U);
    assert(!MotionSequence_PrepareNext());
    puts("TRAY_REPEAT_PICK_TESTS_OK: 03-absolute/6-angles/last-ID-freeze/before-place/3s-wait/zero-once/multicycle/invalid-ID/fault/abort/wrap");
    return 0;
}
