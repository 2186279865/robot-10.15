"""从派端图传拉两路各一帧，存成 jpg 供查看。用法：python grab_two.py <PI_IP> [out_prefix]"""
import socket
import sys
import pathlib

PKG = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG / '10.21' / 'common'))
import wire  # noqa: E402


def grab(host, cam, port=5057, timeout=5.0):
    s = wire.open_tcp(host, port, timeout)
    try:
        s.sendall(wire.pack_request(cam, 1280, 720, 80))
        magic = wire.recv_exact(s, 2)
        if magic == b'ER':
            hdr = wire.parse_error_header(wire.recv_exact(s, wire.ERR_HDR_LEN - 2))
            body = wire.recv_exact(s, hdr['n'])
            raise RuntimeError('派端报错: %s %s' % (hdr['code'], body.decode('utf-8', 'replace')))
        hdr = wire.parse_image_header(magic + wire.recv_exact(s, wire.IMG_HDR_LEN - 2))
        return hdr, wire.recv_exact(s, hdr['n'])
    finally:
        s.close()


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else '192.168.100.198'
    prefix = sys.argv[2] if len(sys.argv) > 2 else 'real_cam'
    outdir = PKG / '10.21' / 'runtime'
    outdir.mkdir(parents=True, exist_ok=True)
    for cam in (0, 1):
        hdr, body = grab(host, cam)
        p = outdir / ('%s%d.jpg' % (prefix, cam))
        p.write_bytes(body)
        print('cam%d %dx%d %d B -> %s' % (cam, hdr['width'], hdr['height'], hdr['n'], p))


if __name__ == '__main__':
    main()
