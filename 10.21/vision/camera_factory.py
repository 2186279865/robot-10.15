"""相机工厂：按环境变量 / config.json 决定用「本地 USB 相机」还是「派上远程相机」。

默认行为完全不变：没有 `ROBOT_PI` 环境变量、`config.json` 里也没有 `remote.host`
时，返回原来的 `vision/camera.py::Camera`。要连树莓派时只改配置或环境变量，
GUI 代码不用动。

判定顺序（先到先得）：
1. 环境变量 `ROBOT_PI` 非空（`main.py --pi HOST` 会设置它）→ 远程；
2. `vision/config.json` 的 `remote.host` 非空 → 远程；
3. 否则 → 本地 `Camera`。

远程端口：`ROBOT_PI_IMG_PORT` 环境变量 > `ROBOT_PI` 里的 `host:port` 写法 >
`config.json` 的 `remote.img_port` > `wire.IMG_PORT`（5057）。

`config.json` 示例（只有需要远程时加）::

    {"remote": {"host": "192.168.137.50", "img_port": 5057}}
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # 10.21/vision
CONFIG = HERE / 'config.json'
_COMMON = HERE.parent / 'common'

for _p in (str(HERE), str(_COMMON)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from camera import Camera  # noqa: E402

#: 与 common/wire.py 的 IMG_PORT 一致；这里留个字面量兜底，避免循环导入。
DEFAULT_IMG_PORT = 5057


def _load_config() -> dict:
    """读 config.json；不存在/坏文件/不是 dict 一律视为「没有配置」。"""
    try:
        data = json.loads(CONFIG.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _wire_img_port() -> int:
    try:
        import wire  # type: ignore
        return int(wire.IMG_PORT)
    except Exception:  # noqa: BLE001
        return DEFAULT_IMG_PORT


def _split_host_port(text: str):
    """`192.168.137.50` 或 `192.168.137.50:5057` → (host, port|None)。"""
    text = (text or '').strip()
    if not text:
        return '', None
    host, sep, raw = text.rpartition(':')
    if sep and host and raw.isdigit():
        return host, int(raw)
    return text, None


def _parse_cam_map(raw) -> dict:
    """`{"1": 0, "2": 1}` 或 `"1:0,2:1"` → `{1: 0, 2: 1}`；坏值忽略。

    这是"本机相机编号 → 派端相机槽位"的映射：Windows 上 `top_camera_index`
    是设备号（可能是 1/2），而派端 `pi_agent.py` 的槽位总是从 0 开始的
    出现顺序（`--cam-index 0 --cam-index 2` → 槽位 0、1）。
    """
    out = {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, str):
        items = [p.split(':', 1) for p in raw.split(',') if ':' in p]
    else:
        return out
    for key, value in items:
        try:
            out[int(str(key).strip())] = int(str(value).strip())
        except (TypeError, ValueError):
            continue
    return out


def cam_map() -> dict:
    """当前生效的相机编号映射（环境变量 `ROBOT_PI_CAM_MAP` 优先于 config.json）。"""
    env = os.environ.get('ROBOT_PI_CAM_MAP', '').strip()
    if env:
        parsed = _parse_cam_map(env)
        if parsed:
            return parsed
    cfg = _load_config()
    remote = cfg.get('remote') if isinstance(cfg.get('remote'), dict) else {}
    return _parse_cam_map(remote.get('cam_map'))


def remote_settings() -> dict:
    """返回当前的远程相机设置：`{'host': str, 'img_port': int}`；host 空表示本地。"""
    cfg = _load_config()
    remote = cfg.get('remote') if isinstance(cfg.get('remote'), dict) else {}
    env = os.environ.get('ROBOT_PI', '').strip()
    host, env_port = _split_host_port(env)
    if not host:
        host, env_port = _split_host_port(str(remote.get('host') or ''))
    port = None
    raw_port = os.environ.get('ROBOT_PI_IMG_PORT', '').strip()
    if raw_port:
        try:
            port = int(raw_port)
        except ValueError:
            port = None
    if port is None:
        port = env_port
    if port is None:
        try:
            port = int(remote.get('img_port'))
        except (TypeError, ValueError):
            port = None
    if port is None:
        port = _wire_img_port()
    return {'host': host, 'img_port': int(port)}


def make_remote(index, width, height, fps, host=None, img_port=None, cam_map_=None):
    """显式构造远程相机（测试/工具可直接用）。"""
    from remote_camera import RemoteCamera

    settings = remote_settings()
    host = settings['host'] if not host else host
    port = settings['img_port'] if img_port is None else int(img_port)
    if not host:
        raise ValueError('没有指定远程相机主机（ROBOT_PI / config.json remote.host）')
    mapping = cam_map() if cam_map_ is None else cam_map_
    cam_id = int(mapping.get(int(index), index))
    return RemoteCamera(cam_id, width, height, fps, host, port)


def make_camera(index, width, height, fps):
    """GUI 唯一入口：有远程配置就用 RemoteCamera，否则用本地 Camera。"""
    settings = remote_settings()
    host = settings['host']
    if host:
        from remote_camera import RemoteCamera
        cam_id = int(cam_map().get(int(index), index))
        return RemoteCamera(cam_id, width, height, fps, host, settings['img_port'])
    return Camera(index, width, height, fps)


def is_remote() -> bool:
    return bool(remote_settings()['host'])


if __name__ == '__main__':  # 便于现场快速确认当前会用哪路相机
    settings = remote_settings()
    print('远程相机：' + (f"{settings['host']}:{settings['img_port']}"
                         if settings['host'] else '否（用本地 USB 相机）'))
