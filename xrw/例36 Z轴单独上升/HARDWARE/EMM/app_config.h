#ifndef APP_CONFIG_H
#define APP_CONFIG_H

/* 复用例32 PA2/USART2发送方式，不改驱动协议或存储参数。 */
#define EMM_CHANNEL_COUNT 1U
#define EMM_TX_ONLY 1U
#define EMM_BAUD 115200UL
#define PHYSICAL_ESTOP_ENABLED 0
#define X_ADDRESS 4U
#define Y_ADDRESS 5U
#define Z_ADDRESS 1U
#define X_AXIS_ENABLED 0U
#define Y_AXIS_ENABLED 0U

/* ===== XY平移，单位RPM/毫秒，1000ms=1s ===== */
#define X_SPEED_RPM 34U
#define X_MOVE_MS 0UL
#define X_DIRECTION 0U
#define Y_SPEED_RPM 150U
#define Y_MOVE_MS 0UL
#define Y_DIRECTION 0U /* 已按实测反向：视觉Y正偏差使用方向00 */
#define MOTOR_ENABLE_WAIT_MS 500UL
#define VISION_TARGET_ID 0U
#define VISION_TOLERANCE_PX 8
#define VISION_TIMEOUT_MS 600UL
#define VISION_MAX_SESSION_MS 0UL
#define MOTOR_COMMAND_RETRY_MS 250UL /* 偏差持续存在时重发F6，防止首帧丢失 */
#define XY_ARRIVAL_WAIT_MS 500UL /* XY到位且停车后，等待0.5秒才开始Z下降 */
#define XY_STOP_REPEAT_COUNT 3U /* 到位后重复FE，降低首帧停车丢失概率 */
#define XY_STOP_REPEAT_GAP_MS 20UL

/* ===== Z与爪子：升降抓取方式沿用例32 ===== */
#define Z_SPEED_RPM 300U /* 带载低速上升测试，不修改电流 */
#define Z_ACCEL 128U /* 缓启动档位，与之前的带载版一致 */
#define Z_PULSES_PER_REV 3200UL /* 1.8度电机16细分；32细分改6400 */
#define Z_TRAVEL_TURNS 20UL /* 一次按键：上升20圈，再下降20圈 */
#define Z_TRAVEL_PULSES (Z_PULSES_PER_REV*Z_TRAVEL_TURNS)
#define Z_POSITION_WAIT_MS 7000UL /* FD每段留7秒完成时间；不读取驱动反馈 */
#define KEY_DEBOUNCE_MS 20UL
#define Z_DOWN_DIRECTION 0U
#define Z_UP_DIRECTION 1U /* 当前CCW向上约定 */
#define Z_BOTTOM_WAIT_MS 500UL
#define CLAW_OPEN_DEG 140U /* 已与原抓紧角度对调 */
#define CLAW_OPEN_WAIT_MS 1000UL
#define CLAW_GRAB_DEG 20U /* 已与原松爪角度对调 */
#define CLAW_GRAB_WAIT_MS 3000UL
/* 例32抓取后抬升；若只验证下降抓紧，可设0，Z停在下方保持。 */
#define LIFT_AFTER_GRAB 1U

#if (X_AXIS_ENABLED && X_SPEED_RPM==0) || (Y_AXIS_ENABLED && Y_SPEED_RPM==0)
#error Speed_must_not_be_zero
#endif
#if (X_AXIS_ENABLED && X_DIRECTION>1) || (Y_AXIS_ENABLED && Y_DIRECTION>1) || Z_DOWN_DIRECTION>1 || Z_UP_DIRECTION>1
#error Invalid_motor_direction
#endif
#if Y_AXIS_ENABLED>1
#error Y_axis_enabled_must_be_0_or_1
#endif
#if X_AXIS_ENABLED>1
#error X_axis_enabled_must_be_0_or_1
#endif
#if Z_SPEED_RPM==0 || Z_ACCEL>255 || Z_POSITION_WAIT_MS==0 || \
    Z_TRAVEL_TURNS==0 || Z_PULSES_PER_REV==0 || Z_TRAVEL_PULSES>2147483647UL
#error Invalid_Z_load_test_parameters
#endif
#endif
