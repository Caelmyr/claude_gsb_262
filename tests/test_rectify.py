"""文档矫正算法的正确性验证：倾斜检测/摆正、四角检测、透视校正往返。

运行：python tests/test_rectify.py
全部用 Pillow 合成「已知真值」的图像（旋转角、四角坐标都已知），
断言检测误差在可接受范围内。不经过 HTTP 层。
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw  # noqa: E402

from server.algorithms import rectify  # noqa: E402


def _text_doc(w=500, h=700, bg=(252, 251, 248), ink=(90, 90, 95), border=False):
    img = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(img)
    if border:
        d.rectangle([30, 30, w - 30, h - 30], outline=(0, 0, 0), width=3)
    for y in range(70, h - 40, 30):
        x2 = w - 60 if (y // 30) % 3 else int(w * 0.6)
        d.rectangle([60, y, x2, y + 10], fill=ink)
    return img


def _solve8(A, b):
    """8x8 高斯消元（求透视变换系数用，避免依赖 numpy）。"""
    n = 8
    m = [list(map(float, A[i])) + [float(b[i])] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[piv] = m[piv], m[col]
        d = m[col][col]
        for j in range(col, n + 1):
            m[col][j] /= d
        for r in range(n):
            if r != col:
                f = m[r][col]
                for j in range(col, n + 1):
                    m[r][j] -= f * m[col][j]
    return [m[i][n] for i in range(n)]


def _perspective_coeffs(src, dst):
    A, b = [], []
    for (x, y), (u, v) in zip(dst, src):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.append(u)
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y]); b.append(v)
    return _solve8(A, b)


def _render_perspective(doc, quad, size=(900, 1100), bg=(110, 88, 66)):
    """把正矩形文档用真实 PERSPECTIVE 变换贴到指定四边形（无渲染光晕瑕疵）。"""
    W, H = size
    src = [(0, 0), (doc.size[0], 0), (doc.size[0], doc.size[1]), (0, doc.size[1])]
    coeffs = _perspective_coeffs(src, quad)
    warped = doc.transform((W, H), Image.Transform.PERSPECTIVE, coeffs,
                           resample=Image.Resampling.BICUBIC)
    canvas = Image.new("RGB", (W, H), bg)
    alpha = warped.convert("L").point(lambda v: 255 if v > 10 else 0)
    return Image.composite(warped, canvas, alpha)


def test_skew():
    doc = _text_doc(600, 800, bg=(255, 255, 255), ink=(40, 40, 40))
    worst = 0
    for true in (-15, -8, -3, 5, 12, 20):
        tilted = doc.rotate(true, resample=Image.Resampling.BICUBIC,
                            expand=True, fillcolor=(190, 190, 190))
        got = rectify.detect_skew_angle(tilted)["angle"]
        err = abs(got - true)
        worst = max(worst, err)
        assert err < 1.5, f"倾斜 {true}° 检测为 {got}°，误差 {err}"
    print(f"  倾斜检测：±20° 内最大误差 {worst:.2f}° ✔")

    # 摆正后再检测应接近 0
    tilted = doc.rotate(9, resample=Image.Resampling.BICUBIC, expand=True,
                        fillcolor=(190, 190, 190))
    ang = rectify.detect_skew_angle(tilted)["angle"]
    fixed = rectify.deskew(tilted, {"angle": ang})["image"]
    again = rectify.detect_skew_angle(fixed)["angle"]
    assert abs(again) < 1.0, f"摆正后残余 {again}°"
    print(f"  摆正后残余倾斜 {again:.2f}° ✔")


def test_corners_and_perspective():
    doc = _text_doc()
    cases = {
        "轻微透视": ((180, 150), (720, 220), (690, 980), (130, 900)),
        "强透视": ((260, 180), (820, 260), (760, 1020), (140, 940)),
        "近正面": ((140, 120), (760, 140), (740, 1020), (120, 1000)),
        "小文档": ((260, 300), (640, 340), (620, 820), (220, 780)),
    }
    for name, quad in cases.items():
        img = _render_perspective(doc, list(quad))
        r = rectify.detect_document_corners(img)
        assert r["score"] > 0.5, f"{name} 检测失败 score={r['score']}"
        err = max(math.dist(c, quad[i]) for i, c in enumerate(r["corners"]))
        assert err < 12, f"{name} 四角最大误差 {err:.1f}px"
        # 透视校正后应回到接近原文档的正矩形（宽高比与四角推算一致）
        out = rectify.perspective_correct(img, {"corners": r["corners"]})
        ow, oh = out["out_width"], out["out_height"]
        assert ow > 100 and oh > 100
        # 校正结果四条边对应长度应近似（顶≈底、左≈右），用输出本身是矩形保证
        ratio = ow / oh
        assert 0.4 < ratio < 1.0, f"{name} 输出比例异常 {ratio:.2f}"
        print(f"  {name}：四角最大误差 {err:.1f}px，校正为 {ow}×{oh} ✔")


def test_perspective_handles_clamped_corners():
    doc = _text_doc()
    img = _render_perspective(doc, [(180, 150), (720, 220), (690, 980), (130, 900)])
    # 传入越界/负坐标角点不应抛异常
    bad = [[-50, -20], [9000, 0], [9000, 9000], [0, 9000]]
    out = rectify.perspective_correct(img, {"corners": bad})
    assert out["image"].size[0] > 0
    # 角点数量不对时原样返回
    out2 = rectify.perspective_correct(img, {"corners": [[0, 0]]})
    assert out2["image"].size == img.size
    print("  异常角点容错 ✔")


def test_front_facing():
    canvas = Image.new("RGB", (900, 1100), (110, 88, 66))
    canvas.paste(_text_doc(), (200, 200))
    real = [(200, 200), (700, 200), (700, 900), (200, 900)]
    r = rectify.detect_document_corners(canvas)
    err = max(math.dist(c, real[i]) for i, c in enumerate(r["corners"]))
    assert err < 12, f"正面文档角点误差 {err:.1f}px"
    print(f"  正面矩形文档：四角最大误差 {err:.1f}px ✔")


def main():
    print("== 倾斜检测与摆正 ==")
    test_skew()
    print("\n== 四角检测与透视校正 ==")
    test_corners_and_perspective()
    test_front_facing()
    test_perspective_handles_clamped_corners()
    print("\n文档矫正测试全部通过 ✔")


if __name__ == "__main__":
    main()
