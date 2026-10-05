"""文档/建筑图像矫正：倾斜角检测、自动摆正、文档四边形检测、透视校正。

纯 Pillow 实现（无 numpy/opencv 依赖），全部计算在低分辨率工作副本上完成：

- detect_skew：投影轮廓法（projection profile）。把文字/结构线二值化，
  对一系列候选旋转角用 NEAREST 重采样旋转，计算行投影的相邻行差分能量；
  水平内容的行投影峰谷最分明，能量在「摆正角」处取最大值。
  粗搜 ±45°（步长 2°）+ 精搜 ±4°（步长 0.1°），并给出置信度。
  掩码优先取暗笔画（文档），暗像素过少时回退到强边缘掩码（建筑/低对比照）。
  必须用 NEAREST：双线性插值在非零角会模糊笔画，给 0° 引入虚假优势。
- straighten：检测倾斜角（或用手动微调角）并旋转摆正，expand 扩展画布后裁掉白边。
- detect_document_quad：按「边缘背景 vs 中央」判定纸张明暗极性并多阈值二值化
  -> 最大连通域 -> Moore 边界跟踪 -> 凸包 -> DP/边直线拟合求交取 4 角；
  按 TL/TR/BR/BL 排序，附置信度，检测失败给兜底四边形。
- perspective_correct：由 4 个源角点解 8 参数单应（dst->src），
  调 PIL PERSPECTIVE 把梯形拉回正矩形。
"""
import math

from PIL import Image, ImageDraw, ImageFilter

from . import util


# ---------------------------------------------------------------------------
# 倾斜角检测（投影轮廓法）
# ---------------------------------------------------------------------------
SKEW_COARSE_RANGE = 45          # 粗搜角度范围 ±（度）
SKEW_COARSE_STEP = 2
SKEW_FINE_HALF = 4              # 精搜围绕粗峰 ±（度）
SKEW_WORK_DIM = 260             # 检测用最长边（越小越快）
SKEW_MIN_DARK_RATIO = 0.004     # 前景像素占比低于此值认为没有可检测的结构


def _dark_stroke_mask(gray, dark_gap=60):
    """暗笔画掩码（文字、结构线条）。背景取 95 分位灰度，暗于背景 dark_gap 的为前景。"""
    vals = sorted(gray.getdata())
    bg = vals[int(len(vals) * 0.95)]
    return gray.point(lambda v: 0 if v < bg - dark_gap else 255)


def _edge_stroke_mask(gray, abs_thresh=45, min_frac=0.02):
    """强边缘掩码（建筑/低对比场景）。取梯度幅值超过绝对阈值的像素。

    用绝对阈值而非分位数，这样真正无结构的空白图会得到空掩码；
    若边缘像素占比低于 min_frac，也视为无内容。
    """
    mag = util.gradient_magnitude(gray, "sobel")
    bw = mag.point(lambda v: 0 if v > abs_thresh else 255)
    frac = sum(1 for p in bw.getdata() if p == 0) / float(bw.width * bw.height)
    if frac < min_frac:
        return Image.new("L", bw.size, 255)
    return bw


def _projection_mask(gray):
    """为投影轮廓法选择二值掩码。

    文档场景用「暗笔画」（文字行形成清晰行投影）；
    若暗像素过少（建筑、低对比结构照），回退到强边缘掩码。
    返回 (掩码, 来源标签)。
    """
    bw = _dark_stroke_mask(gray)
    dark = sum(1 for p in bw.getdata() if p == 0)
    if dark / float(bw.width * bw.height) >= SKEW_MIN_DARK_RATIO:
        return bw, "dark"
    return _edge_stroke_mask(gray), "edge"


def _profile_energy(bw, angle):
    """把二值图（文字黑/底白）旋转 angle 后，行投影的相邻行差分能量。

    内容越接近水平，文字行在投影上形成越尖锐的峰谷，差分能量越大。
    NEAREST 重采样对所有角度一视同仁，避免插值模糊带来的角度偏置。
    """
    r = bw.rotate(angle, resample=Image.Resampling.NEAREST,
                  fillcolor=255, expand=True)
    w, h = r.size
    px = list(r.getdata())
    prof = [255 * w - sum(px[y * w:(y + 1) * w]) for y in range(h)]
    return sum((prof[i] - prof[i - 1]) ** 2 for i in range(1, h))


def detect_skew(image, params=None):
    """检测图像倾斜并返回「摆正所需旋转角」（度）。

    返回 dict：angle=传给 rotate 的摆正角（与倾斜角大小相等符号相反），
    tilt=检测到的内容倾斜角，confidence ∈ [0,1]。
    """
    params = params or {}
    work_dim = int(params.get("work_dim", SKEW_WORK_DIM))
    coarse_range = int(params.get("max_angle", SKEW_COARSE_RANGE))

    work = util.downscale_to_max(util.ensure_rgb(image), work_dim)
    gray = util.to_grayscale(work)
    bw, mask_kind = _projection_mask(gray)

    dark = sum(1 for p in bw.getdata() if p == 0)
    ratio = dark / float(bw.width * bw.height)

    # 粗搜
    coarse_angles = list(range(-coarse_range, coarse_range + 1, SKEW_COARSE_STEP))
    coarse_scores = [(a, _profile_energy(bw, a)) for a in coarse_angles]
    best_coarse = max(coarse_scores, key=lambda t: t[1])[0]

    # 精搜
    lo = int((best_coarse - SKEW_FINE_HALF) * 10)
    hi = int((best_coarse + SKEW_FINE_HALF) * 10)
    fine = [(a / 10.0, _profile_energy(bw, a / 10.0)) for a in range(lo, hi + 1)]
    correct_angle, peak = max(fine, key=lambda t: t[1])

    # 置信度：峰顶相对 0° 的提升归一化；前景过少直接给 0
    base = _profile_energy(bw, 0)
    lift = peak / max(base, 1) - 1.0
    confidence = 0.0 if ratio < SKEW_MIN_DARK_RATIO else min(1.0, max(0.0, lift / 2.0))
    # 角度太小视为本来就正
    if abs(correct_angle) < 0.15:
        correct_angle = 0.0

    # correct_angle 是「摆正所需旋转角」，直接可用；tilt 为内容倾斜角
    return {
        "angle": round(correct_angle, 2),
        "tilt": round(-correct_angle, 2),
        "confidence": round(confidence, 3),
        "dark_ratio": round(ratio, 4),
        "mask": mask_kind,
    }


def straighten(image, params=None):
    """自动摆正：检测倾斜角 -> 旋转 -> 裁掉旋转引入的白边。"""
    params = params or {}
    angle_override = params.get("angle")
    auto = params.get("auto", True)
    # 手动微调：auto=False 且显式给了 angle；或非节点调用直接传 angle
    if angle_override is not None and (not auto or "auto" not in params):
        info = {"angle": float(angle_override), "tilt": -float(angle_override),
                "confidence": None, "detected": False}
    else:
        info = detect_skew(image, params)

    angle = info["angle"]
    rgb = util.ensure_rgb(image)
    if abs(angle) < 1e-6:
        out = rgb
    else:
        rotated = rgb.rotate(angle, resample=Image.Resampling.BICUBIC,
                             expand=True, fillcolor=(255, 255, 255))
        if params.get("trim", True):
            out = trim_white_border(rotated)
        else:
            out = rotated
    return {"image": out, "angle": angle, "tilt": info["tilt"],
            "confidence": info["confidence"],
            "width": out.size[0], "height": out.size[1]}


def trim_white_border(image, thresh=245, pad=2):
    """裁掉旋转后四角的近白边（基于内容外接框），pad 保留少量边距。"""
    rgb = util.ensure_rgb(image)
    gray = util.to_grayscale(rgb)
    w, h = gray.size
    px = gray.load()
    xs, ys = [], []
    step = 2  # 隔行隔列扫描加速
    for y in range(0, h, step):
        for x in range(0, w, step):
            if px[x, y] < thresh:
                xs.append(x)
                ys.append(y)
    if not xs:
        return rgb
    x0, x1 = max(0, min(xs) - pad), min(w, max(xs) + pad)
    y0, y1 = max(0, min(ys) - pad), min(h, max(ys) + pad)
    if x1 - x0 < 10 or y1 - y0 < 10:
        return rgb
    return rgb.crop((x0, y0, x1, y1))


# ---------------------------------------------------------------------------
# 文档四边形检测
# ---------------------------------------------------------------------------
QUAD_WORK_DIM = 520


def _largest_component_mask(fg, w, h):
    """在二值掩码（255=前景）上找最大 4-连通域，返回同尺寸二值图。"""
    rows = [[1 if fg.getpixel((x, y)) >= 128 else 0 for x in range(w)] for y in range(h)]
    labels = [[0] * w for _ in range(h)]
    best_label, best = 0, []
    label = 0
    for y in range(h):
        for x in range(w):
            if rows[y][x] and labels[y][x] == 0:
                label += 1
                stack = [(x, y)]
                labels[y][x] = label
                pts = []
                while stack:
                    cx, cy = stack.pop()
                    pts.append((cx, cy))
                    for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                        if 0 <= nx < w and 0 <= ny < h and rows[ny][nx] and labels[ny][nx] == 0:
                            labels[ny][nx] = label
                            stack.append((nx, ny))
                if len(pts) > len(best):
                    best_label, best = label, pts
    out = Image.new("L", (w, h), 0)
    if best:
        out.putdata([255 if labels[y][x] == best_label else 0 for y in range(h) for x in range(w)])
    return out, best


def _moore_boundary(mask, w, h):
    """Moore 邻域跟踪提取最大连通域的外轮廓（顺时针）。"""
    # 找最上、再最左的前景点作为起点
    start = None
    for y in range(h):
        for x in range(w):
            if mask.getpixel((x, y)) >= 128:
                start = (x, y)
                break
        if start:
            break
    if not start:
        return []

    # 8 邻域，顺时针方向表：从「正左」开始
    nbrs = [(0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1)]
    boundary = [start]
    cur = start
    back_dir = 6  # 进入起点的反方向（起点上方而来 -> 朝向下方 nbr index 4；初始试探从左邻开始）
    # 初始：上一步「来自左方」，故先检查的方向索引
    check_dir = 0
    first = True
    for _ in range(w * h):
        found = None
        for k in range(8):
            d = (check_dir + k) % 8
            dx, dy = nbrs[d]
            nx, ny = cur[0] + dx, cur[1] + dy
            if 0 <= nx < w and 0 <= ny < h and mask.getpixel((nx, ny)) >= 128:
                found = ((nx, ny), d)
                break
        if not found:
            break
        nxt, d = found
        # 下一步从进入方向的反方向的顺时针下一个邻域开始找
        check_dir = (d + 6) % 8  # 反方向再退一格（Jacobi 扫描）
        if not first and nxt == start:
            break
        boundary.append(nxt)
        cur = nxt
        first = False
    return boundary


def _douglas_peucker(points, epsilon):
    """Ramer-Douglas-Peucker 多边形逼近。"""
    if len(points) < 3:
        return points[:]

    def perp_dist(p, a, b):
        if a == b:
            return math.hypot(p[0] - a[0], p[1] - a[1])
        dx, dy = b[0] - a[0], b[1] - a[1]
        return abs(dy * p[0] - dx * p[1] + b[0] * a[1] - b[1] * a[0]) / math.hypot(dx, dy)

    def recurse(lo, hi):
        if hi <= lo + 1:
            return
        a, b = points[lo], points[hi]
        idx, dmax = -1, 0.0
        for i in range(lo + 1, hi):
            d = perp_dist(points[i], a, b)
            if d > dmax:
                dmax, idx = d, i
        if dmax > epsilon:
            recurse(lo, idx)
            keep.add(idx)
            recurse(idx, hi)

    keep = {0, len(points) - 1}
    recurse(0, len(points) - 1)
    return [points[i] for i in sorted(keep)]


def _convex_hull(points):
    """Andrew 单调链凸包，去除数字边界锯齿，返回逆时针凸多边形。"""
    pts = sorted(set(points))
    if len(pts) <= 1:
        return pts

    def cross(o, a, b):
        return ((a[0] - o[0]) * (b[1] - o[1])
                - (a[1] - o[1]) * (b[0] - o[0]))

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _dedup_close(points, min_dist):
    """合并凸包上距离过近的相邻顶点（下采样抗锯齿会在同一角点产生一簇点）。"""
    if not points:
        return points
    out = [points[0]]
    for p in points[1:]:
        if math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) >= min_dist:
            out.append(p)
    if len(out) > 1 and math.hypot(out[0][0] - out[-1][0],
                                   out[0][1] - out[-1][1]) < min_dist:
        out.pop()
    return out


def _line_intersection(p1, p2, p3, p4):
    """两条无限直线交点；平行返回 None。"""
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    x4, y4 = p4
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) < 1e-9:
        return None
    px = ((x1 * y2 - y1 * x2) * (x3 - x4) - (x1 - x2) * (x3 * y4 - y3 * x4)) / den
    py = ((x1 * y2 - y1 * x2) * (y3 - y4) - (y1 - y2) * (x3 * y4 - y3 * x4)) / den
    return px, py


def _fit_line(pts):
    """最小二乘直线方向（PCA），返回 (mx,my,theta)。"""
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    syy = sum((p[1] - my) ** 2 for p in pts)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
    theta = 0.5 * math.atan2(2 * sxy, sxx - syy)
    return mx, my, theta


def _refine_corners(hull, anchors):
    """用 4 个锚点把凸包边界分成 4 段，每段拟合直线，相邻直线求交得到精确角点。

    下采样抗锯齿会让真实角点附近出现一簇近共线点，DP 容易选偏；
    而对整条边做直线拟合再求交，能把角点定位到几何真实交点。
    anchors 为凸包上按环序排列的 4 个顶点索引。
    """
    n = len(hull)
    anchors = sorted(anchors)
    lines = []
    for k in range(4):
        i0, i1 = anchors[k], anchors[(k + 1) % 4]
        seg = []
        j = i0
        while True:
            seg.append(hull[j])
            if j == i1:
                break
            j = (j + 1) % n
        lines.append(_fit_line(seg))

    def line_pts(line, t=1000.0):
        mx, my, theta = line
        return (mx + math.cos(theta) * t, my + math.sin(theta) * t), \
               (mx - math.cos(theta) * t, my - math.sin(theta) * t)

    corners = []
    for k in range(4):
        a1, a2 = line_pts(lines[(k - 1) % 4])
        b1, b2 = line_pts(lines[k])
        ip = _line_intersection(a1, a2, b1, b2)
        corners.append(ip if ip is not None else hull[anchors[k]])
    return corners


def _corner_anchors(hull):
    """在凸包顶点中找 4 个内角最小（转折最强）且在环上彼此分开的点，返回环序索引。"""
    n = len(hull)
    if n == 4:
        return [0, 1, 2, 3]
    angles = []
    for i in range(n):
        a, b, c = hull[(i - 1) % n], hull[i], hull[(i + 1) % n]
        v1 = (a[0] - b[0], a[1] - b[1])
        v2 = (c[0] - b[0], c[1] - b[1])
        denom = math.hypot(*v1) * math.hypot(*v2)
        cosv = max(-1, min(1, (v1[0] * v2[0] + v1[1] * v2[1]) / max(denom, 1e-9)))
        angles.append((math.degrees(math.acos(cosv)), i))
    gap = max(2, n // 10)
    chosen = []
    for _ang, i in sorted(angles):
        if all(min(abs(i - j), n - abs(i - j)) >= gap for j in chosen):
            chosen.append(i)
        if len(chosen) == 4:
            break
    return sorted(chosen) if len(chosen) == 4 else None


def _simplify_to_quad(boundary):
    """边界 -> 凸包 -> 近点合并 -> 四边形角点。失败返回 None。

    两级策略：
    1. DP 多边形逼近，若直接得到 4 个「内角合理」的顶点则采用
       （角点本身就清晰时最准，如规整矩形/标准透视）；
    2. 否则用「内角锚点 + 边最小二乘拟合求交」，对抗锯齿/轻微缺边更稳健。
    """
    hull = _convex_hull(boundary)
    if len(hull) < 4:
        return None
    peri = sum(math.hypot(hull[i][0] - hull[i - 1][0],
                          hull[i][1] - hull[i - 1][1])
               for i in range(len(hull)))
    hull = _dedup_close(hull, peri * 0.012)
    if len(hull) < 4:
        return None

    def angles_ok(quad, min_angle=35, max_angle=150):
        for i in range(4):
            a, b, c = quad[(i - 1) % 4], quad[i], quad[(i + 1) % 4]
            v1 = (a[0] - b[0], a[1] - b[1])
            v2 = (c[0] - b[0], c[1] - b[1])
            denom = math.hypot(*v1) * math.hypot(*v2)
            if denom < 1e-9:
                return False
            cosv = max(-1, min(1, (v1[0] * v2[0] + v1[1] * v2[1]) / denom))
            ang = math.degrees(math.acos(cosv))
            if not (min_angle <= ang <= max_angle):
                return False
        return True

    # 1) DP：从严到宽找第一组 4 点且内角合理
    base_eps = max(2.0, peri * 0.012)
    for mul in (0.8, 1.4, 2.2, 3.5, 5.5):
        poly = _douglas_peucker(hull, base_eps * mul)
        if len(poly) == 4 and angles_ok(poly):
            return poly

    # 2) 内角锚点 + 边直线拟合求交
    anchors = _corner_anchors(hull)
    if anchors is not None:
        refined = _refine_corners(hull, anchors)
        if angles_ok(refined, min_angle=25, max_angle=160):
            return refined

    # 3) 放宽 DP 内角限制
    for mul in (8.0, 14.0):
        poly = _douglas_peucker(hull, base_eps * mul)
        if len(poly) == 4:
            return poly
    if len(hull) >= 4:
        return _four_from_polygon(hull)
    return None


def _fallback_quad(w, h, margin=0.06):
    """检测失败时的兜底：居中内缩矩形。"""
    mx, my = w * margin, h * margin
    return [(mx, my), (w - mx, my), (w - mx, h - my), (mx, h - my)]


def _order_quads(quad):
    """把 4 个角点排序为 TL, TR, BR, BL。

    对任意凸四边形稳健：先按相对质心的方位角排成环形顺序，
    再选 x+y 最小的点为 TL，沿环向后（屏幕坐标 y 向下的顺时针）取 TR/BR/BL。
    """
    cx = sum(p[0] for p in quad) / 4.0
    cy = sum(p[1] for p in quad) / 4.0
    # 屏幕坐标 y 向下：atan2(y-cy, x-cx) 从最小角升序，TL(-,-) 起依次为 TL,TR,BR,BL
    ring = sorted(quad, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))
    tl_idx = min(range(4), key=lambda i: ring[i][0] + ring[i][1])
    tl = ring[tl_idx]
    tr = ring[(tl_idx + 1) % 4]
    br = ring[(tl_idx + 2) % 4]
    bl = ring[(tl_idx + 3) % 4]
    return [(float(tl[0]), float(tl[1])), (float(tr[0]), float(tr[1])),
            (float(br[0]), float(br[1])), (float(bl[0]), float(bl[1]))]


def detect_document_quad(image, params=None):
    """检测文档/纸张的四个角点。

    返回 dict：quad=[[x,y]×4]（TL/TR/BR/BL，工作副本坐标经比例映射回原图），
    normalized 为 0..1 归一化坐标，confidence ∈[0,1]，image 为角点叠加图。
    """
    params = params or {}
    orig = util.ensure_rgb(image)
    work = util.downscale_to_max(orig, QUAD_WORK_DIM)
    w, h = work.size
    ratio_x = orig.width / float(w)
    ratio_y = orig.height / float(h)

    gray = util.to_grayscale(work)
    # 前景：文档在桌面/背景上通常更亮，但有时也更暗。选「与边缘背景色差大」的区域：
    # 用 95/5 分位判定极性，再 Otsu 二值化（亮纸取反）。
    vals = sorted(gray.getdata())
    v95 = vals[int(len(vals) * 0.95)]
    # 边缘一圈的中位亮度作为背景估计
    border = [gray.getpixel((x, y)) for x in range(0, w, 4) for y in (list(range(0, 8)) + list(range(h - 8, h)))]
    border += [gray.getpixel((x, y)) for y in range(0, h, 4) for x in (list(range(0, 8)) + list(range(w - 8, w)))]
    bg = sorted(border)[len(border) // 2] if border else v95
    center = sorted(gray.getpixel((x, y)) for x in range(w // 4, 3 * w // 4, 3)
                    for y in range(h // 4, 3 * h // 4, 3))
    center_med = center[len(center) // 2] if center else bg
    paper_bright = center_med >= bg  # 中央比边缘亮 => 亮纸暗背景

    # 多阈值尝试：下采样抗锯齿会让纸边出现半透明灰像素，单一阈值可能丢边。
    # 从严到宽尝试几个对比差，选能得到「合理四边形」且置信度最高的。
    best = None  # (confidence, quad, area_ratio, fg)
    for gap in (35, 20, 10):
        if paper_bright:
            fg = gray.point(lambda v, g=gap: 255 if v > bg + g else 0)
        else:
            fg = gray.point(lambda v, g=gap: 255 if v < bg - g else 0)
        fg = fg.filter(ImageFilter.MaxFilter(3))
        mask, comp = _largest_component_mask(fg, w, h)
        area_ratio = len(comp) / float(w * h)
        if len(comp) < 40:
            continue
        boundary = _moore_boundary(mask, w, h)
        if len(boundary) < 8:
            continue
        cand = _simplify_to_quad(boundary)
        if cand and _is_reasonable_quad(cand, w, h, area_ratio):
            conf = _quad_confidence(cand, w, h, area_ratio)
            if best is None or conf > best[0]:
                best = (conf, cand, area_ratio, fg)
        # 已得到高置信结果则不再放宽（阈值越宽越易把背景也吞进来）
        if best and best[0] >= 0.85:
            break

    if best:
        confidence, quad, area_ratio, _fg = best
        method = "boundary"
    else:
        # 用最严阈值的连通域占比仅用于兜底提示
        fg0 = gray.point(lambda v: 255 if (v > bg + 35 if paper_bright else v < bg - 35) else 0)
        _, comp0 = _largest_component_mask(fg0, w, h)
        quad = _fallback_quad(w, h)
        confidence = 0.15 if len(comp0) >= 40 else 0.05
        method = "fallback"

    ordered = _order_quads(quad)

    quad_orig = [[round(x * ratio_x, 1), round(y * ratio_y, 1)] for x, y in ordered]
    norm = [[round(x / w, 4), round(y / h, 4)] for x, y in ordered]
    overlay = _draw_quad_overlay(work, ordered)
    return {
        "quad": quad_orig,
        "normalized": norm,
        "confidence": round(confidence, 3),
        "method": method,
        "width": orig.width,
        "height": orig.height,
        "image": overlay,
    }


def _four_from_polygon(poly):
    """多边形顶点数 >4 时，按每段弧长均匀聚合为 4 点（取各段端点）。"""
    # 简化策略：取 x+y 最小、y-x 最小、x+y 最大、x-y 最大的四个顶点
    tl = min(poly, key=lambda p: p[0] + p[1])
    br = max(poly, key=lambda p: p[0] + p[1])
    rest = [p for p in poly if p != tl and p != br]
    tr = min(rest, key=lambda p: p[1] - p[0])
    bl = max(rest, key=lambda p: p[1] - p[0])
    return [tl, tr, br, bl]


def _is_reasonable_quad(quad, w, h, area_ratio):
    """四边形有效性：面积不能太小/太大、各边不能退化为点。"""
    if area_ratio < 0.03 or area_ratio > 0.98:
        return False
    edges = []
    for i in range(4):
        a, b = quad[i], quad[(i + 1) % 4]
        edges.append(math.hypot(b[0] - a[0], b[1] - a[1]))
    if min(edges) < min(w, h) * 0.12:
        return False
    return True


def _polygon_area(quad):
    return abs(sum(quad[i][0] * quad[(i + 1) % 4][1]
                   - quad[(i + 1) % 4][0] * quad[i][1] for i in range(4))) / 2.0


def _quad_confidence(quad, w, h, area_ratio):
    area = _polygon_area(quad) / float(w * h)
    # 理想文档占画面 25%~90%
    area_score = 1.0 if 0.2 <= area <= 0.92 else max(0.2, 1.0 - abs(area - 0.5))
    # 对边长度比接近 1
    def d(a, b):
        return math.hypot(b[0] - a[0], b[1] - a[1])
    top, right, bottom, left = (d(quad[0], quad[1]), d(quad[1], quad[2]),
                                d(quad[2], quad[3]), d(quad[3], quad[0]))
    ratio_h = min(top, bottom) / max(top, bottom, 1e-6)
    ratio_v = min(left, right) / max(left, right, 1e-6)
    shape = (ratio_h + ratio_v) / 2.0
    return max(0.0, min(1.0, area_score * 0.5 + shape * 0.5))


def _draw_quad_overlay(image, quad):
    img = util.ensure_rgb(image).copy()
    draw = ImageDraw.Draw(img)
    pts = [(p[0], p[1]) for p in quad]
    draw.line(pts + [pts[0]], fill=(255, 82, 82), width=max(2, min(img.size) // 160))
    for i, (x, y) in enumerate(pts):
        r = max(5, min(img.size) // 70)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(255, 193, 7), outline=(255, 255, 255))
        draw.text((x + r + 2, y - 8), ["TL", "TR", "BR", "BL"][i], fill=(255, 193, 7))
    return img


# ---------------------------------------------------------------------------
# 透视校正
# ---------------------------------------------------------------------------
def _solve_homography(src, dst):
    """解 dst->src 的 8 参数单应矩阵（供 Image.transform(PERSPECTIVE) 使用）。

    返回 PIL 要求的 8 系数 [a,b,c,d,e,f,g,h]，使
      x_src = (a x + b y + c)/(g x + h y + 1)
      y_src = (d x + e y + f)/(g x + h y + 1)
    其中 (x,y) 为目标（矩形）坐标。
    """
    rows = []
    for (sx, sy), (dx, dy) in zip(src, dst):
        rows.append([dx, dy, 1, 0, 0, 0, -sx * dx, -sx * dy, sx])
        rows.append([0, 0, 0, dx, dy, 1, -sy * dx, -sy * dy, sy])
    mat = [r[:] for r in rows]
    n = 8
    # 高斯消元（列主元）
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(mat[r][col]))
        mat[col], mat[piv] = mat[piv], mat[col]
        pv = mat[col][col]
        if abs(pv) < 1e-12:
            raise ValueError("退化的角点配置，无法求解单应")
        for j in range(col, n + 1):
            mat[col][j] /= pv
        for r in range(n):
            if r == col:
                continue
            factor = mat[r][col]
            for j in range(col, n + 1):
                mat[r][j] -= factor * mat[col][j]
    return [mat[i][n] for i in range(n)]


def perspective_correct(image, params):
    """按四角点做透视校正，把梯形拉回正矩形。

    params: corners=[[x,y]×4]（TL/TR/BR/BL，原图像素坐标或归一化坐标，由 normalized 指定），
    normalized=True 时坐标 ∈ [0,1]；可选 target_aspect（宽/高），缺省按平均边长推断。
    """
    rgb = util.ensure_rgb(image)
    w, h = rgb.size
    raw = params.get("corners")
    if isinstance(raw, str):
        import json
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raise ValueError("corners 不是合法的 JSON")
    if not raw or len(raw) != 4:
        raise ValueError("需要 4 个角点 corners=[[x,y]×4]")
    if params.get("normalized", False):
        src = [(float(p[0]) * w, float(p[1]) * h) for p in raw]
    else:
        src = [(float(p[0]), float(p[1])) for p in raw]

    def edge(a, b):
        return math.hypot(b[0] - a[0], b[1] - a[1])

    top = edge(src[0], src[1])
    right = edge(src[1], src[2])
    bottom = edge(src[2], src[3])
    left = edge(src[3], src[0])
    out_w = max(1, int(round((top + bottom) / 2.0)))
    out_h = max(1, int(round((left + right) / 2.0)))
    aspect = params.get("target_aspect")
    if aspect:
        aspect = float(aspect)
        if aspect >= 1:
            out_h = max(1, int(round(out_w / aspect)))
        else:
            out_w = max(1, int(round(out_h * aspect)))

    dst = [(0, 0), (out_w, 0), (out_w, out_h), (0, out_h)]
    coeffs = _solve_homography(src, dst)
    out = rgb.transform((out_w, out_h), Image.Transform.PERSPECTIVE, coeffs,
                        Image.Resampling.BICUBIC, fillcolor=(255, 255, 255))
    if params.get("trim", True):
        out = trim_white_border(out)
    return {"image": out, "out_width": out.size[0], "out_height": out.size[1]}
