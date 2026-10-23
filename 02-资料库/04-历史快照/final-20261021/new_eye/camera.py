import cv2
import time

class Camera:
    def __init__(self, index, width, height, fps):
        self.index, self.width, self.height, self.fps = index, width, height, fps
        self.cap = None

    def open(self):
        self.cap = cv2.VideoCapture(self.index, cv2.CAP_DSHOW)
        if not self.cap.isOpened():
            self.cap = cv2.VideoCapture(self.index, cv2.CAP_MSMF)
        if not self.cap.isOpened():
            raise RuntimeError(f"无法打开摄像头 index={self.index}")
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        self.cap.set(cv2.CAP_PROP_FPS, self.fps)
        for _ in range(5):
            self.cap.read()
            time.sleep(0.03)

    def read(self):
        if self.cap is None:
            self.open()
        ok, frame = self.cap.read()
        if not ok or frame is None:
            raise RuntimeError(f"摄像头读取失败 index={self.index}")
        return frame

    def close(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def __enter__(self):
        self.open(); return self

    def __exit__(self, *_):
        self.close()
