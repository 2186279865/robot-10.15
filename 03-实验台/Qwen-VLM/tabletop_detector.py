"""Color-separated foreground proposals; neutral objects need a reference."""
import cv2
import numpy as np
from object_segmentation import region_mask, separate_objects, split_touching


def detect_tabletop(frame, polygon=None, background=None, threshold=30, min_area=.3):
    allowed = region_mask(frame.shape, polygon)
    inner = cv2.erode(allowed, np.ones((3,3),np.uint8), borderType=cv2.BORDER_CONSTANT, borderValue=0)
    hsv = cv2.cvtColor(cv2.GaussianBlur(frame,(3,3),0),cv2.COLOR_BGR2HSV)
    hue,saturation,value = cv2.split(hsv)
    foreground = inner
    if background is not None:
        _, foreground = separate_objects(frame,polygon,background,threshold,min_area)
    masks = []
    # Keep unlike touching colors separate, without guessing their names/shapes.
    chromatic = (saturation >= max(45,threshold)) & (value >= 45)
    for low,high in [(0,12),(12,30),(30,90),(90,135),(135,170)]:
        band = (hue >= low) & (hue < high)
        if low == 0: band |= hue >= 170
        masks.append(np.uint8(band & chromatic)*255)
    if background is not None:
        masks.append(np.uint8(~chromatic)*255)
    objects=[]
    combined=np.zeros(frame.shape[:2],np.uint8)
    cutoff=max(80,cv2.countNonZero(allowed)*min_area/100)
    for mask in masks:
        mask=cv2.bitwise_and(mask,foreground)
        mask=cv2.morphologyEx(mask,cv2.MORPH_CLOSE,np.ones((9,9),np.uint8))
        mask=cv2.morphologyEx(mask,cv2.MORPH_OPEN,np.ones((3,3),np.uint8))
        mask=cv2.bitwise_and(mask,inner)
        mask=split_touching(mask)
        contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            area=cv2.contourArea(contour)
            if area < cutoff: continue
            x,y,w,h=cv2.boundingRect(contour)
            if w<10 or h<10: continue
            # Filled external contour retains printed labels/white faces inside.
            cv2.drawContours(combined,[contour],-1,255,cv2.FILLED)
            contact=np.zeros_like(mask)
            cv2.drawContours(contact,[contour],-1,255,3)
            edge=cv2.subtract(allowed,cv2.erode(allowed,np.ones((7,7),np.uint8),borderType=cv2.BORDER_CONSTANT,borderValue=0))
            objects.append(dict(bbox=[x,y,w,h],contour=contour,area=round(area,1),
                center_px=[x+(w-1)/2,y+(h-1)/2],
                touches_roi=bool(cv2.countNonZero(cv2.bitwise_and(contact,edge))),
                proposal_source='tabletop',requires_review=True))
    if len(objects)>20:
        raise ValueError('前景候选超过20个，请缩小选区或提高最小面积。')
    objects.sort(key=lambda o:o['bbox'][:2])
    for index,obj in enumerate(objects,1): obj['object_id']=index
    return objects,combined
