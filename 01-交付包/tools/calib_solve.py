r"""从记录好的标注点直接求解 X/Y 标定（自动做 counts/脉冲单位归一化）。

用法（在 01-交付包 目录下）：
    .venv\Scripts\python.exe tools\calib_solve.py            # 只算，不写文件
    .venv\Scripts\python.exe tools\calib_solve.py --write    # 写 vision/tray_calib.json
    .venv\Scripts\python.exe tools\calib_solve.py --file vision\runtime\calib_marks.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG / "host"))

import calib_wizard as W          # noqa: E402
import auto_runner as AR          # noqa: E402



def _fit_direct(rows):
    """最小二乘：px = a*u + b*v + c ；py = d*u + e*v + f（返回两组系数）。"""
    n = len(rows)
    Su = sum(r['u'] for r in rows); Sv = sum(r['v'] for r in rows)
    Suu = sum(r['u'] ** 2 for r in rows); Svv = sum(r['v'] ** 2 for r in rows)
    Suv = sum(r['u'] * r['v'] for r in rows)
    M = [[Suu, Suv, Su], [Suv, Svv, Sv], [Su, Sv, n]]

    def det(m):
        return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
                - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
                + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))

    D = det(M)
    out = []
    for key in ('px', 'py'):
        b = [sum(r['u'] * r[key] for r in rows), sum(r['v'] * r[key] for r in rows),
             sum(r[key] for r in rows)]
        coef = []
        for i in range(3):
            Mi = [row[:] for row in M]
            for k in range(3):
                Mi[k][i] = b[k]
            coef.append(det(Mi) / D)
        out.append(coef)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="多点标注 → X/Y 标定（自动单位归一化）")
    ap.add_argument("--file", default=str(PKG / "vision" / "runtime" / "calib_marks.json"))
    ap.add_argument("--write", action="store_true", help="写入 vision/tray_calib.json")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--out", default=None, help="输出文件（默认 vision/tray_calib.json）")
    ap.add_argument("--keep-outliers", action="store_true", help="不做离群点剔除")
    ap.add_argument("--out-x", type=float, default=250.0, help="X 离群阈值（脉冲）")
    ap.add_argument("--out-y", type=float, default=80.0, help="Y 离群阈值（脉冲）")
    args = ap.parse_args(argv)

    src = Path(args.file)
    if not src.is_file():
        print("[FAIL] 找不到标注点文件：%s" % src)
        return 2
    data = json.loads(src.read_text(encoding="utf-8"))
    points = data.get("points") or []
    print("标注点：%d 个（来自 %s）" % (len(points), src))
    for i, p in enumerate(points, 1):
        print("  P%d u=%.4f v=%.4f  X=%+g Y=%+g" % (i, p.get("u", 0), p.get("v", 0),
                                                     p.get("px", 0), p.get("py", 0)))

    raw_limits = AR.load_limits()
    travel = {}
    for k, v in raw_limits.items():
        if not str(k).isdigit() or not isinstance(v, dict):
            continue
        travel[int(k)] = (v.get("min"), v.get("max"))
    print("软限位（脉冲）：%s" % travel)

    # 先做一次“原始拟合”找离群点：标注时手一抖，某个点会偏出 1~2 mm，
    # 它会把整个仿射拉歪。这里先去掉残差明显偏大的点，再正式求解。
    pts_n, notes = W.normalize_points(points, travel=travel)
    if not args.keep_outliers and len(pts_n) > 4:
        thr_x, thr_y = args.out_x, args.out_y
        while len(pts_n) > 4:
            ax, ay = _fit_direct(pts_n)
            worst, worst_score = None, 1.0
            for i, q in enumerate(pts_n):
                ex = abs(q['px'] - (ax[0] * q['u'] + ax[1] * q['v'] + ax[2]))
                ey = abs(q['py'] - (ay[0] * q['u'] + ay[1] * q['v'] + ay[2]))
                score = max(ex / thr_x, ey / thr_y)
                if score > worst_score:
                    worst, worst_score = (i, ex, ey), score
            if worst is None:
                break
            i, ex, ey = worst
            print("[离群] P%d 残差 dx=%.0f dy=%.0f 脉冲（阈值 %.0f/%.0f），剔除后重拟合"
                  % (i + 1, ex, ey, thr_x, thr_y))
            pts_n.pop(i)

    path = Path(args.out) if args.out else W.CALIB_PATH
    try:
        rep = W.solve(pts_n, path=path, write=args.write, backup=not args.no_backup,
                      travel=travel)
    except ValueError as exc:
        print("[FAIL] %s" % exc)
        print("提示：如果是「跨度超过行程」这类，说明某轴记录的是 counts；"
              "本工具会自动换算，但换算后仍然超限就是标注/记录有问题。")
        return 1

    for note in rep.get("notes") or []:
        print("[单位] %s" % note)
    aff = rep["affine"]
    print("反解（脉冲 = 系数·u/v + 常数）：")
    print("  X = %.1f*u %+.1f*v %+.1f" % (aff["a"], aff["b"], aff["tx"]))
    print("  Y = %.1f*u %+.1f*v %+.1f" % (aff["c"], aff["d"], aff["ty"]))
    per = rep["per_pulse"]
    print("每脉冲：X 方向 (du,dv)=(%.3e, %.3e)，Y 方向 (du,dv)=(%.3e, %.3e)"
          % (per["x"]["du"], per["x"]["dv"], per["y"]["du"], per["y"]["dv"]))
    mx = rep["max_abs_pulse"]
    print("残差：max|dx| = %.1f 脉冲, max|dy| = %.1f 脉冲（合格线 100）"
          % (mx["dx"], mx["dy"]))
    print("逐点残差：")
    for item in rep["residuals"]:
        print("  u=%.4f v=%.4f  dx=%+9.1f  dy=%+9.1f"
              % (item["u"], item["v"], item["dx_pulse"], item["dy_pulse"]))
    if args.write:
        print("已写入：%s%s" % (rep.get("path"), "" if not rep.get("backup") else
                               "（备份：%s）" % rep["backup"]))
    else:
        print("（未写文件；确认数字可用后加 --write）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
