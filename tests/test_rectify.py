"""文档矫正算法冒烟测试：倾斜检测/摆正、四边形检测/透视校正、边界情形。

运行：python tests/test_rectify.py
合成「拍歪的文档」（已知真值）验证：
- detect_skew 在多个倾斜角下误差 < 0.5°，摆正角与倾斜角符号相反；
- straighten 后再检测，残余倾斜应接近 0；
- detect_document_quad 能从梯形透视夹具中找回四个角（归一化坐标误差 < 0.03）；
- perspective_correct 把梯形拉回矩形，输出四角应与文档底色一致；
- 手动 angle / 兜底四边形 / 退化角点抛错等边界行为。
"""
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw  # noqa: E402

from server.algorithms import rectify  # noqa: E402


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def make_text_page(w=700, h=900, bg=(255, 255, 252), line=(30, 30, 30), seed=1):
    """生成一张布满水平文字行（细矩形模拟）的文档。"""
    img = Image.new("RGB", (w, h), bg)
    dd = ImageDraw.Draw(img)
    rnd = random.Random(seed)
    for row in range(14):
        y = 90 + row * 52
        x = 70
        while x < 600:
            ww = rnd.randrange(18, 70)
            hh = rnd.choice([2, 3])
            dd.rectangle([x, y, x + ww, y + hh], fill=line)
            x += ww + rnd.randrange(8, 22)
    return img


def tilted(angle):
    """把文档旋转 angle 度（PIL 逆时针为正），模拟拍歪。"""
    return make_text_page().rotate(
        angle, expand=True, fillcolor=(252, 252, 252),
        resample=Image.Resampling.BICUBIC)


def perspective_page(quad, canvas_size=(760, 900), desk=(90, 96, 104)):
    """把正面文档按给定四角 quad（TL,TR,BR,BL）透视贴到桌面上，模拟侧拍。"""
    doc = make_text_page(520, 720, seed=3)
    dd = ImageDraw.Draw(doc)
    dd.rectangle([24, 24, 496, 696], outline=(60, 60, 60), width=3)
    W, H = canvas_size
    canvas = Image.new("RGB", (W, H), desk)
    # output(梯形坐标) 采样自 input(文档坐标) => dst(quad)->src(矩形)
    coeffs = rectify._solve_homography(
        [(0, 0), (520, 0), (520, 720), (0, 720)], quad)
    warped = doc.transform(
        (W, H), Image.Transform.PERSPECTIVE, coeffs, Image.Resampling.BICUBIC)
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).polygon(quad, fill=255)
    canvas.paste(warped, (0, 0), mask)
    return canvas


# ---------------------------------------------------------------------------
# 断言
# ---------------------------------------------------------------------------
PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✔ {name}")
    else:
        FAIL += 1
        print(f"  ✘ {name}  {detail}")


def main():
    print("== 倾斜角检测 ==")
    for truth in (8, -12, 3.5, -1.2):
        img = tilted(truth)
        info = rectify.detect_skew(img)
        # 摆正角应 ≈ -truth
        err = abs(info["angle"] - (-truth))
        check(f"倾斜 {truth:+}° -> 摆正角 {info['angle']:+.2f}°（误差 {err:.2f}°）",
              err < 0.6, f"angle={info['angle']}")
        check(f"倾斜 {truth:+}° 置信度高（{info['confidence']}）",
              info["confidence"] >= 0.6)

    print("\n== 摆正后残余倾斜 ==")
    img = tilted(8)
    out = rectify.straighten(img, {})
    again = rectify.detect_skew(out["image"])
    check(f"摆正后残余 |tilt| < 0.6°（实测 {again['tilt']:+.2f}°）",
          abs(again["tilt"]) < 0.6)
    check("白边已裁剪（输出不大于原图）",
          out["image"].size[0] <= img.size[0] and out["image"].size[1] <= img.size[1])

    print("\n== 手动角度 ==")
    out_manual = rectify.straighten(img, {"angle": -5.0})
    check("手动 angle=-5 时 meta.angle=-5", out_manual["angle"] == -5.0)
    out_auto0 = rectify.straighten(img, {"auto": True})
    check("auto=True 仍走检测（meta.confidence 非 None）",
          out_auto0["confidence"] is not None)

    print("\n== 无内容图（应低置信、不报错）==")
    blank = Image.new("RGB", (400, 300), (255, 255, 255))
    info = rectify.detect_skew(blank)
    check(f"空白图置信度为 0（实测 {info['confidence']}）", info["confidence"] == 0.0)

    print("\n== 建筑/中灰结构照（边缘掩码回退）==")
    bld = Image.new("RGB", (600, 800), (120, 150, 180))
    bd = ImageDraw.Draw(bld)
    for x in range(40, 580, 90):
        bd.rectangle([x, 40, x + 50, 760], fill=(90, 110, 130))
    for y in range(60, 780, 120):
        bd.rectangle([20, y, 580, y + 12], fill=(70, 90, 110))
    tilted_bld = bld.rotate(6, expand=True, fillcolor=(120, 150, 180))
    binfo = rectify.detect_skew(tilted_bld)
    check(f"建筑 6° -> 摆正角 {binfo['angle']:+.1f}°（误差 <1°）",
          abs(binfo["angle"] - (-6)) < 1.0, f"angle={binfo['angle']}")
    check("建筑图走 edge 掩码", binfo["mask"] == "edge", binfo.get("mask"))

    print("\n== 文档四边形检测 ==")
    quad = [(210, 150), (560, 150), (700, 820), (60, 820)]
    page = perspective_page(quad)
    det = rectify.detect_document_quad(page)
    check("检测方法为轮廓跟踪", det["method"] == "boundary", det["method"])
    check(f"置信度 > 0.6（实测 {det['confidence']}）", det["confidence"] > 0.6)
    W, H = page.size
    norm_truth = [[x / W, y / H] for x, y in quad]
    errs = [math.hypot(a[0] - b[0], a[1] - b[1])
            for a, b in zip(det["normalized"], norm_truth)]
    check(f"四角归一化误差 max={max(errs):.3f} < 0.03", max(errs) < 0.03,
          f"det={det['normalized']}")

    print("\n== 透视校正 ==")
    fixed = rectify.perspective_correct(page, {"corners": det["normalized"],
                                               "normalized": True})
    fw, fh = fixed["image"].size
    check(f"输出为竖向矩形（{fw}×{fh}）", fh > fw * 1.2)
    # 输出四角附近应是文档内容（非桌面深灰 90,96,104）
    corners_px = [fixed["image"].getpixel(p)
                  for p in ((10, 10), (fw - 10, 10), (fw - 10, fh - 10), (10, fh - 10))]
    bright = all(sum(c) / 3 > 180 for c in corners_px)
    check("输出四角为浅色文档区域", bright, str(corners_px))

    print("\n== 透视校正边界 ==")
    try:
        rectify.perspective_correct(page, {"corners": [[0, 0], [1, 0]]})
        check("角点不足时抛错", False)
    except ValueError:
        check("角点不足时抛 ValueError", True)
    try:
        rectify._solve_homography([(0, 0)] * 4, [(0, 0), (1, 0), (1, 1), (0, 1)])
        check("退化角点抛错", False)
    except ValueError:
        check("退化角点抛 ValueError", True)

    print("\n== 兜底四边形（检测不到纸张时不崩）==")
    noise = Image.new("RGB", (400, 300), (120, 120, 120))
    det2 = rectify.detect_document_quad(noise)
    check("检测失败时返回 4 个兜底角点", len(det2["quad"]) == 4)
    check("兜底角点在画面内部", all(0 <= x <= 400 and 0 <= y <= 300 for x, y in det2["quad"]))
    fixed2 = rectify.perspective_correct(noise, {"corners": det2["normalized"],
                                                 "normalized": True})
    check("兜底角点也能产出结果图", fixed2["image"].size[0] > 0)

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    if FAIL:
        sys.exit(1)
    print("矫正冒烟测试全部通过 ✔")


if __name__ == "__main__":
    main()
