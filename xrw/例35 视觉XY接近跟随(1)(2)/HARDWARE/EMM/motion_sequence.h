#ifndef MOTION_SEQUENCE_H
#define MOTION_SEQUENCE_H
#include <stdint.h>
typedef enum {
    SEQUENCE_WAIT=0,SEQUENCE_XY,SEQUENCE_Z_DOWN,SEQUENCE_GRAB,
    SEQUENCE_Z_UP,SEQUENCE_DONE,SEQUENCE_FAULT,SEQUENCE_ABORT,
    SEQUENCE_RETURN, /* 保留原有状态编号，回程状态8 */
    SEQUENCE_PLACE_DOWN,SEQUENCE_RELEASE,SEQUENCE_PLACE_UP,
    SEQUENCE_X_CLEARANCE,SEQUENCE_TRAY_POSITION
} SequenceState;
extern volatile uint8_t sequence_state;
extern volatile uint8_t cycle_target_id;
extern volatile uint32_t completed_cycles;
uint8_t MotionSequence_Init(void);
uint8_t MotionSequence_RunOnce(void);
uint8_t MotionSequence_PrepareNext(void);
#endif
