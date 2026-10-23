#!/usr/bin/env python
"""Check that the vision package imports and its SAM weights are present.

Used by selftest.bat step 2. Safe to run with any interpreter that has the
package requirements installed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # 10.21/tests
PKG = HERE.parent                               # 10.21
VISION = PKG / 'vision'

for _p in (str(PKG / 'common'), str(PKG), str(VISION), str(PKG / 'host')):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main() -> int:
    import auto_segment            # noqa: F401
    import fusion                  # noqa: F401
    import classify                # noqa: F401
    import tray_calib              # noqa: F401
    import camera_factory          # noqa: F401
    import remote_camera           # noqa: F401
    import wire                    # noqa: F401

    weights = VISION / 'models' / 'sam_b.pt'
    if not weights.is_file():
        print('  MISSING %s' % weights, file=sys.stderr)
        return 1
    size_mb = os.path.getsize(weights) / (1024.0 * 1024.0)

    import cv2
    print('  opencv %s, numpy %s' % (cv2.__version__, __import__('numpy').__version__))
    print('  SAM weights %.1f MB' % size_mb)
    print('  vision dir %s' % VISION)
    return 0


if __name__ == '__main__':
    sys.exit(main())
