"""Manual proposals on a frozen frame; no inference or machine commands."""
import tkinter as tk
from tkinter import ttk
import cv2
import numpy as np


def select_boxes(parent, frame):
    window=tk.Toplevel(parent)
    window.title('框选完整物体（固定截图）')
    scale=min(1000/frame.shape[1],650/frame.shape[0],1)
    width,height=round(frame.shape[1]*scale),round(frame.shape[0]*scale)
    ttk.Label(window,text='左键拖动框住一件完整物体，重复添加；右键撤销。尽量不包含邻近物体。关闭窗口取消。').pack()
    canvas=tk.Canvas(window,width=width,height=height)
    canvas.pack()
    photo=tk.PhotoImage(data=cv2.imencode('.png',cv2.resize(frame,(width,height)))[1].tobytes())
    canvas.create_image(0,0,image=photo,anchor='nw')
    boxes=[]; drawings=[]; start=[]; temporary=[]; result=[]
    def point(event): return max(0,min(width,event.x)),max(0,min(height,event.y))
    def press(event):
        start[:]=point(event)
        temporary[:]=[canvas.create_rectangle(*start,*start,outline='#00ff80',width=2)]
    def drag(event):
        if temporary: canvas.coords(temporary[0],*start,*point(event))
    def release(event):
        if not temporary: return
        ex,ey=point(event);sx,sy=start
        x,y=int(min(sx,ex)/scale),int(min(sy,ey)/scale)
        right,bottom=min(frame.shape[1],round(max(sx,ex)/scale)),min(frame.shape[0],round(max(sy,ey)/scale))
        if right-x<10 or bottom-y<10 or len(boxes)>=20:
            canvas.delete(temporary.pop());return
        boxes.append([x,y,right-x,bottom-y])
        label=canvas.create_text(min(sx,ex)+4,min(sy,ey)+4,text=str(len(boxes)),anchor='nw',fill='#00ff80')
        drawings.append((temporary.pop(),label))
    def undo(event=None):
        if boxes:
            boxes.pop()
            for item in drawings.pop():canvas.delete(item)
    def accept():
        result.extend(boxes);window.destroy()
    canvas.bind('<ButtonPress-1>',press);canvas.bind('<B1-Motion>',drag)
    canvas.bind('<ButtonRelease-1>',release);canvas.bind('<ButtonPress-3>',undo)
    ttk.Button(window,text='按这些框逐件识别',command=accept).pack(pady=6)
    window.transient(parent);window.grab_set();parent.wait_window(window)
    return result


def candidates_from_boxes(boxes):
    return [dict(object_id=i,bbox=[x,y,w,h],center_px=[x+(w-1)/2,y+(h-1)/2],
                 area=w*h,touches_roi=False,proposal_source='manual',requires_review=True,
                 contour=np.array([[[x,y]],[[x+w-1,y]],[[x+w-1,y+h-1]],[[x,y+h-1]]],np.int32))
            for i,(x,y,w,h) in enumerate(boxes,1)]
