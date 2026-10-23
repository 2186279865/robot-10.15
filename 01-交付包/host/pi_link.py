"""把「树莓派/香橙派」当成一个可选端口：UDP 发现 + 记住上次 IP。

上位机的端口下拉框里原本只有串口（COMx）。这个模块让它多出几行「派」条目：

    派 robot-pi 192.168.100.198（图传 5057 / 串口 5058）

选中它点「连接」，`host_gui` 就会：
  1. `apply_env()` → 设 `ROBOT_PI` / `ROBOT_PI_IMG_PORT` / `ROBOT_PI_SER_PORT`，
     之后视觉页创建相机时自动走 `RemoteCamera`（见 `vision/camera_factory.py`）；
  2. 用 `tcp://192.168.100.198:5058` 连派端串口桥（见 `serial_link.SocketLink`）。

发现失败也能用：`load_last()` 会把上一次连通的 IP 也列出来（比赛现场常有 AP 隔离，
UDP 广播收不到，但直连 IP 是通的）。

协议在 `10.21/common/wire.py`：UDP 5056，发 `ROBOTPI?`，回 `ROBOTPI!ip|img|ser|name`。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

HERE = Path(__file__).resolve().parent          # 10.21/host
PKG_ROOT = HERE.parent                          # 10.21
COMMON = PKG_ROOT / 'common'
RUNTIME = PKG_ROOT / 'runtime'
LAST_FILE = RUNTIME / 'pi_last.json'

for _p in (str(COMMON), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:                                            # 正常情况下都能 import 到
    import wire                                  # type: ignore
    DISCOVERY_PORT = int(wire.DISCOVERY_PORT)
    DISCOVERY_PROBE = bytes(wire.DISCOVERY_PROBE)
    DEFAULT_IMG_PORT = int(wire.IMG_PORT)
    DEFAULT_SER_PORT = int(wire.SER_PORT)
    _parse_reply = wire.parse_discovery_reply
except Exception:                                # pragma: no cover - common/ 缺失时兜底
    DISCOVERY_PORT = 5056
    DISCOVERY_PROBE = b'ROBOTPI?'
    DEFAULT_IMG_PORT = 5057
    DEFAULT_SER_PORT = 5058
    _parse_reply = None


# ---------------------------------------------------------------------- 数据
@dataclass
class PiEndpoint:
    """一台派（或任何跑着 pi_agent.py 的机器）。"""

    host: str
    name: str = 'robot-pi'
    img_port: int = DEFAULT_IMG_PORT
    ser_port: int = DEFAULT_SER_PORT
    source: str = '发现'                          # 「发现」/「上次」

    # ---- 给 host_gui 当 PortInfo 用（同 label()/device 两个方法）
    def label(self) -> str:
        return '派 %s %s（图传 %d / 串口 %d）' % (
            self.name, self.host, int(self.img_port), int(self.ser_port))

    @property
    def device(self) -> str:
        """直接喂给 `open_best_link()`。"""
        return 'tcp://%s:%d' % (self.host, int(self.ser_port))

    def to_json(self) -> dict:
        return {'host': self.host, 'name': self.name,
                'img_port': int(self.img_port), 'ser_port': int(self.ser_port)}


def endpoint_from_text(text: str) -> Optional[PiEndpoint]:
    """`tcp://IP:5058` → PiEndpoint；不是 TCP 端点就返回 None。

    用户手打 tcp:// 时也得把视觉切到远程，否则会出现「控制连派、相机还在本机」。
    """
    text = (text or '').strip()
    if not text.lower().startswith('tcp://'):
        return None
    body = text[6:].strip('/')
    host, _, raw = body.partition(':')
    if not host:
        return None
    try:
        ser = int(raw) if raw else DEFAULT_SER_PORT
    except ValueError:
        ser = DEFAULT_SER_PORT
    return PiEndpoint(host=host, ser_port=ser, source='手填')


# ---------------------------------------------------------------------- 发现
def _parse(data: bytes) -> Optional[PiEndpoint]:
    if _parse_reply is not None:
        try:
            info = _parse_reply(data)
        except Exception:                        # noqa: BLE001 - 不是我们的包
            return None
        return PiEndpoint(host=str(info.get('ip') or ''),
                          name=str(info.get('name') or 'robot-pi'),
                          img_port=int(info.get('img_port') or DEFAULT_IMG_PORT),
                          ser_port=int(info.get('ser_port') or DEFAULT_SER_PORT))
    return None


def discover(timeout: float = 1.2) -> List[PiEndpoint]:
    """广播找派。收不到就返回空表（不抛异常，界面不该因为网络卡住）。"""
    found = {}
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.25)
    except OSError:                              # pragma: no cover
        return []
    try:
        deadline = time.time() + max(0.0, float(timeout))
        while time.time() < deadline:
            try:
                sock.sendto(DISCOVERY_PROBE, ('255.255.255.255', DISCOVERY_PORT))
            except OSError:
                pass
            try:
                while True:
                    data, addr = sock.recvfrom(512)
                    ep = _parse(data)
                    if ep is not None and ep.host:
                        ep.host = ep.host or addr[0]
                        found[ep.host] = ep
            except socket.timeout:
                continue
            except OSError:                      # pragma: no cover
                break
    finally:
        try:
            sock.close()
        except OSError:                          # pragma: no cover
            pass
    return list(found.values())


def discover_async(callback, timeout: float = 1.2) -> threading.Thread:
    """后台发现，拿到结果后回调（回调里请自己切回界面线程，如 root.after）。"""

    def _run():
        try:
            callback(discover(timeout=timeout))
        except Exception:                        # noqa: BLE001 - 后台线程不许炸
            callback([])

    th = threading.Thread(target=_run, name='pi-discover', daemon=True)
    th.start()
    return th


# ---------------------------------------------------------------------- 记住
def load_last() -> Optional[PiEndpoint]:
    try:
        data = json.loads(LAST_FILE.read_text(encoding='utf-8'))
    except Exception:                            # noqa: BLE001 - 没记录/坏文件
        return None
    if not isinstance(data, dict) or not data.get('host'):
        return None
    return PiEndpoint(host=str(data['host']),
                      name=str(data.get('name') or 'robot-pi'),
                      img_port=int(data.get('img_port') or DEFAULT_IMG_PORT),
                      ser_port=int(data.get('ser_port') or DEFAULT_SER_PORT),
                      source='上次')


def save_last(ep: PiEndpoint) -> None:
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        tmp = LAST_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(ep.to_json(), ensure_ascii=False, indent=2),
                       encoding='utf-8')
        tmp.replace(LAST_FILE)
    except Exception:                            # noqa: BLE001 - 写不进去不影响连接
        pass


# ---------------------------------------------------------------------- 生效
def apply_env(ep: PiEndpoint) -> None:
    """告诉视觉层「相机在派上」。必须在创建 Stream/相机之前调用。"""
    os.environ['ROBOT_PI'] = ep.host
    os.environ['ROBOT_PI_IMG_PORT'] = str(int(ep.img_port))
    os.environ['ROBOT_PI_SER_PORT'] = str(int(ep.ser_port))


def clear_env() -> None:
    """切回本机相机（连串口时调用）。"""
    for key in ('ROBOT_PI', 'ROBOT_PI_IMG_PORT', 'ROBOT_PI_SER_PORT'):
        os.environ.pop(key, None)


def entries() -> List[PiEndpoint]:
    """下拉框要显示的派条目：上次连过的排前面（发现结果由界面异步合并）。"""
    out = []
    last = load_last()
    if last is not None:
        out.append(last)
    return out


def merge(found: List[PiEndpoint], last: Optional[PiEndpoint] = None) -> List[PiEndpoint]:
    """发现结果 + 上次记录，按 IP 去重，发现优先。"""
    out: List[PiEndpoint] = []
    seen = set()
    for ep in list(found or []) + ([last] if last is not None else []):
        if not ep or not ep.host or ep.host in seen:
            continue
        seen.add(ep.host)
        out.append(ep)
    return out
