#ifndef __LCD_INIT_H
#define __LCD_INIT_H

#include "stm32f4xx.h"

/*====================== 显示方向选择 ======================
 0 或 1 ：竖屏 128 x 160
 2 或 3 ：横屏 160 x 128
 注意：方向和屏幕型号（ST7735S 的 0x36 寄存器）有关，
       如果显示上下或左右反了，把下面的数字在 0/1 之间换一下即可。
==========================================================*/
#define USE_HORIZONTAL 0

#if (USE_HORIZONTAL == 0) || (USE_HORIZONTAL == 1)
#define LCD_W 128
#define LCD_H 160
#else
#define LCD_W 160
#define LCD_H 128
#endif


/*====================== LCD 端口定义 ======================
 引脚接板载 TFT 显示屏接口 P2（8 针排针，2.54mm）：

   液晶屏引脚      P2 引脚 / 单片机      说明
   ------------------------------------------------------
   VCC            P2-1  3.3V           电源正（切勿接 5V）
   GND            P2-2  GND            电源地
   SCL / SCK      P2-3  PB13           时钟
   SDA / MOSI     P2-4  PB15           数据（单片机 -> 屏）
   CS             P2-5  PB12           片选（低电平有效）
   RES / RST      P2-6  PB14           复位（低电平复位）
   DC / RS        P2-7  PC5            命令/数据选择
   BLK            P2-8  PB1            背光控制（高电平点亮）

 驱动方式为软件模拟 SPI（4 线），所以这些脚必须是普通 GPIO，
 不能配置成 SPI 复用功能；如果以后要换成别的引脚，只要改下面
 的宏，同时把 lcd_init.c 中 LCD_GPIO_Init() 里使能的 GPIO
 时钟和引脚一起改掉即可。
==========================================================*/
#define LCD_MOSI_Clr() GPIO_ResetBits(GPIOB,GPIO_Pin_15)  //SDA=MOSI  PB15
#define LCD_MOSI_Set() GPIO_SetBits(GPIOB,GPIO_Pin_15)

#define LCD_SCLK_Clr() GPIO_ResetBits(GPIOB,GPIO_Pin_13)  //SCL=SCK   PB13
#define LCD_SCLK_Set() GPIO_SetBits(GPIOB,GPIO_Pin_13)

#define LCD_CS_Clr()   GPIO_ResetBits(GPIOB,GPIO_Pin_12)  //CS        PB12
#define LCD_CS_Set()   GPIO_SetBits(GPIOB,GPIO_Pin_12)

#define LCD_DC_Clr()   GPIO_ResetBits(GPIOC,GPIO_Pin_5)   //DC        PC5
#define LCD_DC_Set()   GPIO_SetBits(GPIOC,GPIO_Pin_5)

#define LCD_BLK_Clr()  GPIO_ResetBits(GPIOB,GPIO_Pin_1)   //BLK 背光  PB1
#define LCD_BLK_Set()  GPIO_SetBits(GPIOB,GPIO_Pin_1)

/*----------------- 复位引脚（RES） -----------------
 本工程把屏的 RES 接到 PB14，上电时由单片机做一次硬件复位
 （拉低 100ms 再拉高 100ms），这样上电最可靠。
 如果屏的 RES 已经接固定电平/内部复位，保持 1 也不会损坏电路，
 只是那次复位电平无效而已。
---------------------------------------------------*/
#define LCD_HAS_RES 1

#if (LCD_HAS_RES != 0)
#define LCD_RES_Clr()  GPIO_ResetBits(GPIOB,GPIO_Pin_14)  //RES 低电平
#define LCD_RES_Set()  GPIO_SetBits(GPIOB,GPIO_Pin_14)    //RES 高电平
#endif


/*====================== 函数声明 ======================*/
void LCD_GPIO_Init(void);                            //初始化GPIO
void LCD_Writ_Bus(u8 dat);                           //模拟SPI时序
void LCD_WR_DATA8(u8 dat);                           //写入一个字节
void LCD_WR_DATA(u16 dat);                           //写入两个字节
void LCD_WR_REG(u8 dat);                             //写入一个指令
void LCD_Address_Set(u16 x1, u16 y1, u16 x2, u16 y2);//设置坐标函数
void LCD_Init(void);                                 //LCD初始化

#endif
