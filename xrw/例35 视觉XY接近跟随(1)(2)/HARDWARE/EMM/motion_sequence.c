#include "motion_sequence.h"
#include "app_config.h"
#include "emm_port.h"
#include "emm_protocol.h"
#include "s20_servo.h"

volatile uint8_t sequence_state;
volatile uint8_t cycle_target_id;
volatile uint32_t completed_cycles;
static uint8_t ready,executed;
static uint8_t boot_x_pending;
static uint8_t line[64],used;
static uint8_t discard_line;
typedef struct {
 int64_t net_rpm_ms;
 uint32_t sampled_ms;
 uint16_t rpm;
 uint8_t direction,running;
} AxisTravel;
static AxisTravel x_travel,y_travel;
static uint8_t recording_travel;
static int64_t next_x_origin_offset; /* 下一轮起步位置相对原放置点的短移偏移 */
/* 方向00记正、01记负；仅积分已发送F6到已发送FE的时间。
 * 同速F6重发不重新计时，视觉断流停车时间不计入净位移。
 * 没有电机位置反馈，记录的是估算运动量，不是实际位移。 */
static void account_axis(AxisTravel *axis,uint32_t now)
{
 uint32_t elapsed=(uint32_t)(now-axis->sampled_ms);
 int64_t amount=(int64_t)axis->rpm*elapsed;
 if(axis->running)axis->net_rpm_ms+=axis->direction?-amount:amount;
 axis->sampled_ms=now;
}
static AxisTravel *axis_record(uint8_t address)
{
 if(address==X_ADDRESS)return &x_travel;
 if(address==Y_ADDRESS)return &y_travel;
 return 0;
}

typedef struct { uint8_t id; int32_t x,y; } VisionTarget;

static uint8_t enable_motor(uint8_t a){uint8_t f[6];Emm_BuildEnable(f,a,1U);return EmmPort_Write(0U,f,6U);}
static uint8_t speed_motor(uint8_t a,uint8_t d,uint16_t r)
{
 uint8_t f[8],ok;
 AxisTravel *axis=axis_record(a);
 Emm_BuildSpeed(f,a,d,r,0U);ok=EmmPort_Write(0U,f,8U);
 if(recording_travel && axis){
  account_axis(axis,EmmPort_Millis());
  if(ok){axis->rpm=r;axis->direction=d;axis->running=1U;}
 }
 return ok;
}
static uint8_t stop_motor(uint8_t a)
{
 uint8_t f[5],ok;
 AxisTravel *axis=axis_record(a);
 Emm_BuildStop(f,a);ok=EmmPort_Write(0U,f,5U);
 if(recording_travel && axis){
  account_axis(axis,EmmPort_Millis());
  if(ok)axis->running=0U;
 }
 return ok;
}
static uint8_t vision_get(VisionTarget *t);
static uint8_t wait_ms(uint32_t n);
static void discard_vision(void)
{
 uint8_t byte;
 /* 抓取/回程/放置只丢弃字节，不解析坐标、不ACK、不修改追踪目标。 */
 (void)Console_TakeRxError();
 while(Console_GetCommand(&byte)){}
 used=0U;discard_line=1U;
}
static uint8_t report_done(uint8_t id)
{
 uint8_t msg[9],n=0U;
 msg[n++]='D';msg[n++]='O';msg[n++]='N';msg[n++]='E';msg[n++]=',';
 if(id>=100U)msg[n++]=(uint8_t)('0'+id/100U);
 if(id>=10U)msg[n++]=(uint8_t)('0'+(id/10U)%10U);
 msg[n++]=(uint8_t)('0'+id%10U);msg[n++]='\n';
 return Console_Write(msg,n);
}
static uint8_t fail_sequence(void)
{
 uint8_t attempt,ok;
 ready=0U;
 sequence_state=App_StopRequested()?SEQUENCE_ABORT:SEQUENCE_FAULT;
 for(attempt=0U;attempt<3U;attempt++){
  ok=1U;
  if(!stop_motor(X_ADDRESS))ok=0U;
#if Y_AXIS_ENABLED
  if(!stop_motor(Y_ADDRESS))ok=0U;
#endif
#if TRAY_ENABLED
  if(!stop_motor(TRAY_ADDRESS))ok=0U;
#endif
  if(!stop_motor(Z_ADDRESS))ok=0U;
  if(ok)break;
 }
 return 0U; /* 不释放Z使能，不松爪，不自动重启。 */
}
static uint8_t grab_wait(uint32_t n)
{
 uint32_t start=EmmPort_Millis();
 while((uint32_t)(EmmPort_Millis()-start)<n){
  discard_vision();
  if(!wait_ms(1U))return 0U;
 }
 return 1U;
}
static uint8_t move_z(uint8_t d)
{
 uint8_t cmd[13];
 int32_t pulses=(int32_t)Z_TRAVEL_PULSES;
 /* 相对位置命令只能发送一次。驱动器自行走完40圈，不用定时FE截断。 */
 if(d)pulses=-pulses;
 Emm_BuildPosition(cmd,Z_ADDRESS,pulses,Z_SPEED_RPM,Z_ACCEL,0U,0U);
 if(App_StopRequested() || !EmmPort_Write(0U,cmd,13U))return 0U;
 /* 只等待流程时间，不发FE截断圈数、不重发FD、不依赖PA3/驱动ACK。
  * 若负载使实际运动慢于等待时间，可能尚未到位就继续夹紧/反向。 */
 return grab_wait(Z_POSITION_WAIT_MS);
}
static uint8_t tray_angle_for_id(uint8_t id,uint16_t *angle)
{
 /* 使用XY最后一条参与定位的编号，不使用抓取/回程期间的新编号。 */
 static const uint16_t degrees[6]={0U,60U,120U,180U,240U,300U};
 if(id<1U || id>6U)return 0U;
 *angle=degrees[id-1U];return 1U;
}
static uint8_t position_tray(void)
{
 uint16_t angle;
 uint8_t frame[13];
 uint64_t magnitude;
 int32_t pulses;
 if(App_StopRequested() || !tray_angle_for_id(cycle_target_id,&angle))return 0U;
 magnitude=((uint64_t)TRAY_PULSES_PER_REV*angle+180U)/360U;
 pulses=(int32_t)magnitude;
 if(TRAY_POSITIVE_DIRECTION)pulses=-pulses;
 sequence_state=SEQUENCE_TRAY_POSITION;
 /* 绝对位置、地址02、sync=0。多轮不能用相对角度重复叠加，
  * 例如上次300度、本次60度，应回到绝对60度而不是再转60度。 */
 Emm_BuildPosition(frame,TRAY_ADDRESS,pulses,TRAY_SPEED_RPM,TRAY_ACCEL,1U,0U);
 if(!EmmPort_Write(0U,frame,13U))return 0U;
 return grab_wait(TRAY_POSITION_WAIT_MS); /* 不解析坐标，不ACK，不等待电机反馈 */
}
static uint8_t wait_ms(uint32_t n){App_WaitMs(n);return(uint8_t)!App_StopRequested();}
static uint8_t move_x_clearance(void)
{
 uint32_t begin,retry;
 int64_t amount;
 recording_travel=0U;
 sequence_state=SEQUENCE_X_CLEARANCE;
 if(App_StopRequested())return 0U;
 /* Y始终保持停车；X仅使用04单地址、普通F6，不涉及Z命令。 */
#if Y_AXIS_ENABLED
 if(!stop_motor(Y_ADDRESS))return 0U;
#endif
 if(!speed_motor(X_ADDRESS,X_CLEARANCE_DIRECTION,X_CLEARANCE_SPEED_RPM))
  return 0U;
 begin=retry=EmmPort_Millis();
 while((uint32_t)(EmmPort_Millis()-begin)<X_CLEARANCE_MOVE_MS){
  if(App_StopRequested())return 0U;
  if((uint32_t)(EmmPort_Millis()-retry)>=MOTOR_COMMAND_RETRY_MS){
   if(!speed_motor(X_ADDRESS,X_CLEARANCE_DIRECTION,X_CLEARANCE_SPEED_RPM))
    return 0U;
   retry=EmmPort_Millis(); /* 重发不延长设定的总运动时间 */
  }
  discard_vision();
  if(!wait_ms(1UL))return 0U;
 }
 if(!stop_motor(X_ADDRESS))return 0U;
 amount=(int64_t)X_CLEARANCE_SPEED_RPM*
        (uint32_t)(EmmPort_Millis()-begin);
 next_x_origin_offset=X_CLEARANCE_DIRECTION?-amount:amount;
 /* 将短移偏移带入下一轮记录，使下一次放置仍回原放置点。
  * 不是实际位置反馈，误差仍可能累计。 */
 return grab_wait(X_CLEARANCE_SETTLE_MS);
}
static uint8_t return_axis_plan(const AxisTravel *axis,uint16_t rpm,
                                uint32_t *duration,uint8_t *direction)
{
 uint64_t magnitude=(uint64_t)(axis->net_rpm_ms<0?
                              -axis->net_rpm_ms:axis->net_rpm_ms);
 uint64_t ms=(magnitude+rpm/2U)/rpm;
 if(ms>RETURN_MAX_AXIS_MS)return 0U;
 *duration=(uint32_t)ms;
 *direction=axis->net_rpm_ms>0?1U:0U; /* 逆向抵消本轮净运动量 */
 return 1U;
}
static uint8_t return_xy_to_start(void)
{
 uint32_t x_ms,y_ms,x_begin=0U,y_begin=0U,x_retry=0U,y_retry=0U,now;
 uint8_t x_dir,y_dir,x_running=0U,y_running=0U;
 recording_travel=0U; /* 回程不能再累加到本轮去程记录 */
 if(!return_axis_plan(&x_travel,RETURN_X_SPEED_RPM,&x_ms,&x_dir) ||
    !return_axis_plan(&y_travel,RETURN_Y_SPEED_RPM,&y_ms,&y_dir))return 0U;
 sequence_state=SEQUENCE_RETURN;
 if(App_StopRequested())return 0U;
 if(x_ms){
  if(!speed_motor(X_ADDRESS,x_dir,RETURN_X_SPEED_RPM))return 0U;
  x_begin=x_retry=EmmPort_Millis();x_running=1U;
 }
#if Y_AXIS_ENABLED
 if(y_ms){
  if(App_StopRequested() || !speed_motor(Y_ADDRESS,y_dir,RETURN_Y_SPEED_RPM))
   return 0U;
  y_begin=y_retry=EmmPort_Millis();y_running=1U;
 }
#endif
 while(x_running || y_running){
  if(App_StopRequested())return 0U;
  now=EmmPort_Millis();
  if(x_running){
   if((uint32_t)(now-x_begin)>=x_ms){
    if(!stop_motor(X_ADDRESS))return 0U;
    x_running=0U;
   }else if((uint32_t)(now-x_retry)>=MOTOR_COMMAND_RETRY_MS){
    if(!speed_motor(X_ADDRESS,x_dir,RETURN_X_SPEED_RPM))return 0U;
    x_retry=EmmPort_Millis(); /* 仅更新重发时刻，不延长总回程时间 */
   }
  }
#if Y_AXIS_ENABLED
  if(y_running){
   if((uint32_t)(EmmPort_Millis()-y_begin)>=y_ms){
    if(!stop_motor(Y_ADDRESS))return 0U;
    y_running=0U;
   }else if((uint32_t)(EmmPort_Millis()-y_retry)>=MOTOR_COMMAND_RETRY_MS){
    if(!speed_motor(Y_ADDRESS,y_dir,RETURN_Y_SPEED_RPM))return 0U;
    y_retry=EmmPort_Millis();
   }
  }
#endif
  discard_vision(); /* 回程坐标直接丢弃，不参与控制，不ACK */
  if((x_running||y_running)&&!wait_ms(1UL))return 0U;
 }
 return grab_wait(RETURN_SETTLE_MS);
}
static uint8_t number(const uint8_t **pp,int32_t *v)
{
 const uint8_t*p=*pp;int32_t s=1,n=0;uint8_t d=0U;
 if(*p=='-'){s=-1;p++;}else if(*p=='+')p++;
 while(*p>='0'&&*p<='9'){n=n*10+(*p-'0');p++;d=1U;}
 if(!d)return 0U;*v=s*n;*pp=p;return 1U;
}
static uint8_t parse(const uint8_t*s,uint8_t n,VisionTarget*t)
{
 const uint8_t*p=s,*e=s+n;int32_t id,x,y;
 if(!number(&p,&id)||p>=e||*p++!=',')return 0U;
 if(!number(&p,&x)||p>=e||*p++!=',')return 0U;
 if(!number(&p,&y)||p>=e||*p++!=',')return 0U;
 if(p>=e||id<=0||id>255)return 0U;
 t->id=(uint8_t)id;t->x=x;t->y=y;return 1U;
}
static uint8_t vision_get(VisionTarget*t)
{
 uint8_t b;
 if(sequence_state!=SEQUENCE_XY)return 0U;
 if(Console_TakeRxError()){used=0U;discard_line=1U;}
 while(Console_GetCommand(&b)){
  if(discard_line){
   if(b=='\n'){discard_line=0U;used=0U;}
   continue;
  }
  if(b=='\n'||b=='\r'){
   if(used){uint8_t n=used;line[used]=0U;used=0U;
    if(parse(line,n,t)){uint8_t a[8],k=0U;a[k++]='A';a[k++]='C';a[k++]='K';a[k++]=',';
     if(t->id>=100U)a[k++]=(uint8_t)('0'+t->id/100U);
     if(t->id>=10U)a[k++]=(uint8_t)('0'+(t->id/10U)%10U);
     a[k++]=(uint8_t)('0'+t->id%10U);a[k++]='\n';Console_Write(a,k);return 1U;}
   }
  }else if(b==0U || used>=sizeof(line)-1U){
   used=0U;discard_line=1U;
  }else line[used++]=b;
 }
 return 0U;
}
static uint8_t follow_xy(void)
{
 VisionTarget t,last={0};uint8_t have=0U,xr=0U,yr=0U,xd=0U,yd=0U;
 uint32_t fresh=0U,xcmd=0U,ycmd=0U;
 sequence_state=SEQUENCE_XY;used=0U;
 recording_travel=1U;
 x_travel.sampled_ms=y_travel.sampled_ms=EmmPort_Millis();
 for(;;){
  if(App_StopRequested())return 0U;
  while(vision_get(&t)){
   last=t;cycle_target_id=t.id;have=1U;fresh=EmmPort_Millis();
  }
  if(!have||(uint32_t)(EmmPort_Millis()-fresh)>VISION_TIMEOUT_MS){
   if(xr&&!stop_motor(X_ADDRESS))return 0U;
#if Y_AXIS_ENABLED
   if(yr&&!stop_motor(Y_ADDRESS))return 0U;
#endif
   xr=yr=0U;if(!wait_ms(1U))return 0U;continue;
  }
  if(last.x>VISION_TOLERANCE_PX){
   if(!xr||xd!=X_DIRECTION){if(xr&&!stop_motor(X_ADDRESS))return 0U;if(!speed_motor(X_ADDRESS,X_DIRECTION,X_SPEED_RPM))return 0U;xr=1U;xd=X_DIRECTION;xcmd=EmmPort_Millis();}
   else if((uint32_t)(EmmPort_Millis()-xcmd)>=MOTOR_COMMAND_RETRY_MS){if(!speed_motor(X_ADDRESS,xd,X_SPEED_RPM))return 0U;xcmd=EmmPort_Millis();}
  }else if(last.x< -VISION_TOLERANCE_PX){
   if(!xr||xd==X_DIRECTION){if(xr&&!stop_motor(X_ADDRESS))return 0U;if(!speed_motor(X_ADDRESS,(uint8_t)(1U-X_DIRECTION),X_SPEED_RPM))return 0U;xr=1U;xd=(uint8_t)(1U-X_DIRECTION);xcmd=EmmPort_Millis();}
   else if((uint32_t)(EmmPort_Millis()-xcmd)>=MOTOR_COMMAND_RETRY_MS){if(!speed_motor(X_ADDRESS,xd,X_SPEED_RPM))return 0U;xcmd=EmmPort_Millis();}
  }else if(xr&&!stop_motor(X_ADDRESS))return 0U;else xr=0U;
#if Y_AXIS_ENABLED
  if(last.y>VISION_TOLERANCE_PX){
   if(!yr||yd!=Y_DIRECTION){if(yr&&!stop_motor(Y_ADDRESS))return 0U;if(!speed_motor(Y_ADDRESS,Y_DIRECTION,Y_SPEED_RPM))return 0U;yr=1U;yd=Y_DIRECTION;ycmd=EmmPort_Millis();}
   else if((uint32_t)(EmmPort_Millis()-ycmd)>=MOTOR_COMMAND_RETRY_MS){if(!speed_motor(Y_ADDRESS,yd,Y_SPEED_RPM))return 0U;ycmd=EmmPort_Millis();}
  }else if(last.y< -VISION_TOLERANCE_PX){
   if(!yr||yd==Y_DIRECTION){if(yr&&!stop_motor(Y_ADDRESS))return 0U;if(!speed_motor(Y_ADDRESS,(uint8_t)(1U-Y_DIRECTION),Y_SPEED_RPM))return 0U;yr=1U;yd=(uint8_t)(1U-Y_DIRECTION);ycmd=EmmPort_Millis();}
   else if((uint32_t)(EmmPort_Millis()-ycmd)>=MOTOR_COMMAND_RETRY_MS){if(!speed_motor(Y_ADDRESS,yd,Y_SPEED_RPM))return 0U;ycmd=EmmPort_Millis();}
  }else if(yr&&!stop_motor(Y_ADDRESS))return 0U;else yr=0U;
#else
  yr=0U;(void)yd;
#endif
  if(!xr
#if Y_AXIS_ENABLED
     &&!yr
#endif
    ){
     /* 原XY到位停车路径不变；只关闭记录，后续Z阶段不继续积分。 */
     recording_travel=0U;
     return 1U;
    }
  if(!wait_ms(1U))return 0U;
 }
}
uint8_t MotionSequence_Init(void)
{
 uint8_t ok=1U;
#if TRAY_ENABLED && TRAY_BOOT_ZERO_ENABLED
 uint8_t clear_frame[4];
#endif
 AxisTravel zero={0};
 ready=0U;executed=0U;sequence_state=SEQUENCE_WAIT;
 boot_x_pending=BOOT_X_CLEARANCE_ENABLED;
 cycle_target_id=0U;completed_cycles=0U;used=discard_line=0U;
 recording_travel=0U;x_travel=zero;y_travel=zero;
 next_x_origin_offset=0;
 if(!stop_motor(X_ADDRESS))ok=0U;
#if Y_AXIS_ENABLED
 if(!stop_motor(Y_ADDRESS))ok=0U;
#endif
 if(!enable_motor(X_ADDRESS))ok=0U;
#if Y_AXIS_ENABLED
 if(!enable_motor(Y_ADDRESS))ok=0U;
#endif
#if TRAY_ENABLED
 /* 不转动托盘，只停车并使能；不能自动确定机械零点。 */
 if(!stop_motor(TRAY_ADDRESS))ok=0U;
 if(!enable_motor(TRAY_ADDRESS))ok=0U;
#endif
 if(!ok||!wait_ms(MOTOR_ENABLE_WAIT_MS))return fail_sequence();
#if TRAY_ENABLED && TRAY_BOOT_ZERO_ENABLED
 /* 清零仅上电执行一次，不保存/不校准，操作者先把1号位对准放置点。 */
 Emm_BuildClear(clear_frame,TRAY_ADDRESS);
 if(!EmmPort_Write(0U,clear_frame,4U))return fail_sequence();
#endif
 ready=1U;return 1U;
}
uint8_t MotionSequence_RunOnce(void)
{
 if(!ready||executed)return 0U;
 executed=1U;
 S20_Servo_SetAngle(CLAW_OPEN_DEG);
 if(!wait_ms(CLAW_OPEN_WAIT_MS))return fail_sequence();
 if(boot_x_pending){
  /* 仅上电首轮：复用放置后的X负向移动，期间丢弃视觉、保持Y停止。
   * 偏移计入首次回程，以原上电位置为放置点；PrepareNext不重新置此标志。 */
  if(!move_x_clearance())return fail_sequence();
  x_travel.net_rpm_ms=next_x_origin_offset;
  boot_x_pending=0U;
  discard_vision();Console_FlushRx();used=0U;discard_line=1U;
 }
 if(!follow_xy())return fail_sequence(); /* 保留原始XY到位停车逻辑 */
#if TRAY_ENABLED
 {
  uint16_t angle;
  /* 编号不在1..6时禁止继续抓取/放置到错误储物位，锁停等待人工处理。 */
  if(!tray_angle_for_id(cycle_target_id,&angle))return fail_sequence();
 }
#endif
 if(!grab_wait(500UL))return fail_sequence();
 /* XY退出后才准备Z；不改变XY跟随中的命令或停车时序。 */
 if(!stop_motor(Z_ADDRESS) || !enable_motor(Z_ADDRESS) ||
    !grab_wait(Z_ENABLE_SETTLE_MS))return fail_sequence();
 sequence_state=SEQUENCE_Z_DOWN;
 if(!move_z(Z_DOWN_DIRECTION)||!grab_wait(Z_BOTTOM_WAIT_MS))return fail_sequence();
 sequence_state=SEQUENCE_GRAB;
 S20_Servo_SetAngle(CLAW_GRAB_DEG);
 if(!grab_wait(CLAW_GRAB_WAIT_MS))return fail_sequence();
#if LIFT_AFTER_GRAB
 sequence_state=SEQUENCE_Z_UP;
 if(!move_z(Z_UP_DIRECTION))return fail_sequence();
#endif
#if RETURN_TO_START_ENABLED
 if(!return_xy_to_start())return fail_sequence();
#endif
#if PLACE_AFTER_RETURN_ENABLED
 #if TRAY_ENABLED
 /* XY已回程且Z在高位，再转托盘03，固定等待完后才下降放置。 */
 if(!position_tray())return fail_sequence();
 #endif
 /* 回程停车及500ms稳定等待完成后才放置；XY不再重新定位或启动。 */
 sequence_state=SEQUENCE_PLACE_DOWN;
 if(!move_z(Z_DOWN_DIRECTION)||!grab_wait(Z_BOTTOM_WAIT_MS))
  return fail_sequence();
 sequence_state=SEQUENCE_RELEASE;
 S20_Servo_SetAngle(CLAW_OPEN_DEG);
 if(!grab_wait(CLAW_RELEASE_WAIT_MS))return fail_sequence();
 sequence_state=SEQUENCE_PLACE_UP;
 if(!move_z(Z_UP_DIRECTION))return fail_sequence();
#endif
#if X_CLEARANCE_ENABLED
 if(!move_x_clearance())return fail_sequence();
#endif
 sequence_state=SEQUENCE_DONE;
 discard_vision();Console_FlushRx(); /* 完成前的坐标不能留作下轮目标 */
 /* 软件流程完成后通知视觉切换任务，不代表收到机械位置传感器反馈。 */
 if(!report_done(cycle_target_id))return fail_sequence();
 completed_cycles++;ready=0U;return 1U;
}
uint8_t MotionSequence_PrepareNext(void)
{
 AxisTravel zero={0};
 /* 故障/中止不得自动继续；只有完整完成放置、抬升的周期才能重装。 */
 if(sequence_state!=SEQUENCE_DONE || App_StopRequested())return 0U;
 if(!grab_wait(NEXT_CYCLE_GUARD_MS))return fail_sequence();
 discard_vision();Console_FlushRx();
 x_travel=zero;y_travel=zero;recording_travel=0U;
 x_travel.net_rpm_ms=next_x_origin_offset;
 cycle_target_id=0U;used=0U;discard_line=1U;
 /* 忽略下一段残缺/旧首行至LF，下一条完整新帧才开始定位。 */
 executed=0U;ready=1U;sequence_state=SEQUENCE_WAIT;
 return 1U;
}
