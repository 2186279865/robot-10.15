import argparse, json, time
from pathlib import Path
import cv2
from camera import Camera
from vision import segment, vote

ROOT = Path(__file__).resolve().parent

def load_config(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))

def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

def capture_batch(top, side, cfg, preview=False):
    frames, side_frames = [], []
    count = int(cfg.get("frames_for_vote", 5))
    for _ in range(count):
        frames.append(top.read())
        side_frames.append(side.read())
        time.sleep(0.04)
    if preview:
        cv2.imshow("new_eye top", frames[-1])
        cv2.imshow("new_eye side", side_frames[-1])
        cv2.waitKey(1)
    return frames, side_frames

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--top-index", type=int)
    ap.add_argument("--side-index", type=int)
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--once", action="store_true", help="采集一次后退出")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.top_index is not None: cfg["top_camera_index"] = args.top_index
    if args.side_index is not None: cfg["side_camera_index"] = args.side_index
    out = ROOT / cfg.get("output_dir", "runtime")
    out.mkdir(exist_ok=True)
    top = Camera(cfg["top_camera_index"], cfg["width"], cfg["height"], cfg["fps"])
    side = Camera(cfg["side_camera_index"], cfg["width"], cfg["height"], cfg["fps"])
    try:
        top.open(); side.open()
        while True:
            frames, side_frames = capture_batch(top, side, cfg, args.preview)
            detections = [segment(f, cfg) for f in frames]
            targets = vote(detections)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            (out / "captures").mkdir(exist_ok=True)
            cv2.imwrite(str(out / "captures" / f"top-{stamp}.jpg"), frames[-1])
            payload = {"schema_version": 1, "captured_at": time.time(),
                       "motion_authorized": False, "calibration_status": "missing",
                       "recognition_before_grab": True, "targets": targets,
                       "side_camera_used": True,
                       "note": "抓取前完成识别；抓取后不再依赖视觉识别"}
            save_json(out / "latest_targets.json", payload)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            if args.once: break
            if cv2.waitKey(1) & 0xff == 27: break
            time.sleep(.2)
    finally:
        top.close(); side.close(); cv2.destroyAllWindows()

if __name__ == "__main__": main()
