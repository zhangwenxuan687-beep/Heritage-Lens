# -*- coding: utf-8 -*-
"""
立面 0/1 矩阵 -> 类型字符串频次 / Wilkinson seriation / 比例汉明距离矩阵 / NJ 树(Newick)

纯标准库，零依赖。对应 Xue et al. (2026) Methods 的 Interpretation 一节，
但按小样本试分类改写：保留全部字符串，不做「<8 实例剔除」。

用法:
    python pdistance_nj.py 立面构件0-1矩阵_模板.csv --min-freq 3 --out 试点结果

--min-freq N : 出现少于 N 栋的构件不进距离计算（论文是 <40，3361 张；
               30-50 栋小样本建议 3，设 0 则不做频次筛选）
输出:
    <out>_字符串频次.csv
    <out>_距离矩阵.csv        (按 seriation 重排后的顺序)
    <out>_重排矩阵.csv
    <out>_类型树.newick
"""
import argparse
import csv
import os
from collections import Counter

META_COLS = {
    "样本编号", "照片文件", "圩镇/街道", "楼层", "拍摄质量(正视/斜视/遮挡)",
    "字符串标签", "首选风格(5类/混合/待定)", "次选风格", "判定理由",
    "争议标记", "复核人", "日期", "备注",
}


def read_matrix(path, min_freq):
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit("矩阵为空：请至少填写一行 0/1 数据")
    headers = list(rows[0].keys())
    # 构件列 = 非元信息列，且所有取值都属于 {'', '0', '1'}
    candidates = [h for h in headers if h not in META_COLS and h.strip()]
    elem_cols = [
        h for h in candidates
        if all((r.get(h) or "").strip() in ("", "0", "1") for r in rows)
    ]
    if not elem_cols:
        raise SystemExit("没找到构件列：请检查表头，构件列取值只能是 0 / 1 / 空")

    freq = {
        c: sum(1 for r in rows if (r.get(c) or "").strip() == "1")
        for c in elem_cols
    }
    dropped = [c for c in elem_cols if freq[c] < min_freq] if min_freq > 0 else []
    used = [c for c in elem_cols if c not in dropped]

    data = []
    for r in rows:
        vec = tuple(1 if (r.get(c) or "").strip() == "1" else 0 for c in used)
        if not any(vec):
            continue  # 全 0 串，对应论文的 000...0 过滤
        name = (r.get("样本编号") or "?").strip() or "?"
        data.append((name, vec))
    return used, freq, dropped, data


def print_strings(data):
    counter = Counter(v for _, v in data)
    return ["".join(map(str, v)) for v in counter], counter


def seriation(vecs):
    """Wilkinson 无约束序列重排：交替按行质心、列质心排序至稳定。"""
    n, m = len(vecs), len(vecs[0])
    rows, cols = list(range(n)), list(range(m))
    for _ in range(200):
        def row_centroid(i):
            pos = [p + 1 for p, c in enumerate(cols) if vecs[i][c] == 1]
            return (sum(pos) / len(pos) if pos else m + 1, i)

        new_rows = sorted(rows, key=row_centroid)

        def col_centroid(j):
            pos = [p + 1 for p, i in enumerate(new_rows) if vecs[i][j] == 1]
            return (sum(pos) / len(pos) if pos else n + 1, j)

        new_cols = sorted(cols, key=col_centroid)
        if new_rows == rows and new_cols == cols:
            break
        rows, cols = new_rows, new_cols
    return rows, cols


def pdistance(a, b):
    m = len(a)
    return sum(1 for x, y in zip(a, b) if x != y) / m if m else 0.0


def neighbor_joining(names, vecs):
    """标准 NJ：Q 判据选对 -> 记枝长 -> 合并 -> 输出 Newick。"""
    n = len(names)
    if n < 2:
        return (names[0] + ";" if n else ";")
    m = len(vecs[0])
    cur = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = pdistance(vecs[i], vecs[j])
            cur[i][j] = cur[j][i] = d
    ids = list(range(n))
    labels = {i: names[i] for i in range(n)}
    nxt = n
    while len(ids) > 2:
        r = len(ids)
        sums = [sum(cur[i]) for i in range(r)]
        best = None
        for i in range(r):
            for j in range(i + 1, r):
                q = (r - 2) * cur[i][j] - sums[i] - sums[j]
                if best is None or q < best[0]:
                    best = (q, i, j)
        _, i, j = best
        dij = cur[i][j]
        ri = sums[i] / (r - 2) if r > 2 else 0.0
        rj = sums[j] / (r - 2) if r > 2 else 0.0
        li = max(0.0, 0.5 * dij + 0.5 * (ri - rj))
        lj = max(0.0, dij - li)
        new = nxt
        nxt += 1
        labels[new] = "(%s:%.5f,%s:%.5f)" % (labels[ids[i]], li, labels[ids[j]], lj)
        keep = [k for k in range(r) if k not in (i, j)]
        new_row = [max(0.0, 0.5 * (cur[i][k] + cur[j][k] - dij)) for k in keep]
        size = len(keep) + 1
        new_cur = [[0.0] * size for _ in range(size)]
        for a, ka in enumerate(keep):
            for b, kb in enumerate(keep):
                new_cur[a][b] = cur[ka][kb]
            new_cur[a][size - 1] = new_cur[size - 1][a] = new_row[a]
        ids = [ids[k] for k in keep] + [new]
        cur = new_cur
    d = cur[0][1]
    return "(%s:%.5f,%s:%.5f);" % (labels[ids[0]], d / 2, labels[ids[1]], d / 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("matrix", help="0/1 矩阵 CSV")
    ap.add_argument("--min-freq", type=int, default=3, help="构件最少出现栋数，默认 3；0 表示不筛选")
    ap.add_argument("--out", default="试点结果", help="输出文件前缀")
    args = ap.parse_args()

    used, freq, dropped, data = read_matrix(args.matrix, args.min_freq)
    if len(data) < 2:
        raise SystemExit("有效样本少于 2 栋，无法计算距离")
    vecs = [v for _, v in data]
    names = [n for n, _ in data]

    print("有效样本 %d 栋，进矩阵构件 %d 项" % (len(data), len(used)))
    if dropped:
        print("频次筛选剔除 %d 项: %s" % (len(dropped), ", ".join(dropped[:12]) + (" ..." if len(dropped) > 12 else "")))
    print("构件: " + ", ".join(used))

    strings, counter = print_strings(data)
    print("不同字符串 %d 个；单例 %d 个（小样本不剔除，按汉明距离归并）" % (
        len(counter), sum(1 for c in counter.values() if c == 1)))

    base = os.path.join(os.path.dirname(os.path.abspath(args.matrix)), args.out)

    with open(base + "_字符串频次.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["字符串", "实例数", "构件列顺序"])
        for v, c in counter.most_common():
            w.writerow(["".join(map(str, v)), c, "|".join(used)])

    order_r, order_c = seriation(vecs)
    ordered_names = [names[i] for i in order_r]
    ordered_cols = [used[c] for c in order_c]
    with open(base + "_重排矩阵.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["样本编号"] + ordered_cols)
        for i in order_r:
            w.writerow([names[i]] + [vecs[i][c] for c in order_c])

    with open(base + "_距离矩阵.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([""] + ordered_names)
        for a, i in enumerate(order_r):
            w.writerow([names[i]] + ["%.4f" % pdistance(vecs[i], vecs[j]) for j in order_r])

    tree = neighbor_joining(names, vecs)
    with open(base + "_类型树.newick", "w", encoding="utf-8") as f:
        f.write(tree + "\n")

    print("已输出: %s_字符串频次.csv / _重排矩阵.csv / _距离矩阵.csv / _类型树.newick" % base)
    print("Newick 可导入 SplitsTree6 画 NJ 树；把距离矩阵导入 SplitsTree6 可改跑 Neighbor-Net 网络。")


if __name__ == "__main__":
    main()
