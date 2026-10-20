"""Offline YOLO + Qwen benchmark on saved photos; never uses serial ports."""
import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np
from world_detector import WorldDetector, ROOT
from object_segmentation import annotate, object_crop
from recognize import recognize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('--recognize', action='store_true')
    args = parser.parse_args()
    frame = cv2.imdecode(np.fromfile(args.image, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        parser.error('Cannot read image')
    folder = ROOT/'samples/yolo-validation'
    folder.mkdir(parents=True, exist_ok=True)
    detector = WorldDetector()
    trials = []
    for _ in range(3):
        objects, _, timing = detector.detect(frame)
        trials.append(timing)
        print(timing, flush=True)
    cv2.imencode('.jpg',annotate(frame,objects))[1].tofile(str(folder/'detected.jpg'))
    results = []
    if args.recognize:
        for obj in objects:
            target = folder/f"crop-{obj['object_id']}.jpg"
            cv2.imencode('.jpg',object_crop(frame,obj,max_side=640))[1].tofile(str(target))
            record = recognize(target)
            results.append(record)
            print(json.dumps(record,ensure_ascii=False),flush=True)
    result = dict(image=str(args.image.resolve()), input_source='saved_image', trials=trials,
                  objects=[{k:v for k,v in obj.items() if k!='contour'} for obj in objects],
                  recognitions=results)
    (folder/'benchmark.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__ == '__main__':
    main()
