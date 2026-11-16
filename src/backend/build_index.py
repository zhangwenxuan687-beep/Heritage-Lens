# -*- coding: utf-8 -*-
"""
一次性离线脚本 · 第 0 步：给调研照建立「带编号的可用正立面清单」。
零第三方依赖，只读原图，绝不修改 / 删除 / 移动任何原文件。

用法：
    python "04-正立面/build_index.py"

产物（全部写到 04-正立面/ 下）：
    可用正立面清单.csv     带编号清单；「人工判定」列留空，供看缩略图拼图后填写
    file_index.json        编号 → 原图路径 / 尺寸 / EXIF 方向的机器可读索引
    concat_list.txt        ffmpeg contact sheet 输入列表

说明：
  * 自动剔除 macOS 产生的 `._*` AppleDouble 元数据文件（不是图片，ffmpeg 无法解码）。
  * JPEG 解析按段 seek，兼容手机照片里超大的 APP1/APP2 分块（SOF 可能落在 300 KB 之后）。
  * 机器只做尺寸、EXIF 方向、长宽比、重复文件这类规则初筛；
    「展板／室内／门口特写／路面／严重斜拍／模糊」必须人工看拼图判定，本脚本不臆测。
"""
import csv, hashlib, json, os, re, sys, datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SRC = os.path.join(ROOT, '01-前期资料', '调研图片')
OUT = os.path.join(ROOT, '04-正立面')
EXTS = ('.jpg', '.jpeg', '.png')
SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]


def parse_orientation(tiff):
    """tiff 为 TIFF header 起始处的字节（Exif\\0\\0 之后）。"""
    if len(tiff) < 8:
        return 1
    if tiff[:2] == b'II':
        bo = 'little'
    elif tiff[:2] == b'MM':
        bo = 'big'
    else:
        return 1
    if int.from_bytes(tiff[2:4], bo) != 0x2A:
        return 1
    ifd = int.from_bytes(tiff[4:8], bo)
    if ifd + 2 > len(tiff):
        return 1
    cnt = int.from_bytes(tiff[ifd:ifd + 2], bo)
    for k in range(min(cnt, 512)):
        e = ifd + 2 + k * 12
        if e + 12 > len(tiff):
            break
        if int.from_bytes(tiff[e:e + 2], bo) == 0x0112:
            return int.from_bytes(tiff[e + 8:e + 10], bo) or 1
    return 1


def image_header(path):
    """按 JPEG 段 seek 读取，返回 (w, h, exif_orientation)。只读极少量字节。"""
    with open(path, 'rb') as f:
        head = f.read(8)
        if head[:8] == b'\x89PNG\r\n\x1a\n':
            f.seek(16)
            return (int.from_bytes(f.read(4), 'big'),
                    int.from_bytes(f.read(4), 'big'), 1)
        if head[:2] != b'\xff\xd8':
            return 0, 0, 1
        f.seek(2)
        w = h = 0
        ori = 1
        while True:
            b = f.read(1)
            if not b:
                break
            if b != b'\xff':
                continue                       # 填充字节，继续找 FF
            m = f.read(1)
            while m == b'\xff':                # FF FF ... 填充
                m = f.read(1)
            if not m:
                break
            mark = m[0]
            if mark in (0xD8, 0x01) or 0xD0 <= mark <= 0xD9:
                continue
            lb = f.read(2)
            if len(lb) < 2:
                break
            L = int.from_bytes(lb, 'big')
            if L < 2:
                break
            if mark in SOF:
                seg = f.read(5)
                if len(seg) >= 5:
                    h = int.from_bytes(seg[1:3], 'big')
                    w = int.from_bytes(seg[3:5], 'big')
            elif mark == 0xE1 and L < 2_000_000:
                seg = f.read(L - 2)
                if seg[:4] == b'Exif':
                    ori = parse_orientation(seg[6:])
            else:
                f.seek(L - 2, 1)
            if mark == 0xDA or (w and ori != 1):
                # SOS 之后是压缩数据；若已拿到尺寸且方向非 1 可提前退出
                if mark == 0xDA:
                    break
            elif w and mark in SOF:
                # SOF 已取到，但还要继续找 EXIF？EXIF 一定在 SOF 之前，可退出
                break
    return w, h, ori


def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for blk in iter(lambda: f.read(1 << 20), b''):
            h.update(blk)
    return h.hexdigest()


def main():
    if not os.path.isdir(SRC):
        sys.exit('找不到调研图片目录：' + SRC)
    files, skipped = [], []
    for dirpath, _, names in os.walk(SRC):
        for nm in names:
            if nm.startswith('._'):
                skipped.append(nm)
                continue
            if os.path.splitext(nm)[1].lower() in EXTS:
                files.append(os.path.join(dirpath, nm))
    files.sort(key=lambda p: (natural_key(os.path.basename(os.path.dirname(p))),
                              natural_key(os.path.basename(p))))
    print('真实图片 %d 张；已剔除 AppleDouble 元数据文件 %d 个' % (len(files), len(skipped)))

    rows = []
    for idx, p in enumerate(files):
        size = os.path.getsize(p)
        w, h, ori = image_header(p)
        rows.append(dict(no=idx, group=os.path.basename(os.path.dirname(p)),
                         name=os.path.basename(p),
                         rel=os.path.relpath(p, ROOT).replace('\\', '/'),
                         w=w, h=h, kb=round(size / 1024, 1),
                         ori=ori, size=size, path=p))

    # 重复检测：先按文件大小分组，只对同尺寸文件算 md5，避免读几 GB
    bysize = {}
    for r in rows:
        bysize.setdefault(r['size'], []).append(r)
    dup = 0
    for _, group in bysize.items():
        if len(group) < 2:
            continue
        seen = {}
        for r in group:
            v = md5(r['path'])
            if v in seen:
                r['dup'] = '重复#' + str(seen[v])
                dup += 1
            else:
                seen[v] = r['no']

    for r in rows:
        ratio = (r['w'] / r['h']) if r['w'] and r['h'] else 0
        tags = []
        if r.get('dup'):
            tags.append(r['dup'])
        if r['w'] and r['h'] and min(r['w'], r['h']) < 1000:
            tags.append('分辨率偏低')
        if ratio and (ratio > 2.2 or ratio < 0.45):
            tags.append('长宽比异常')
        if r['ori'] != 1:
            tags.append('EXIF需转正=' + str(r['ori']))
        if not r['w']:
            tags.append('尺寸未解析')
        r['ratio'] = round(ratio, 3)
        r['machine'] = '；'.join(tags) if tags else '机器初筛通过'

    os.makedirs(OUT, exist_ok=True)
    csv_path = os.path.join(OUT, '可用正立面清单.csv')
    with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
        wr = csv.writer(f)
        wr.writerow(['编号', '拍摄批次', '文件名', '原图相对路径', '宽px', '高px',
                     '长宽比', '文件大小KB', 'EXIF方向', '机器初筛标记',
                     '人工判定(可用/展板/室内/特写/路面/斜拍/模糊/重复/待定)', '备注'])
        for r in rows:
            wr.writerow([r['no'], r['group'], r['name'], r['rel'], r['w'], r['h'],
                         r['ratio'], r['kb'], r['ori'], r['machine'], '', ''])

    index = {
        'note': '编号自 0 起，与 contact_sheets 缩略图每格叠印的编号一一对应；原图一律不动；已剔除 ._* 元数据文件',
        'created': datetime.datetime.now().isoformat(timespec='seconds'),
        'source_root': '01-前期资料/调研图片',
        'count': len(rows),
        'skipped_appledouble': len(skipped),
        'items': [{k: r[k] for k in ('no', 'group', 'name', 'rel', 'w', 'h',
                                     'ori', 'kb', 'machine')} for r in rows]
    }
    with open(os.path.join(OUT, 'file_index.json'), 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    with open(os.path.join(OUT, 'concat_list.txt'), 'w', encoding='utf-8') as f:
        for r in rows:
            f.write("file '" + r['path'].replace('\\', '/') + "'\n")

    nrot = sum(1 for r in rows if r['ori'] != 1)
    ntag = sum(1 for r in rows if r['machine'] != '机器初筛通过')
    print('清单:', csv_path)
    print('统计: 共 %d 张 | EXIF 需转正 %d 张 | 机器标记 %d 张 | 完全重复 %d 张 | 尺寸未解析 %d 张'
          % (len(rows), nrot, ntag, dup, sum(1 for r in rows if not r['w'])))


if __name__ == '__main__':
    main()
