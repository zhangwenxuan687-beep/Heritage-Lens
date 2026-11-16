# -*- coding: utf-8 -*-
"""
第 2–4 步 · 一次性离线批量正立面校正（OpenCV）。不是常驻服务，跑完即退出。

依赖（本机当前没有，需用户自行一次性安装；不要装在 C 盘）：
    python -m pip install --target "D:\\SRY-SkillS\\cvlibs" opencv-python-headless numpy
    set PYTHONPATH=D:\\SRY-SkillS\\cvlibs
  或（不介意 C 盘占用时）：python -m pip install opencv-python-headless numpy

设计：
  * 主模式 --corners-dir：读取前端「正立面校正」导出的 *_rect.json，
    对同名原图重放同一变换（同样的 EXIF 转正 / 缩放 / k1 / 四点单应），
    结果可复现、可回溯；输出 rectified/<name>_rect.jpg + 同名 .json。
  * 辅助模式 --auto：LSD 线段 + 两消失点估计候选四角（实验性），
    结果一律标 status="待定 · 实验性"，必须人工到前端工具复核，不直接当成果。
  * 一律不改原图；不做生成式补图，图外区域填白。
  * 斜拍、遮挡、拉伸过度的样本按规则标「待定」，不产出可用的校正图结论。

用法：
    python "04-正立面/batch_rectify.py" --dry-run
    python "04-正立面/batch_rectify.py" --corners-dir corners
    python "04-正立面/batch_rectify.py" --image "01-前期资料/调研图片/.../DSCF2559.JPG" --corners corners/xxx_rect.json
    python "04-正立面/batch_rectify.py" --auto --index 12,45,78 --dry-run
    python "04-正立面/batch_rectify.py" --self-test
"""
import argparse, datetime, json, math, os, random, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
OUT = HERE / 'rectified'
INDEX = HERE / 'file_index.json'

cv2 = None
np = None


def ensure_deps():
    """延迟加载：保证 --help / 文档查看在没装 cv2 的机器上也能用。"""
    global cv2, np
    if cv2 is not None:
        return
    try:
        import cv2 as _cv2
        import numpy as _np
        cv2, np = _cv2, _np
    except ImportError as e:
        sys.exit('缺少依赖：%s\n请先一次性安装（见本文件头部注释）：\n'
                 '  python -m pip install --target "D:\\SRY-SkillS\\cvlibs" opencv-python-headless numpy\n'
                 '  set PYTHONPATH=D:\\SRY-SkillS\\cvlibs' % e)


def log(*a):
    print(*a, flush=True)


def apply_orientation(img, ori):
    """与前端 rcApplyOrientation 的 setTransform 等价。"""
    if ori == 2:
        return cv2.flip(img, 1)
    if ori == 3:
        return cv2.flip(img, -1)
    if ori == 4:
        return cv2.flip(img, 0)
    if ori == 5:
        return cv2.transpose(img)
    if ori == 6:
        return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    if ori == 7:
        return cv2.flip(cv2.transpose(img), -1)
    if ori == 8:
        return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return img


def load_index():
    if not INDEX.exists():
        sys.exit('找不到 file_index.json，请先跑 build_index.py')
    d = json.loads(INDEX.read_text(encoding='utf-8'))
    return d['items']


def find_original(name, items):
    hits = [x for x in items if x['name'] == name]
    if not hits:
        hits = [x for x in items if Path(x['name']).stem == Path(name).stem]
    if not hits:
        return None
    if len(hits) > 1:
        log('  注意：文件名 %s 在清单里有 %d 条，取第一条（%s）'
            % (name, len(hits), hits[0]['rel']))
    return ROOT / hits[0]['rel']


def angle_between(a, b):
    v1 = (a[0] - b[0], a[1] - b[1])
    v2 = (a[2] - b[2], a[3] - b[3])
    n1, n2 = math.hypot(*v1), math.hypot(*v2)
    if n1 < 1e-6 or n2 < 1e-6:
        return 0.0
    c = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
    return math.degrees(math.acos(c))


def judge(corners, W, H):
    """规则判定，不做生成式补图；返回 (status, reasons)。"""
    p = [tuple(c) for c in corners]
    top, bottom = math.dist(p[0], p[1]), math.dist(p[3], p[2])
    left, right = math.dist(p[0], p[3]), math.dist(p[1], p[2])
    reasons = []
    if min(top, bottom) < 1e-6 or min(left, right) < 1e-6:
        return '待定 · 角点退化', ['边长接近 0']
    r_h, r_v = top / bottom, left / right
    if r_h < 0.5 or r_h > 2.0:
        reasons.append('上下边长度比 %.2f（斜拍拉伸严重）' % r_h)
    if r_v < 0.5 or r_v > 2.0:
        reasons.append('左右边长度比 %.2f（斜拍拉伸严重）' % r_v)
    # 四角是否接近矩形：相邻边夹角偏离 90° 的程度
    devs = []
    for i in range(4):
        a, b, c = p[i], p[(i + 1) % 4], p[(i - 1) % 4]
        v1 = (b[0] - a[0], b[1] - a[1])
        v2 = (c[0] - a[0], c[1] - a[1])
        n1, n2 = math.hypot(*v1), math.hypot(*v2)
        if n1 < 1e-6 or n2 < 1e-6:
            devs.append(90)
            continue
        cosv = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
        devs.append(abs(90 - math.degrees(math.acos(max(-1, min(1, cosv))))))
    if max(devs) > 45:
        reasons.append('四边形内角偏离直角最大 %.1f°' % max(devs))
    Hm = cv2.getPerspectiveTransform(np.float32(corners),
                                    np.float32([[0, 0], [W - 1, 0], [W - 1, H - 1], [0, H - 1]]))
    cond = float(np.linalg.cond(Hm))
    if cond > 1e7:
        reasons.append('单应矩阵条件数 %.2e（数值不稳定）' % cond)
    return ('待定 · 需人工复核' if reasons else '可用'), reasons


def rectify(src_path, meta, out_dir, k1_override=None, dry=False):
    name = meta.get('source_file') or src_path.name
    corners = meta.get('corners_px')
    if not corners or len(corners) != 4:
        log('  跳过 %s：JSON 缺少 4 个角点' % name)
        return None
    W, H = meta.get('output_size', [0, 0])
    if not W or not H:
        log('  跳过 %s：JSON 缺少 output_size' % name)
        return None
    ori = int(meta.get('exif_orientation', 1) or 1)
    work = tuple(meta.get('working_image_size') or [0, 0])
    k1 = float(k1_override if k1_override is not None else meta.get('lens_k1', 0) or 0)
    status, reasons = judge(corners, W, H)

    if dry:
        log('  [dry-run] %s -> %dx%d  k1=%.4f  ori=%d  状态=%s %s'
            % (name, W, H, k1, ori, status, '; '.join(reasons)))
        return dict(name=name, status=status)

    img = cv2.imread(str(src_path), cv2.IMREAD_COLOR)
    if img is None:
        log('  跳过 %s：无法读取（可能不是有效图片）' % name)
        return None
    img = apply_orientation(img, ori)
    if work[0] and work[1] and (img.shape[1], img.shape[0]) != work:
        img = cv2.resize(img, work, interpolation=cv2.INTER_AREA)

    # 与前端完全一致：目标像素 -> Hinv -> 源坐标 -> 可选径向 k1 -> 双线性采样
    Hm = cv2.getPerspectiveTransform(np.float32(corners),
                                    np.float32([[0, 0], [W - 1, 0], [W - 1, H - 1], [0, H - 1]]))
    Hinv = np.linalg.inv(Hm)
    u, v = np.meshgrid(np.arange(W, dtype=np.float32), np.arange(H, dtype=np.float32))
    a = Hinv[0, 0] * u + Hinv[0, 1] * v + Hinv[0, 2]
    b = Hinv[1, 0] * u + Hinv[1, 1] * v + Hinv[1, 2]
    c = Hinv[2, 0] * u + Hinv[2, 1] * v + Hinv[2, 2]
    c = np.where(np.abs(c) < 1e-9, 1e-9, c)
    mx, my = a / c, b / c
    if k1:
        cx, cy, hx, hy = img.shape[1] / 2, img.shape[0] / 2, img.shape[1] / 2, img.shape[0] / 2
        nx, ny = (mx - cx) / hx, (my - cy) / hy
        f = 1 + k1 * (nx * nx + ny * ny)
        mx, my = cx + nx * f * hx, cy + ny * f * hy
    out = cv2.remap(img, mx, my, cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))

    # 图外白色占比过高 -> 说明原图覆盖面不足，标待定
    white = float(np.mean(np.all(out > 248, axis=2)))
    if white > 0.25:
        status = '待定 · 需人工复核'
        reasons.append('图外白色填充占比 %.1f%%（原图覆盖不足或大面积遮挡）' % (white * 100))

    base = Path(name).stem + '_rect'
    out_dir.mkdir(parents=True, exist_ok=True)
    jpg = out_dir / (base + '.jpg')
    cv2.imwrite(str(jpg), out, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    rec = dict(meta)
    rec.update({
        'offline_batch': {
            'tool': 'batch_rectify.py', 'executed_at': datetime.datetime.now().isoformat(timespec='seconds'),
            'source_path': str(src_path.relative_to(ROOT)).replace('\\', '/'),
            'k1_applied': k1, 'outside_fill': '#ffffff（白色空白，非补图）',
            'outside_white_ratio': round(white, 4),
            'status': status, 'status_reasons': reasons,
            'homography_cv2_src_to_dst': Hm.tolist(),
            'homography_cv2_dst_to_src': Hinv.tolist(),
            'note': '变换方式与前端工具一致（四点单应 + 逆映射双线性 + 可选 k1）；原图未被修改',
        },
        'academic_note': '校正图仅用于构件标注与展示，不是测量级正射影像；论文方法章节需写明变换方式与参数；不做生成式补图',
    })
    (out_dir / (base + '.json')).write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding='utf-8')
    log('  %s  %dx%d  k1=%.4f  %s  %s'
        % (jpg.name, W, H, k1, status, ('; '.join(reasons)) if reasons else ''))
    return dict(name=name, status=status, out=str(jpg))


def auto_rectifying_homography(img):
    """LSD 线段 → 两个消失点 → 消失线 → 射影(仿射)矫正矩阵。

    H 的第三行取消失线 l，则 l 上的点满足 x'_3 = l·x = 0，即消失线被推到无穷远，
    画面中原本 converges 的水平/垂直线恢复平行。只做仿射矫正、不做度量矫正，
    结果一律标「待定 · 实验性」，必须人工在前端工具里点四角复核。
    """
    h, w = img.shape[:2]
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    raw = cv2.createLineSegmentDetector(cv2.LSD_REFINE_ADV).detect(g)[0]
    if raw is None:
        return None, 'LSD 未检测到线段'
    # OpenCV 4.x 返回 (N,1,4)，5.x 返回 (N,4)，统一成 (N,4)
    lines = np.asarray(raw).reshape(-1, 4)
    if len(lines) < 8:
        return None, 'LSD 线段不足，无法估计消失点'
    horiz, vert = [], []
    for L in lines:
        x1, y1, x2, y2 = map(float, L)
        if math.hypot(x2 - x1, y2 - y1) < min(w, h) * 0.07:
            continue
        ang = abs(math.degrees(math.atan2(y2 - y1, x2 - x1)))
        ang = min(ang, 180 - ang)
        if ang < 25:
            horiz.append((x1, y1, x2, y2))
        elif ang > 65:
            vert.append((x1, y1, x2, y2))
    if len(horiz) < 4 or len(vert) < 4:
        return None, '水平/垂直线段不足（h=%d, v=%d）' % (len(horiz), len(vert))

    def vp(seg):
        """RANSAC 投票求消失点：建筑立面里混入大量装饰线，直接最小二乘会被拉偏。"""
        norm = []
        for x1, y1, x2, y2 in seg:
            a, b, c = y1 - y2, x2 - x1, x1 * y2 - x2 * y1
            n = math.hypot(a, b)
            if n > 1e-9:
                norm.append((a / n, b / n, c / n))
        if len(norm) < 4:
            return None
        rng = random.Random(20261107)
        n = len(norm)
        thresh = max(2.5, min(w, h) * 0.004)
        best, best_inl = None, []
        pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
        rng.shuffle(pairs)
        for i, j in pairs[:400]:
            a1, b1, c1 = norm[i]; a2, b2, c2 = norm[j]
            d = a1 * b2 - a2 * b1
            if abs(d) < 1e-6:
                continue
            px, py = (b1 * c2 - b2 * c1) / d, (c1 * a2 - c2 * a1) / d
            if abs(px) > 1e6 or abs(py) > 1e6:
                continue
            inl = [k for k, (a, b, c) in enumerate(norm) if abs(a * px + b * py + c) < thresh]
            if len(inl) > len(best_inl):
                best_inl, best = inl, (px, py)
        if not best_inl or len(best_inl) < 4:
            return None
        A = np.array([[norm[k][0], norm[k][1]] for k in best_inl])
        y = np.array([-norm[k][2] for k in best_inl])
        v = np.linalg.lstsq(A, y, rcond=None)[0]
        return float(v[0]), float(v[1])

    vh, vv = vp(horiz), vp(vert)
    if not vh or not vv:
        return None, '消失点估计失败'
    if any(abs(c) > 20 * max(w, h) for c in vh + vv):
        return None, '消失点距离异常（近似正视图，无需矫正）'
    l = np.cross(np.array([vh[0], vh[1], 1.0]), np.array([vv[0], vv[1], 1.0]))
    if abs(l[2]) < 1e-9:
        return None, '消失线退化'
    l = l / l[2]
    quad = [(0, 0), (w, 0), (w, h), (0, h)]
    denoms = [l[0] * x + l[1] * y + 1.0 for x, y in quad]
    if min(denoms) < 0.15:
        return None, '消失线穿过画面，估计不可靠'
    H = np.array([[1, 0, 0], [0, 1, 0], [l[0], l[1], 1]], dtype=float)
    moved = []
    for x, y in quad:
        v = H @ np.array([x, y, 1.0])
        moved.append((v[0] / v[2], v[1] / v[2]))
    minx, miny = min(p[0] for p in moved), min(p[1] for p in moved)
    maxx, maxy = max(p[0] for p in moved), max(p[1] for p in moved)
    ow, oh = maxx - minx, maxy - miny
    if not (0.3 * w < ow < 3.0 * w and 0.3 * h < oh < 3.0 * h):
        return None, '矫正后尺度异常（%.0f×%.0f）' % (ow, oh)
    if not (0.35 < ow / oh < 3.0):
        return None, '矫正后长宽比异常（%.0f×%.0f）' % (ow, oh)
    T = np.array([[1, 0, -minx], [0, 1, -miny], [0, 0, 1]], dtype=float)
    Ha = T @ H
    info = dict(mode='vanishing', H=Ha, out_w=int(round(ow)), out_h=int(round(oh)),
                vh=list(vh), vv=list(vv), vanishing_line=l.tolist(),
                lines_h=len(horiz), lines_v=len(vert))
    return info, '消失点仿射矫正 h=(%.0f,%.0f) v=(%.0f,%.0f) → %d×%d' % (vh + vv + (info['out_w'], info['out_h']))


def auto_quad_plan(img):
    """消失点法失败时的回退：用最长的上/下/左/右结构线求交点，得到候选四角。
    全部结果仍标「待定 · 实验性」，必须人工在前端工具里复核四角。"""
    h, w = img.shape[:2]
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # HoughLinesP 比 LSD 更能找到跨越整幅立面的长结构线（可跨 20+ px 断口）
    edges = cv2.Canny(cv2.GaussianBlur(g, (5, 5), 0), 50, 150)
    raw = cv2.HoughLinesP(edges, 1, np.pi / 180,
                          threshold=int(min(w, h) * 0.12),
                          minLineLength=max(w, h) * 0.14,
                          maxLineGap=max(w, h) * 0.035)
    if raw is None:
        return None, 'Hough 未检测到长直线'
    lines = np.asarray(raw).reshape(-1, 4)
    H, V = [], []
    for L in lines:
        x1, y1, x2, y2 = map(float, L)
        ang = abs(math.degrees(math.atan2(y2 - y1, x2 - x1)))
        ang = min(ang, 180 - ang)
        if ang < 28:
            H.append((x1, y1, x2, y2, (y1 + y2) / 2))
        elif ang > 62:
            V.append((x1, y1, x2, y2, (x1 + x2) / 2))
    if len(H) < 2 or len(V) < 2:
        return None, '长结构线不足（h=%d, v=%d）' % (len(H), len(V))
    top, bot = min(H, key=lambda t: t[4]), max(H, key=lambda t: t[4])
    left, right = min(V, key=lambda t: t[4]), max(V, key=lambda t: t[4])

    def inter(A, B):
        x1, y1, x2, y2 = A[:4]; x3, y3, x4, y4 = B[:4]
        d = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
        if abs(d) < 1e-6:
            return None
        return [((x1 * y2 - y1 * x2) * (x3 - x4) - (x1 - x2) * (x3 * y4 - y3 * x4)) / d,
                ((x1 * y2 - y1 * x2) * (y3 - y4) - (y1 - y2) * (x3 * y4 - y3 * x4)) / d]

    corners = [inter(top, left), inter(top, right), inter(bot, right), inter(bot, left)]
    if any(p is None for p in corners):
        return None, '四角交点退化'
    if any(p[0] < -0.3 * w or p[0] > 1.3 * w or p[1] < -0.3 * h or p[1] > 1.3 * h for p in corners):
        return None, '候选四角超出合理范围'
    area = abs(sum(corners[i][0] * corners[(i + 1) % 4][1] -
                   corners[(i + 1) % 4][0] * corners[i][1] for i in range(4))) / 2
    if area < 0.22 * w * h:
        return None, '候选四边形面积过小'
    ow = int(round((math.dist(corners[0], corners[1]) + math.dist(corners[3], corners[2])) / 2))
    oh = int(round((math.dist(corners[0], corners[3]) + math.dist(corners[1], corners[2])) / 2))
    if not (0.3 * w < ow < 2.5 * w and 0.3 * h < oh < 2.5 * h):
        return None, '候选输出尺寸异常'
    st, reasons = judge(corners, ow, oh)
    if st != '可用':
        return None, '候选四角不合理：' + '；'.join(reasons)[:70]
    return dict(mode='quad', corners=corners, out_w=ow, out_h=oh,
                area_ratio=round(area / (w * h), 3)), '自动四角候选 → %d×%d（面积占比 %.0f%%）' % (ow, oh, 100 * area / (w * h))


def load_work_image(src_path, ori, work_side=1600):
    """读原图 → EXIF 转正 → 等比缩到长边 ≤work_side（LSD 提速），保持两处调用完全一致。"""
    img = cv2.imread(str(src_path), cv2.IMREAD_COLOR)
    if img is None:
        return None, 1.0
    img = apply_orientation(img, ori)
    h0, w0 = img.shape[:2]
    sc = min(1.0, work_side / max(h0, w0))
    if sc < 1:
        img = cv2.resize(img, (int(round(w0 * sc)), int(round(h0 * sc))),
                         interpolation=cv2.INTER_AREA)
    return img, sc


def rectify_auto(src_path, it, info, out_dir, dry=False, work_side=1600):
    """自动模式专用输出：与四点手工模式分开，状态一律为待定。"""
    no = it['no']
    ori = int(it['ori'])
    reasons = ['LSD 自动候选，未经人工点四角复核']
    status = '待定 · 实验性 · 需人工复核'
    if dry:
        log('  [dry-run] 编号 %d %s → %s' % (no, it['name'], '仿射矫正' if info else '仅 EXIF 转正'))
        return dict(no=no, status=status)
    img, sc = load_work_image(src_path, ori, work_side)
    if img is None:
        log('  跳过编号 %d：无法读取' % no)
        return None
    Hm = None
    if info:
        ow, oh = info['out_w'], info['out_h']
        if info.get('mode') == 'quad':
            Hm = cv2.getPerspectiveTransform(
                np.float32(info['corners']),
                np.float32([[0, 0], [ow - 1, 0], [ow - 1, oh - 1], [0, oh - 1]]))
            out = cv2.warpPerspective(img, Hm, (ow, oh), flags=cv2.INTER_LINEAR,
                                      borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))
            reasons.append('候选四角由最长结构线交点自动推定，非人工点选')
        else:
            # 消失线法：H 由同一张 work image 求得，无需再做尺度换算
            Hm = info['H']
            out = cv2.warpPerspective(img, Hm, (ow, oh), flags=cv2.INTER_LINEAR,
                                      borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))
        white = float(np.mean(np.all(out > 248, axis=2)))
        if white > 0.35:
            reasons.append('图外白色填充占比 %.1f%%（原图覆盖不足或大面积遮挡）' % (white * 100))
    else:
        out = img
        ow, oh = img.shape[1], img.shape[0]
        white = 0.0
        reasons.append('自动估计不可靠，本图仅做 EXIF 转正 + 等比缩放，未做透视变换')
    base = '%04d_%s' % (no, Path(it['name']).stem)
    out_dir.mkdir(parents=True, exist_ok=True)
    jpg = out_dir / (base + '_rect.jpg')
    cv2.imwrite(str(jpg), out, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    rec = {
        'tool': 'batch_rectify.py --auto (experimental)', 'version': '0.4.2',
        'created': datetime.datetime.now().isoformat(timespec='seconds'),
        'source_no': no, 'source_file': it['name'],
        'source_rel': it['rel'], 'source_pixel_size': [it['w'], it['h']],
        'exif_orientation': ori, 'orientation_applied': ori != 1,
        'working_image_size': [img.shape[1], img.shape[0]], 'downscale_factor': round(sc, 6),
        'auto_mode': info['mode'] if info else 'none',
        'method': ('LSD -> vanishing points -> affine rectification' if info and info['mode'] == 'vanishing'
                   else 'LSD longest structure lines -> auto quad -> 4-point homography' if info
                   else 'EXIF rotation + downscale only (auto estimation unreliable)'),
        'vanishing_point_horizontal': info.get('vh') if info else None,
        'vanishing_point_vertical': info.get('vv') if info else None,
        'vanishing_line': info.get('vanishing_line') if info else None,
        'lsd_lines': [info['lines_h'], info['lines_v']] if info and info['mode'] == 'vanishing' else None,
        'auto_corners_px': info.get('corners') if info and info['mode'] == 'quad' else None,
        'homography_rectifying': Hm.tolist() if Hm is not None else None,
        'output_size': [ow, oh], 'outside_fill': '#ffffff（白色空白，非补图）',
        'outside_white_ratio': round(white, 4),
        'status': status, 'status_reasons': reasons,
        'review_hint': '请在「正立面校正」视图载入本图、手工点四角复核；本图不得直接用于训练或论文',
        'academic_note': '校正图仅用于构件标注与展示，不是测量级正射影像；论文方法章节需写明变换方式与参数；不做生成式补图',
    }
    (out_dir / (base + '_rect.json')).write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding='utf-8')
    log('  %s  %dx%d  %s  %s' % (jpg.name, ow, oh, status, '; '.join(reasons)))
    return dict(no=no, status=status, out=str(jpg))


def self_test():
    img = np.zeros((120, 200, 3), np.uint8)
    img[:] = (235, 220, 200)
    cv2.rectangle(img, (20, 15), (180, 105), (60, 90, 160), -1)
    corners = [[20, 15], [180, 15], [180, 105], [20, 105]]
    W, H = 160, 90
    Hm = cv2.getPerspectiveTransform(np.float32(corners),
                                    np.float32([[0, 0], [W - 1, 0], [W - 1, H - 1], [0, H - 1]]))
    err = 0
    for p, q in zip(corners, [[0, 0], [W - 1, 0], [W - 1, H - 1], [0, H - 1]]):
        v = Hm @ np.array([p[0], p[1], 1.0]); v = v / v[2]
        err = max(err, math.hypot(v[0] - q[0], v[1] - q[1]))
    status, reasons = judge(corners, W, H)
    log('自检：重投影误差 %.8f px，判定=%s %s' % (err, status, reasons))
    assert err < 1e-6 and status == '可用'
    log('自检通过（OpenCV / numpy 可用）')


def main():
    ap = argparse.ArgumentParser(description='台山骑楼正立面批量校正（一次性离线脚本）')
    ap.add_argument('--corners-dir', default='', help='前端导出的 *_rect.json 所在目录')
    ap.add_argument('--image', default='', help='单张模式：原图路径')
    ap.add_argument('--corners', default='', help='单张模式：角点 JSON 路径')
    ap.add_argument('--auto', action='store_true', help='LSD 消失点仿射矫正（实验性，结果一律标待定）')
    ap.add_argument('--index', default='', help='配合 --auto：编号逗号分隔；或 usable50=取 CSV 前 50 张「可用」、usable=全部可用')
    ap.add_argument('--k1', type=float, default=None, help='覆盖 JSON 里的镜头 k1')
    ap.add_argument('--out', default='', help='输出目录，默认 04-正立面/rectified')
    ap.add_argument('--dry-run', action='store_true', help='只列出将处理的内容，不写文件')
    ap.add_argument('--self-test', action='store_true', help='用合成图验证算法')
    a = ap.parse_args()
    ensure_deps()
    if a.self_test:
        return self_test()

    out_dir = Path(a.out) if a.out else OUT
    items = load_index()
    jobs = []
    if a.auto:
        import csv as _csv
        mode = (a.index or '').strip().lower()
        if mode.startswith('usable'):
            col = '人工判定(可用/展板/室内/特写/路面/斜拍/模糊/重复/待定)'
            with open(HERE / '可用正立面清单.csv', encoding='utf-8-sig') as f:
                rows = list(_csv.DictReader(f))
            nums = [int(r['编号']) for r in rows if (r[col] or '').strip() == '可用']
            if mode == 'usable50':
                nums = nums[:50]
            log('按 CSV「人工判定=可用」选出 %d 张' % len(nums))
        else:
            nums = [int(x) for x in (a.index or '').split(',') if x.strip()]
        if not nums:
            sys.exit('--auto 需要 --index 指定编号，或用 --index usable50 取清单前 50 张可用')
        log('自动模式：%d 张 → %s%s' % (len(nums), out_dir, '（dry-run）' if a.dry_run else ''))
        done = 0
        for n in nums:
            if n < 0 or n >= len(items):
                log('  跳过编号 %d：超出清单范围' % n); continue
            it = dict(items[n]); it['no'] = n
            p = ROOT / it['rel']
            if a.dry_run:
                log('  [dry-run] 编号 %d  %s' % (n, it['name']))
                done += 1
                continue
            img, _ = load_work_image(p, int(it['ori']))
            if img is None:
                log('  跳过编号 %d：无法读取 %s' % (n, it['name'])); continue
            info, why = auto_rectifying_homography(img)
            if not info:
                info2, why2 = auto_quad_plan(img)
                why = why + ' ／ 回退：' + why2
                if info2:
                    info = info2
            log('编号 %d：%s' % (n, why))
            r = rectify_auto(p, it, info, out_dir, dry=False)
            if r:
                done += 1
        log('自动模式完成 %d 张。全部标「待定 · 实验性」，请在前端工具里手工点四角复核后再用于标注/论文。' % done)
        return
    elif a.image and a.corners:
        p = Path(a.image)
        if not p.is_absolute():
            p = ROOT / p
        jobs.append((p, json.loads(Path(a.corners).read_text(encoding='utf-8')), None))
    else:
        cd = Path(a.corners_dir or (HERE / 'corners'))
        if not cd.is_absolute():
            cd = ROOT / cd if not cd.exists() else cd
        files = sorted(cd.glob('*_rect.json')) if cd.is_dir() else []
        log('角点 JSON：%d 个（目录 %s）' % (len(files), cd))
        for f in files:
            meta = json.loads(f.read_text(encoding='utf-8'))
            p = find_original(meta.get('source_file', ''), items)
            if not p or not p.exists():
                log('  跳过 %s：在清单里找不到原图，请用 --image/--corners 单张模式指定' % f.name)
                continue
            jobs.append((p, meta, None))

    if not jobs:
        log('没有可处理的任务。前端导出角点后把 *_rect.json 放进 corners/ 即可批量跑。')
        return
    log('共 %d 项，输出目录 %s%s' % (len(jobs), out_dir, '（dry-run）' if a.dry_run else ''))
    ok = 0
    for p, meta, _ in jobs:
        r = rectify(p, meta, out_dir, k1_override=a.k1, dry=a.dry_run)
        if r:
            ok += 1
    log('完成 %d 项。原图一律未改动；校正图仅用于构件标注与展示，不是测量级正射影像。' % ok)


if __name__ == '__main__':
    main()
