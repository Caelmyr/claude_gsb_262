"""文档矫正：倾斜检测/摆正、文档四角检测/透视校正。

全部基于 Pillow + 逐像素统计（与 features/detection 同一套思路），
像素级运算只在降采样工作副本上进行：

- detect_skew_angle：投影剖面法。阈值取出「墨迹」像素（文字/边线），
  按候选角旋转后统计水平投影方差——文字行/矩形边线越接近水平，行间的
  投影起伏越尖锐（方差越大），方差最大处即倾斜角。粗扫 + 细扫两级搜索。
- deskew：按角度旋转（expand 保留全部内容）后自动裁掉旋转产生的白边。
- detect_document_corners：边缘掩码 -> 闭运算 -> 连通域（剔除贴边的）
  -> 外接框四象限取角，按 左上/右上/右下/左下 顺序返回。
- perspective_correct：用 Image.transform(QUAD) 把任意四边形拉回正矩形。
"""
import math

from PIL import Image, ImageDraw, ImageFilter

from . import util

# 角度检测/角点检测的工作分辨率：统计量与尺寸无关，低分辨率足够且快
DETECT_DIM = 480
SKEW_DIM = 260


# ---------------------------------------------------------------------------
# 倾斜角检测（投影剖面法）
# ---------------------------------------------------------------------------
def detect_skew_angle(image, params=None):
    """检测图像需要摆正的角度。

    返回 {"angle": deg, "confidence": 0..1}。angle 是内容相对水平/竖直
    轴的倾斜角（屏幕坐标，正值=内容逆时针倾斜，与 PIL Image.rotate
    的角度约定一致）；把它传给 deskew 即可摆正（deskew 内部反向旋转）。
    """
    params = params or {}
    max_angle = min(45.0, abs(float(params.get("max_angle", 45.0))))
    work = util.downscale_to_max(util.ensure_rgb(image), SKEW_DIM)
    gray = util.to_grayscale(work).filter(ImageFilter.GaussianBlur(1.0))

    # 用直方图中位数自适应区分「墨迹」与纸面/衬底，避免对背景色做假设
    hist = gray.histogram()
    total = sum(hist)
    acc, median = 0, 128
    for i, c in enumerate(hist):
        acc += c
        if acc >= total * 0.5:
            median = i
            break
    cutoff = max(60, median - 25)
    ink = gray.point(lambda v: 1 if v < cutoff else 0)
    ink_count = sum(ink.getdata())
    if ink_count < ink.size[0] * ink.size[1] * 0.01:
        return {"angle": 0.0, "confidence": 0.0}

    def _profile_variance(angle):
        """旋转到候选角后，水平投影的行计数方差。"""
        r = ink.rotate(angle, resample=Image.Resampling.BILINEAR,
                       expand=False, fillcolor=0)
        d = list(r.getdata())
        rw, rh = r.size
        prof = [sum(d[y * rw:(y + 1) * rw]) for y in range(rh)]
        n = len(prof)
        mean = sum(prof) / n
        return sum((v - mean) ** 2 for v in prof) / n

    base = _profile_variance(0.0)

    # 两级搜索：先 2 度粗扫，再在最优点附近 0.25 度细扫
    coarse_angles = [a * 0.5 for a in range(-int(max_angle * 2),
                                            int(max_angle * 2) + 1)]
    coarse_best = max(coarse_angles, key=_profile_variance)
    fine_angles = [coarse_best - 1.0 + i * 0.25 for i in range(9)]
    content_skew = max(fine_angles, key=_profile_variance)

    # 内容倾斜 content_skew 时，反向旋转摆正；rotate 角 = -content_skew
    angle = -content_skew
    if abs(angle) < 0.1:
        angle = 0.0

    # 置信度：最优投影相对 0 度投影的提升幅度
    peak = _profile_variance(content_skew)
    confidence = 0.0 if peak <= 1e-9 else max(0.0, min(1.0, (peak - base) / peak))
    if abs(angle) > max_angle:
        angle, confidence = 0.0, 0.0
    return {"angle": round(angle, 2), "confidence": round(confidence, 3)}


def deskew(image, params=None):
    """按给定角度旋转摆正并自动裁掉白边。

    入参 angle 是「检测到的内容倾斜角」（正值表示内容在屏幕上逆时针倾斜，
    与 detect_skew_angle / Image.rotate 的约定一致）。摆正需反向旋转，
    故内部用 -angle 调用 rotate；对外仍回传 angle 本身。
    """
    params = params or {}
    angle = float(params.get("angle", 0.0))
    fill = tuple(params.get("fill", (255, 255, 255)))
    autocrop = bool(params.get("autocrop", True))
    rgb = util.ensure_rgb(image)
    if abs(angle) < 1e-6:
        return {"image": rgb, "angle": 0.0, "cropped": False}

    rotated = rgb.rotate(-angle, resample=Image.Resampling.BICUBIC,
                         expand=True, fillcolor=fill)
    cropped = False
    if autocrop:
        trimmed = _autocrop_background(rotated, fill)
        if trimmed is not None:
            rotated = trimmed
            cropped = True
    return {"image": rotated, "angle": angle, "cropped": cropped}


def _autocrop_background(image, fill, tolerance=12):
    """裁掉旋转引入的纯色（近 fill）边。裁得太狠时（面积不足 55%）放弃。"""
    rgb = image.convert("RGB")
    w, h = rgb.size
    # 用 ImageChops 差值 + getbbox 找非填充区域
    bg = Image.new("RGB", rgb.size, fill)
    from PIL import ImageChops
    diff = ImageChops.difference(rgb, bg).convert("L")
    mask = diff.point(lambda v: 255 if v > tolerance else 0)
    bbox = mask.getbbox()
    if not bbox:
        return None
    x0, y0, x1, y1 = bbox
    # 向内收 1 像素，避开抗锯齿产生的边缘半透明像素
    x0 = min(x0 + 1, w - 1); y0 = min(y0 + 1, h - 1)
    x1 = max(x1 - 1, 0); y1 = max(y1 - 1, 0)
    if x1 <= x0 or y1 <= y0:
        return None
    if (x1 - x0) * (y1 - y0) < 0.55 * w * h:
        return None  # 背景色与内容接近，bbox 不可信，保留整图
    return rgb.crop((x0, y0, x1, y1))


# ---------------------------------------------------------------------------
# 文档四角检测
# ---------------------------------------------------------------------------
def detect_document_corners(image, params=None):
    """检测文档（纸张）四角。

    照片中的纸张是亮于衬底的近矩形块。按直方图估计纸白/背景阈值后，
    对每行找「暗背景 -> 亮纸面」的阶跃点作为左右边、每列找作为上下边；
    阶跃要求内侧持续一段亮像素，从而排除纸边外抗锯齿光晕的干扰。
    四条边稳健拟合直线后求交得到四角。

    返回 {"corners": [[x,y]x4], "score": 0..1, "w", "h"}，顺序为
    左上/右上/右下/左下；检测失败回退到整幅图内缩四边形。
    """
    rgb = util.ensure_rgb(image)
    w0, h0 = rgb.size
    work = util.downscale_to_max(rgb, DETECT_DIM)
    w, h = work.size
    gray = util.to_grayscale(work).filter(ImageFilter.GaussianBlur(1.0))
    g = list(gray.getdata())

    # 从直方图估计背景亮度与纸张亮度。纸张可能只占画面一小块，固定分位
    # 会被背景主导，故先平滑直方图，取「最暗的显著峰」与「最亮的显著峰」。
    raw = gray.histogram()
    total = sum(raw)
    k = 5
    hist = [sum(raw[max(0, i - k):i + k + 1]) / len(raw[max(0, i - k):i + k + 1])
            for i in range(256)]
    peaks = [i for i in range(8, 248)
             if hist[i] >= hist[i - 1] and hist[i] >= hist[i + 1]
             and hist[i] > total * 0.004]
    if not peaks:
        return _fallback_corners(w0, h0, w, h, 0.0)
    # 背景峰 = 最暗的显著峰；纸峰 = 比背景亮一个明显间隔的最亮显著峰。
    # 用累积占比兜底：即使纸占比小，亮度最高的一簇仍对应纸白。
    bg_v = peaks[0]
    bright_peaks = [i for i in peaks if i >= bg_v + 40]
    paper_v = bright_peaks[-1] if bright_peaks else peaks[-1]
    contrast = paper_v - bg_v
    if contrast < 40:
        return _fallback_corners(w0, h0, w, h, 0.0)
    bright = paper_v - contrast * 0.25   # 判定「进入纸面」
    dark = bg_v + contrast * 0.55        # 判定「离开纸面到背景」
    # 阶跃判定：当前点亮、朝纸内几像素持续亮（确认真进了纸面，而非光晕），
    # 朝背景几像素确实暗。过渡带约 8~12px，窗口不能太大。
    win = max(4, min(10, int(w * 0.02)))
    gap = max(6, min(14, int(w * 0.028)))

    def row_arr(y):
        return g[y * w:(y + 1) * w]

    def col_arr(x):
        return g[x::w]

    def step_forward(a):
        """从左到右找第一个「暗背景 -> 亮纸面」的阶跃位置。"""
        n = len(a)
        for i in range(gap, n - win):
            if (a[i] >= bright and min(a[i:i + win]) >= bright
                    and max(a[i - gap:i - 2]) <= dark):
                return i
        return None

    def step_backward(a):
        """从右到左找「亮纸面 -> 暗背景」的阶跃（返回亮侧最后一点）。"""
        n = len(a)
        for i in range(n - gap - 1, win - 1, -1):
            if (a[i] >= bright and min(a[i - win + 1:i + 1]) >= bright
                    and max(a[i + 3:i + gap]) <= dark):
                return i
        return None

    # 收集左右边散点：逐行暗->亮阶跃（要求阶跃后持续一段亮像素，
    # 排除纸边外抗锯齿光晕的短亮带）。
    left, right = [], []
    for y in range(h):
        r = row_arr(y)
        xl = step_forward(r)
        xr = step_backward(r)
        if xl is not None and xr is not None and xr - xl > w * 0.12:
            left.append((y, xl))    # (y,x) 拟合 x=m*y+c
            right.append((y, xr))
    if len(left) < 20 or len(right) < 20:
        return _fallback_corners(w0, h0, w, h, 0.0)
    mL, cL = _fit_line_robust(left)
    mR, cR = _fit_line_robust(right)

    # 上、下边框近水平。对每一列，若其中部 40%~60% 高度几乎全亮（说明该列
    # 是「纸内列」，从而排除斜纸边外只有端部亮的光晕列），就取该列沿 y 的
    # 暗->亮 / 亮->暗阶跃作为上、下边缘散点，稳健拟合成斜线。
    top, bottom = [], []
    for x in range(w):
        mid = g[x + int(h * 0.4) * w: x + int(h * 0.6) * w: w]
        if sum(1 for v in mid if v >= bright) / len(mid) < 0.9:
            continue
        c = col_arr(x)
        yt = step_forward(c)
        yb = step_backward(c)
        if yt is not None:
            top.append((x, yt))
        if yb is not None:
            bottom.append((x, yb))
    if len(top) < 12 or len(bottom) < 12:
        return _fallback_corners(w0, h0, w, h, 0.0)
    mT, cT = _fit_line_robust(top)     # 上边 y=mT*x+cT
    mB, cB = _fit_line_robust(bottom)

    def _inter(mv, cv, mh_, ch_):
        """侧边 x=mv*y+cv 与 横边 y=mh_*x+ch_ 的交点。"""
        den = 1 - mv * mh_
        if abs(den) < 1e-6:
            return None
        x = (mv * ch_ + cv) / den
        return x, mh_ * x + ch_

    TL = _inter(mL, cL, mT, cT)
    TR = _inter(mR, cR, mT, cT)
    BL = _inter(mL, cL, mB, cB)
    BR = _inter(mR, cR, mB, cB)
    quad = [TL, TR, BR, BL]
    if any(p is None for p in quad):
        return _fallback_corners(w0, h0, w, h, 0.0)

    if any(not (-0.15 * w <= x <= 1.15 * w and -0.15 * h <= y <= 1.15 * h)
           for x, y in quad):
        return _fallback_corners(w0, h0, w, h, 0.0)

    # 四角应构成凸四边形：简单校验相邻边长度与顺序
    poly_ok = (_poly_area(quad) > 0.04 * w * h and
               _poly_area(quad) < 0.99 * w * h)
    if not poly_ok:
        return _fallback_corners(w0, h0, w, h, 0.0)

    sx, sy = w0 / float(w), h0 / float(h)
    corners = [[round(x * sx, 1), round(y * sy, 1)] for x, y in quad]
    cover = _poly_area(quad) / float(w * h)
    score = round(min(1.0, cover / 0.20), 3)
    return {"corners": corners, "score": score, "w": w0, "h": h0}


def _poly_area(quad):
    (x1, y1), (x2, y2), (x3, y3), (x4, y4) = quad
    return 0.5 * abs(x1 * y2 - x2 * y1 + x2 * y3 - x3 * y2
                     + x3 * y4 - x4 * y3 + x4 * y1 - x1 * y4)



def _fit_line(points):
    """最小二乘 y = m x + c。points: [(x,y), ...]。"""
    n = len(points)
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in points)
    m = sxy / sxx if sxx > 1e-9 else 0.0
    return m, my - m * mx


def _fit_line_robust(points, iters=3, k=2.0):
    """迭代最小二乘：每轮剔除残差超过 k 倍标准差的离群点（光晕/噪声）。"""
    pts = list(points)
    for _ in range(iters):
        if len(pts) < 8:
            break
        m, c = _fit_line(pts)
        res = sorted(abs(y - (m * x + c)) for x, y in pts)
        sigma = res[len(res) // 2] or res[int(len(res) * 0.75)] or 1.0
        tol = max(2.0, k * sigma)
        kept = [(x, y) for x, y in pts if abs(y - (m * x + c)) <= tol]
        if len(kept) < 8 or len(kept) == len(pts):
            pts = kept or pts
            break
        pts = kept
    return _fit_line(pts)


def _fallback_corners(w0, h0, w, h, score):
    """检测失败：返回内缩 4% 的整幅框，前端仍可手动拖动四角。"""
    mx, my = w * 0.04, h * 0.04
    sx, sy = w0 / float(w), h0 / float(h)
    quad = [(mx, my), (w - mx, my), (w - mx, h - my), (mx, h - my)]
    return {"corners": [[round(x * sx, 1), round(y * sy, 1)] for x, y in quad],
            "score": score, "w": w0, "h": h0}


# ---------------------------------------------------------------------------
# 透视校正
# ---------------------------------------------------------------------------
def perspective_correct(image, params=None):
    """按四角做透视变换，把梯形拉回正矩形。

    params: corners = [[x,y] x 4]（左上/右上/右下/左下，原图像素坐标）；
    可选 out_ratio = 宽/高，不给则按四边形边的平均长度推算。
    """
    params = params or {}
    corners = params.get("corners")
    rgb = util.ensure_rgb(image)
    w, h = rgb.size
    if not corners or len(corners) != 4:
        return {"image": rgb, "corners": []}

    pts = [(float(x), float(y)) for x, y in corners]
    # 限制在图像范围内，避免越界
    pts = [(min(max(x, 0), w - 1), min(max(y, 0), h - 1)) for x, y in pts]
    (tl, tr, br, bl) = pts

    width_top = math.hypot(tr[0] - tl[0], tr[1] - tl[1])
    width_bottom = math.hypot(br[0] - bl[0], br[1] - bl[1])
    height_left = math.hypot(bl[0] - tl[0], bl[1] - tl[1])
    height_right = math.hypot(br[0] - tr[0], br[1] - tr[1])
    out_w = max(2, int(round((width_top + width_bottom) / 2)))
    out_h = max(2, int(round((height_left + height_right) / 2)))

    ratio = params.get("out_ratio")
    if ratio and ratio > 0:
        if out_w / float(out_h) < ratio:
            out_h = max(2, int(round(out_w / ratio)))
        else:
            out_w = max(2, int(round(out_h * ratio)))

    # Image.transform(QUAD)：quad 为源图中 左上、左下、右下、右上 四点
    quad = [tl[0], tl[1], bl[0], bl[1], br[0], br[1], tr[0], tr[1]]
    warped = rgb.transform((out_w, out_h), Image.Transform.QUAD, quad,
                           resample=Image.Resampling.BICUBIC,
                           fillcolor=(255, 255, 255))
    return {"image": warped, "corners": [list(p) for p in pts],
            "out_width": out_w, "out_height": out_h}


def node_perspective(image, params=None):
    """流水线节点用：自动检测四角后做透视校正（节点不带角点参数）。"""
    params = params or {}
    if not params.get("auto_corners", True):
        return {"image": image}
    detected = detect_document_corners(image, {})
    if detected["score"] <= 0:
        return {"image": image, "score": 0.0}
    return perspective_correct(image, {"corners": detected["corners"]})


def node_deskew(image, params=None):
    """流水线节点用：angle=0 时自动检测角度再摆正。"""
    params = dict(params or {})
    angle = float(params.get("angle", 0)) + float(params.get("manual", 0))
    if abs(float(params.get("angle", 0))) < 1e-9 and abs(float(params.get("manual", 0))) < 1e-9:
        angle = detect_skew_angle(image, {}).get("angle", 0.0)
    return deskew(image, {"angle": angle, "autocrop": params.get("autocrop", True)})


def draw_corners_preview(image, corners):
    """在图上画出检测到的四边形与四角（供前端确认检测结果）。"""
    rgb = util.ensure_rgb(image).copy()
    draw = ImageDraw.Draw(rgb)
    pts = [(float(x), float(y)) for x, y in corners]
    if len(pts) == 4:
        draw.polygon(pts, outline=(255, 82, 82), width=max(2, rgb.size[0] // 300))
        for i, (x, y) in enumerate(pts):
            r = max(5, rgb.size[0] // 120)
            draw.ellipse([x - r, y - r, x + r, y + r],
                         fill=(255, 82, 82), outline=(255, 255, 255), width=2)
            draw.text((x + r + 2, y - r), str(i + 1), fill=(255, 82, 82))
    return rgb
