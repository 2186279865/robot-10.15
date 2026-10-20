/***
***************************************************************************
*  @file    lcd_init.c
*  @brief   ST7735S 液晶屏底层驱动（软件模拟 SPI）
*  @note    移植自厂家 1.8寸/1.77寸 ST7735S 128x160 例程，
*           仅把 delay_ms() 改成 10.15 工程里的 Delay_ms()。
*           引脚已改为板载 TFT 接口 P2：
*             SCL=PB13  SDA=PB15  CS=PB12  RES=PB14  DC=PC5  BLK=PB1
*  实验平台：鹿小班 STM32F407ZGT6 核心板（LXB407ZG-P1）
***************************************************************************
***/

#include "lcd_init.h"
#include "delay.h"


/******************************************************************************
      函数说明：LCD GPIO 初始化
      引脚：SDA=PB15  SCL=PB13  CS=PB12  RES=PB14  BLK=PB1  DC=PC5
******************************************************************************/
void LCD_GPIO_Init(void)
{
	GPIO_InitTypeDef  GPIO_InitStructure;

	/* 同时使能 GPIOB（SDA/SCL/CS/RES/BLK）和 GPIOC（DC）时钟 */
	RCC_AHB1PeriphClockCmd(RCC_AHB1Periph_GPIOB | RCC_AHB1Periph_GPIOC, ENABLE);

	/* GPIOB：PB1(BLK) PB12(CS) PB13(SCL) PB14(RES) PB15(SDA) 推挽输出 */
	GPIO_InitStructure.GPIO_Pin = GPIO_Pin_1 | GPIO_Pin_12 | GPIO_Pin_13 | GPIO_Pin_14 | GPIO_Pin_15;
	GPIO_InitStructure.GPIO_Mode = GPIO_Mode_OUT;      //普通输出模式
	GPIO_InitStructure.GPIO_OType = GPIO_OType_PP;     //推挽输出
	GPIO_InitStructure.GPIO_Speed = GPIO_Speed_100MHz;//100MHz
	GPIO_InitStructure.GPIO_PuPd = GPIO_PuPd_UP;       //上拉
	GPIO_Init(GPIOB, &GPIO_InitStructure);

	/* GPIOC：PC5(DC) 推挽输出 */
	GPIO_InitStructure.GPIO_Pin = GPIO_Pin_5;
	GPIO_Init(GPIOC, &GPIO_InitStructure);

	/* 初始化后的安全电平：CS/DC/RES 高，SCL 低，背光先关 */
	LCD_CS_Set();
	LCD_DC_Set();
	LCD_RES_Set();
	LCD_SCLK_Clr();
	LCD_BLK_Clr();
}


/******************************************************************************
      函数说明：LCD串行数据写入函数
      入口数据：dat  要写入的串行数据
      返回值：  无
******************************************************************************/
void LCD_Writ_Bus(u8 dat)
{
	u8 i;
	LCD_CS_Clr();
	for(i = 0; i < 8; i++)
	{
		LCD_SCLK_Clr();
		if(dat & 0x80)
		{
			LCD_MOSI_Set();
		}
		else
		{
			LCD_MOSI_Clr();
		}
		LCD_SCLK_Set();
		dat <<= 1;
	}
	LCD_CS_Set();
}


/******************************************************************************
      函数说明：LCD写入数据
      入口数据：dat 写入的数据
      返回值：  无
******************************************************************************/
void LCD_WR_DATA8(u8 dat)
{
	LCD_Writ_Bus(dat);
}


/******************************************************************************
      函数说明：LCD写入两个字节数据
      入口数据：dat 写入的数据（高字节在前）
      返回值：  无
******************************************************************************/
void LCD_WR_DATA(u16 dat)
{
	LCD_Writ_Bus(dat >> 8);
	LCD_Writ_Bus(dat);
}


/******************************************************************************
      函数说明：LCD写入命令
      入口数据：dat 写入的命令
      返回值：  无
******************************************************************************/
void LCD_WR_REG(u8 dat)
{
	LCD_DC_Clr();//写命令
	LCD_Writ_Bus(dat);
	LCD_DC_Set();//写数据
}


/******************************************************************************
      函数说明：设置起始和结束地址
      入口数据：x1,x2 设置列的起始和结束地址
                y1,y2 设置行的起始和结束地址
      返回值：  无
******************************************************************************/
void LCD_Address_Set(u16 x1, u16 y1, u16 x2, u16 y2)
{
	if(USE_HORIZONTAL == 0)
	{
		LCD_WR_REG(0x2a);//列地址设置
		LCD_WR_DATA(x1);
		LCD_WR_DATA(x2);
		LCD_WR_REG(0x2b);//行地址设置
		LCD_WR_DATA(y1);
		LCD_WR_DATA(y2);
		LCD_WR_REG(0x2c);//储存器写
	}
	else if(USE_HORIZONTAL == 1)
	{
		LCD_WR_REG(0x2a);//列地址设置
		LCD_WR_DATA(x1);
		LCD_WR_DATA(x2);
		LCD_WR_REG(0x2b);//行地址设置
		LCD_WR_DATA(y1);
		LCD_WR_DATA(y2);
		LCD_WR_REG(0x2c);//储存器写
	}
	else if(USE_HORIZONTAL == 2)
	{
		LCD_WR_REG(0x2a);//列地址设置
		LCD_WR_DATA(x1);
		LCD_WR_DATA(x2);
		LCD_WR_REG(0x2b);//行地址设置
		LCD_WR_DATA(y2);
		LCD_WR_DATA(y1);
		LCD_WR_REG(0x2c);//储存器写
	}
	else
	{
		LCD_WR_REG(0x2a);//列地址设置
		LCD_WR_DATA(x1);
		LCD_WR_DATA(x2);
		LCD_WR_REG(0x2b);//行地址设置
		LCD_WR_DATA(y1);
		LCD_WR_DATA(y2);
		LCD_WR_REG(0x2c);//储存器写
	}
}


/******************************************************************************
      函数说明：LCD初始化（ST7735S）
******************************************************************************/
void LCD_Init(void)
{
	LCD_GPIO_Init();//初始化GPIO

#if (LCD_HAS_RES != 0)
	/* 硬件复位：RES 拉低 100ms 再拉高 100ms */
	LCD_RES_Clr();
	Delay_ms(100);
	LCD_RES_Set();
	Delay_ms(100);
#endif

	LCD_BLK_Set();//打开背光
	Delay_ms(100);

	//************* Start Initial Sequence **********//
	LCD_WR_REG(0x11); //Sleep out
	Delay_ms(120);              //Delay 120ms
	//------------------------------------ST7735S Frame Rate-----------------------------------------//
	LCD_WR_REG(0xB1);
	LCD_WR_DATA8(0x05);
	LCD_WR_DATA8(0x3C);
	LCD_WR_DATA8(0x3C);
	LCD_WR_REG(0xB2);
	LCD_WR_DATA8(0x05);
	LCD_WR_DATA8(0x3C);
	LCD_WR_DATA8(0x3C);
	LCD_WR_REG(0xB3);
	LCD_WR_DATA8(0x05);
	LCD_WR_DATA8(0x3C);
	LCD_WR_DATA8(0x3C);
	LCD_WR_DATA8(0x05);
	LCD_WR_DATA8(0x3C);
	LCD_WR_DATA8(0x3C);
	//------------------------------------End ST7735S Frame Rate---------------------------------//
	LCD_WR_REG(0xB4); //Dot inversion
	LCD_WR_DATA8(0x03);
	//------------------------------------ST7735S Power Sequence---------------------------------//
	LCD_WR_REG(0xC0);
	LCD_WR_DATA8(0x28);
	LCD_WR_DATA8(0x08);
	LCD_WR_DATA8(0x04);
	LCD_WR_REG(0xC1);
	LCD_WR_DATA8(0XC0);
	LCD_WR_REG(0xC2);
	LCD_WR_DATA8(0x0D);
	LCD_WR_DATA8(0x00);
	LCD_WR_REG(0xC3);
	LCD_WR_DATA8(0x8D);
	LCD_WR_DATA8(0x2A);
	LCD_WR_REG(0xC4);
	LCD_WR_DATA8(0x8D);
	LCD_WR_DATA8(0xEE);
	//---------------------------------End ST7735S Power Sequence-------------------------------------//
	LCD_WR_REG(0xC5); //VCOM
	LCD_WR_DATA8(0x1A);
	LCD_WR_REG(0x36); //MX, MY, RGB mode
	if(USE_HORIZONTAL == 0)LCD_WR_DATA8(0x00);
	else if(USE_HORIZONTAL == 1)LCD_WR_DATA8(0xC0);
	else if(USE_HORIZONTAL == 2)LCD_WR_DATA8(0x70);
	else LCD_WR_DATA8(0xA0);
	//------------------------------------ST7735S Gamma Sequence---------------------------------//
	LCD_WR_REG(0xE0);
	LCD_WR_DATA8(0x04);
	LCD_WR_DATA8(0x22);
	LCD_WR_DATA8(0x07);
	LCD_WR_DATA8(0x0A);
	LCD_WR_DATA8(0x2E);
	LCD_WR_DATA8(0x30);
	LCD_WR_DATA8(0x25);
	LCD_WR_DATA8(0x2A);
	LCD_WR_DATA8(0x28);
	LCD_WR_DATA8(0x26);
	LCD_WR_DATA8(0x2E);
	LCD_WR_DATA8(0x3A);
	LCD_WR_DATA8(0x00);
	LCD_WR_DATA8(0x01);
	LCD_WR_DATA8(0x03);
	LCD_WR_DATA8(0x13);
	LCD_WR_REG(0xE1);
	LCD_WR_DATA8(0x04);
	LCD_WR_DATA8(0x16);
	LCD_WR_DATA8(0x06);
	LCD_WR_DATA8(0x0D);
	LCD_WR_DATA8(0x2D);
	LCD_WR_DATA8(0x26);
	LCD_WR_DATA8(0x23);
	LCD_WR_DATA8(0x27);
	LCD_WR_DATA8(0x27);
	LCD_WR_DATA8(0x25);
	LCD_WR_DATA8(0x2D);
	LCD_WR_DATA8(0x3B);
	LCD_WR_DATA8(0x00);
	LCD_WR_DATA8(0x01);
	LCD_WR_DATA8(0x04);
	LCD_WR_DATA8(0x13);
	//------------------------------------End ST7735S Gamma Sequence-----------------------------//
	LCD_WR_REG(0x3A); //65k mode
	LCD_WR_DATA8(0x05);
	LCD_WR_REG(0x29); //Display on
}
