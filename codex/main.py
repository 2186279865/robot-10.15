from __future__ import annotations

import argparse
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Use an ASCII writable config folder before importing optional Ultralytics.
os.environ.setdefault('YOLO_CONFIG_DIR', str(Path(os.environ.get('TEMP', str(ROOT / 'runtime'))) / 'robot-codex-yolo'))


def main():
    if os.name == 'nt':
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description='机器人视觉与控制工作台')
    parser.add_argument('--demo', action='store_true', help='内置图像和模拟机器人')
    parser.add_argument('--image', help='离线顶视图片')
    parser.add_argument('--side', help='离线副视图片')
    parser.add_argument('--backend', choices=('geometry', 'local', 'cloud'))
    parser.add_argument('--segmentation', choices=('contour', 'sam', 'sam_auto'))
    parser.add_argument('--benchmark', help='带人工真值的 labels.json')
    parser.add_argument('--backends', default='geometry,local,cloud')
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--output', default=str(ROOT / 'runtime/benchmark.json'))
    parser.add_argument('--display', action='store_true', help='派端结果显示屏')
    parser.add_argument('--display-port', type=int, default=5059)
    parser.add_argument('--windowed', action='store_true')
    parser.add_argument('--smoke', action='store_true', help='模拟界面截图后退出')
    args = parser.parse_args()
    if args.display:
        from workbench.display import run_display
        run_display(args.display_port, args.windowed)
        return
    from workbench.settings import load
    cfg = load()
    cfg['robot']['reference_confirmed'] = False
    if args.backend:
        cfg['vision']['backend'] = args.backend
    if args.segmentation:
        cfg['vision']['segmentation'] = args.segmentation
    if args.benchmark:
        from workbench.dataset import benchmark, write_report
        backends = [b.strip() for b in args.backends.split(',') if b.strip()]
        if args.repeats < 1 or not backends or any(b not in ('geometry', 'local', 'cloud') for b in backends):
            parser.error('评测需要有效后端 geometry,local,cloud 和至少一次 repeats')
        print(write_report(benchmark(cfg, args.benchmark, backends, args.repeats), args.output))
        return
    from workbench.app import App
    App(cfg, args).mainloop()


if __name__ == '__main__':
    main()
