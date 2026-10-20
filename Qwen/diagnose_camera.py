"""Read-only DirectShow device activation diagnostics (HRESULT)."""
import ctypes as c
from comtypes import GUID


def method(pointer, slot, *types):
    table = c.cast(pointer, c.POINTER(c.POINTER(c.c_void_p))).contents
    return c.WINFUNCTYPE(c.c_long, c.c_void_p, *types)(table[slot])


def main():
    ole = c.OleDLL('ole32')
    ole.CoInitialize(None)
    dev = c.c_void_p()
    hr = ole.CoCreateInstance(c.byref(GUID('{62BE5D10-60EB-11D0-BD3B-00A0C911CE86}')),
        None, 1, c.byref(GUID('{29840822-5B84-11D0-BD3B-00A0C911CE86}')), c.byref(dev))
    if hr:
        raise RuntimeError(hex(hr & 0xffffffff))
    enum = c.c_void_p()
    method(dev, 3, c.c_void_p, c.c_void_p, c.c_ulong)(dev,
        c.byref(GUID('{860BB310-5D01-11D0-BD3B-00A0C911CE86}')), c.byref(enum), 0)
    index = 0
    while True:
        moniker = c.c_void_p()
        fetched = c.c_ulong()
        if method(enum, 3, c.c_ulong, c.c_void_p, c.c_void_p)(enum, 1, c.byref(moniker), c.byref(fetched)) != 0:
            break
        target = c.c_void_p()
        hr = method(moniker, 8, c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p)(
            moniker, None, None, c.byref(GUID('{56A86895-0AD4-11CE-B03A-0020AF0BA770}')), c.byref(target))
        print(f'Device {index}: BindToObject HRESULT=0x{hr & 0xffffffff:08X}', flush=True)
        if target:
            method(target, 2)(target)
        method(moniker, 2)(moniker)
        index += 1
    method(enum, 2)(enum)
    method(dev, 2)(dev)
    ole.CoUninitialize()


if __name__ == '__main__':
    main()
