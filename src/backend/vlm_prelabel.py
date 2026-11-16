# -*- coding: utf-8 -*-
"""
离线 VLM 构件预标注（file:// 双击打开工作台时的兜底方案）。

调本机 Ollama 的 qwen3-vl:4b 识别构件，输出 **labelme 原生 JSON**：
工作台点「导入 labelme」选回这些 JSON，就会得到橙色虚线「VLM 草稿」框，
再逐框复核修改即可。也可以直接用 labelme 桌面版打开继续标注。

特点：不装环境（模型已在本机）、不常驻服务、照片不出本机、不联网。
词汇表从同目录 index.html 的 ELEMENTS 直接读取，永远与工作台同步。

用法：
    python vlm_prelabel.py "D:\\...\\某张立面.jpg"
    python vlm_prelabel.py "D:\\...\\rectified" --limit 10 --out "D:\\...\\vlm_drafts"
    python vlm_prelabel.py 图片.jpg --model qwen3-vl:4b --max 8

输出：<图片名>_prelabel.json（与图片同目录，或 --out 指定目录）
"""
import argparse
import base64
import datetime
import json
import os
import re
import struct
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OLLAMA = "http://127.0.0.1:11434/api/chat"
DEFAULT_MODEL = "qwen3-vl:4b"
# cv2 只用于缩图，缺失时自动降级为原图（Ollama 会自行缩放，功能不受影响）。
# 2026-11-08 用户重组过环境：labelme_app 已不在，改用候选探测。
CV_CANDIDATES = [r"D:\SRY-SkillS\labelme6-env\Lib\site-packages",
                 r"D:\SRY-SkillS\labelme_app"]


def cv_search_path():
    for c in CV_CANDIDATES:
        if os.path.isdir(c):
            return c
    return None


# 第四层解析用的构件中文名映射；main() 里由 load_vocabulary() 填充
ELEMENTS_CACHE = []

# ---------------------------------------------------------------- 词汇表（单一数据源）
def load_vocabulary():
    html = (HERE / "index.html").read_text(encoding="utf-8")
    m = re.search(r"const ELEMENTS = (\[[\s\S]*?\]);\n", html)
    g = re.search(r"const GROUPS = (\[[\s\S]*?\]);\n", html)
    if not m or not g:
        raise RuntimeError("无法从 index.html 读取 ELEMENTS / GROUPS")
    elems = json.loads(m.group(1))
    groups = dict(re.findall(r"\{id:'([^']+)',\s*name:'([^']+)'", g.group(1)))
    # 实测：47 行逐项列举会让 qwen3-vl:4b-thinking 陷入逐项核对循环，
    # 把生成预算全耗在 thinking 里、content 恒空 → 100% 得到 0 个框。
    # 压成一行紧凑「id=中文名」后 done=stop、13–30 秒稳定出框；47 项一项不少。
    vocab = ", ".join(e["id"] + "=" + e["name"] for e in elems)
    return elems, vocab


def build_prompt(vocab, max_items, retry=False):
    return ("看这张台山骑楼立面照片，挑出确实可见的构件，最多 %d 个。不要分析、不要解释。可用 label：" % max_items +
            vocab + "\n只输出一行 RESULT= 后跟紧凑 JSON：\n" +
            'RESULT={"items":[{"label":"tri_pediment","bbox":[100,20,900,180],"conf":0.8}]}\n' +
            "bbox 为 0-1000 整数千分比 [x1,y1,x2,y2]，x1<x2、y1<y2；看不见的不要编造。" +
            ("\n若判断困难，只给最有把握的 3 个，宁缺毋滥。" if retry else ""))


# ---------------------------------------------------------------- 图片处理
def image_size(data):
    """纯标准库读 JPEG SOF / PNG IHDR 宽高。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    if data[:2] != b"\xff\xd8":
        return 0, 0
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        m = data[i + 1]
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD9:
            i += 2
            continue
        L = int.from_bytes(data[i + 2:i + 4], "big")
        if L < 2:
            break
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            return (int.from_bytes(data[i + 7:i + 9], "big"),
                    int.from_bytes(data[i + 5:i + 7], "big"))
        if m == 0xDA:
            break
        i += 2 + L
    return 0, 0


def shrink_for_vlm(data, side=640):
    """给 VLM 的缩图；有 cv2 就用，没有就原图（Ollama 会自行缩放，只是慢一点）。"""
    try:
        cp = cv_search_path()
        if cp and cp not in sys.path:
            sys.path.insert(0, cp)
        import cv2
        import numpy as np
        arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            return data
        h, w = arr.shape[:2]
        sc = min(1.0, side / max(h, w))
        if sc < 1:
            arr = cv2.resize(arr, (int(w * sc), int(h * sc)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", arr, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        return buf.tobytes() if ok else data
    except Exception:
        return data


def shrink_for_annotation(data, side=1600):
    """写进 labelme JSON 的图：保持可标注分辨率，同时别让 JSON 太大。"""
    try:
        cp = cv_search_path()
        if cp and cp not in sys.path:
            sys.path.insert(0, cp)
        import cv2
        import numpy as np
        arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            return data, image_size(data)
        h, w = arr.shape[:2]
        sc = min(1.0, side / max(h, w))
        if sc < 1:
            arr = cv2.resize(arr, (int(w * sc), int(h * sc)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", arr, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        if ok:
            out = buf.tobytes()
            return out, (arr.shape[1], arr.shape[0])
    except Exception:
        pass
    return data, image_size(data)


# ---------------------------------------------------------------- Ollama 调用与解析
def check_ollama(model, timeout=6):
    """连接自检：返回 (ok, 说明)。分清服务没起 / 模型没装 / 正常。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=timeout) as r:
            o = json.load(r)
    except Exception as e:
        return False, "连不上本机 Ollama（127.0.0.1:11434）：%s。请先启动 Ollama 服务。" % e
    names = [m.get("name", "") for m in o.get("models", [])]
    if not any(n == model or n.startswith(model.split(":")[0]) for n in names):
        return False, "Ollama 里没有 %s。当前模型：%s。可执行 ollama pull %s。" % (
            model, "、".join(names) or "（空）", model)
    return True, "连接正常：%s 已就绪（共 %d 个模型）" % (model, len(names))


def call_ollama(b64, prompt, model, timeout=180, retry=False):
    # 2026-11-09 实测：qwen3-vl:4b 是 qwen3-vl-thinking renderer，
    # 默认上下文仅 8192；显式给 think/num_ctx/num_predict 才能稳定出结果。
    body = {"model": model, "stream": False, "think": False,
            "options": {"temperature": 0.3 if retry else 0.1,
                        "num_predict": 2048, "num_ctx": 8192},
            "messages": [{"role": "user", "content": prompt, "images": [b64]}]}
    req = urllib.request.Request(OLLAMA, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def extract_items(text, valid_ids=None):
    """qwen3-vl:4b 是 thinking 模型：常把完整 JSON 写在 thinking 里、content 为空，
    有时思考没写完就被截断，只剩自然语言列举 + 坐标。故做三层容错。"""
    out = []
    # ① 完整 {"items":[...]}
    for m in re.finditer(r'\{\s*"items"\s*:\s*\[([\s\S]*?)\]\s*\}', text):
        try:
            o = json.loads('{"items":[' + m.group(1) + ']}')
            if isinstance(o.get("items"), list):
                out.append(o["items"])
        except Exception:
            pass
    # ② 分散的 "label"/"bbox"/"conf" 片段
    if not out:
        one = []
        for m in re.finditer(r'"label"\s*:\s*"([a-z_]+)"\s*,\s*"bbox"\s*:\s*\[([^\]]+)\]\s*,\s*"conf"\s*:\s*([\d.]+)', text):
            bb = [float(x) for x in m.group(2).split(",")]
            if len(bb) == 4:
                one.append({"label": m.group(1), "bbox": bb, "conf": float(m.group(3))})
        if one:
            out.append(one)
    # 只有 ①② 的 JSON 才算「已有结构化结果」；③④ 都是自然语言兜底，需合并去重
    has_json = bool(out)
    # ③ 自然语言列举：「1. broken_pediment：顶部山花，坐标[100, 20, 900, 180]」
    if not has_json:
        one = []
        pattern = (r'(?:^|\n)\s*(?:\d+\s*[.、)]\s*|[-*]\s*)?'
                   r'([a-z][a-z_]{3,})\s*[:：]?\s*[^\[\]\n]{0,70}?'
                   r'\[\s*(\d{1,4})\s*,\s*(\d{1,4})\s*,\s*(\d{1,4})\s*,\s*(\d{1,4})\s*\]')
        for m in re.finditer(pattern, text):
            lab = m.group(1)
            if valid_ids is not None and lab not in valid_ids:
                continue
            # 这种来源是模型估计，置信度一律压到 0.5（且仍标草稿）
            one.append({"label": lab,
                        "bbox": [int(m.group(i)) for i in range(2, 6)],
                        "conf": 0.5})
        if one:
            out.append(one)
    # ③ 只认英文 id，可能先「抢答」而挡住 ④ 的中文名匹配；
    # 故无 JSON 时 ③④ 都跑，最后合并去重。
    # ④ thinking 里的自然语言估算：「broken_pediment：顶部装饰，所以 bbox[150, 80, 850, 180]」
    #    或「46遮阳板：底部遮阳棚，所以bbox[0, 700, 1000, 800]」。content 为空时这是唯一来源。
    if not has_json:
        one, seen_box = [], set()
        name2id = {}
        for e in ELEMENTS_CACHE:
            name2id[e["name"]] = e["id"]
            for part in re.split(r"[／/]", e["name"]):
                p = part.strip()
                if len(p) >= 2 and p not in name2id:
                    name2id[p] = e["id"]
        for m in re.finditer(r"\[\s*(\d{1,4})\s*,\s*(\d{1,4})\s*,\s*(\d{1,4})\s*,\s*(\d{1,4})\s*\]", text):
            b = [int(m.group(i)) for i in range(1, 5)]
            if b[2] <= b[0] or b[3] <= b[1]:
                continue
            bk = ",".join(map(str, b))
            if bk in seen_box:
                continue
            ctx = text[max(0, m.start() - 140):m.start()]
            lab, best_pos = None, -1
            for mm in re.finditer(r"[a-z][a-z_]{3,}", ctx):
                if (valid_ids is None or mm.group(0) in valid_ids) and mm.start() > best_pos:
                    lab, best_pos = mm.group(0), mm.start()
            for nm, eid in name2id.items():
                if len(nm) < 2:
                    continue
                p = ctx.rfind(nm)
                if p >= 0 and p + len(nm) > best_pos:
                    lab, best_pos = eid, p + len(nm)
            if lab:
                seen_box.add(bk)
                one.append({"label": lab, "bbox": b, "conf": 0.5})
        if one:
            out.append(one)
    if not has_json and len(out) > 1:
        merged, seen = [], set()
        for arr in out:
            for it in arr:
                k = str(it.get("label")) + "|" + ",".join(map(str, it.get("bbox") or []))
                if k not in seen:
                    seen.add(k)
                    merged.append(it)
        return merged
    return max(out, key=len) if out else []


def normalize(items, valid_ids, max_items):
    seen, res = set(), []
    for it in items:
        lab = (it or {}).get("label")
        if lab not in valid_ids:
            continue
        b = [float(x) for x in (it.get("bbox") or [])]
        if len(b) != 4:
            continue
        x1, y1, x2, y2 = [max(0.0, min(1000.0, v)) / 1000.0 for v in b]
        if x2 <= x1 or y2 <= y1 or (x2 - x1) > 0.995 or (y2 - y1) > 0.995:
            continue
        key = lab + "|" + ",".join("%.2f" % v for v in (x1, y1, x2, y2))
        if key in seen:
            continue
        seen.add(key)
        try:
            conf = float(it.get("conf", 0))
        except Exception:
            conf = 0.0
        res.append({"label": lab, "box": [x1, y1, x2, y2], "vlmConf": conf})
        if len(res) >= max_items:
            break
    return res


def to_labelme_json(items, img_name, img_data, W, H, model):
    now = datetime.datetime.now().isoformat(timespec="seconds")
    shapes = []
    for i, it in enumerate(items, 1):
        x1, y1, x2, y2 = it["box"]
        ext = {"id": "vlm%d" % i, "variant": "", "conf": 0.9, "origin": "待考",
               "dispute": False, "note": "", "source": "vlm", "draft": True,
               "reviewed": False, "vlmConf": it["vlmConf"], "vlmModel": model,
               "vlmAt": now, "reviewedAt": None}
        shapes.append({
            "label": it["label"],
            "points": [[round(x1 * W, 2), round(y1 * H, 2)],
                       [round(x2 * W, 2), round(y2 * H, 2)]],
            "group_id": None, "shape_type": "rectangle",
            "flags": {"dispute": False, "draft": True, "reviewed": False},
            "description": json.dumps(ext, ensure_ascii=False),
            "mask": None,
        })
    return {
        "version": "5.8.1",
        "flags": {"draft": True, "prelabel": True},
        "shapes": shapes,
        "imagePath": img_name,
        "imageData": base64.b64encode(img_data).decode(),
        "imageHeight": H, "imageWidth": W, "text": "",
        "description": ("台山骑楼 VLM 构件预标注草稿 · 由本机 %s 生成。"
                        "全部为橙色虚线草稿，必须人工逐框复核后才计入证据分与正式导出；"
                        "框位置为模型估计值、置信度为模型自评，不可复现，不得写入论文结论。" % model),
    }


def main():
    ap = argparse.ArgumentParser(description="离线 VLM 构件预标注 → labelme JSON")
    ap.add_argument("input", help="图片路径，或包含图片的目录")
    ap.add_argument("--out", default="", help="输出目录（默认与图片同目录）")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max", type=int, default=8, help="每张最多几个构件")
    ap.add_argument("--limit", type=int, default=0, help="目录模式最多处理几张（0=全部）")
    a = ap.parse_args()

    elems, vocab = load_vocabulary()
    globals()["ELEMENTS_CACHE"] = elems
    valid = {e["id"] for e in elems}
    prompt = build_prompt(vocab, a.max)
    print("词汇表 %d 项（紧凑格式） | 模型 %s | 本机 Ollama" % (len(elems), a.model))
    ok_svc, msg = check_ollama(a.model)
    print("连接自检：", msg)
    if not ok_svc:
        return 2

    p = Path(a.input)
    if p.is_dir():
        files = sorted([x for x in p.iterdir()
                        if x.suffix.lower() in (".jpg", ".jpeg", ".png")])
        if a.limit:
            files = files[:a.limit]
    else:
        files = [p]
    if not files:
        print("没有找到图片")
        return
    outdir = Path(a.out) if a.out else None
    if outdir:
        outdir.mkdir(parents=True, exist_ok=True)

    ok = 0
    for f in files:
        t0 = time.time()
        try:
            raw = f.read_bytes()
            vlm_img = shrink_for_vlm(raw)
            ann_img, (W, H) = shrink_for_annotation(raw)
            if not W or not H:
                print("  跳过 %s：读不到尺寸" % f.name)
                continue
            o = call_ollama(base64.b64encode(vlm_img).decode(), prompt, a.model)
            msg = o.get("message", {})
            text = (msg.get("content") or "") + "\n" + (msg.get("thinking") or "")
            items = normalize(extract_items(text, valid), valid, a.max)
            if not items:
                print("    首次未取到草稿，自动重试一次…")
                retry_prompt = build_prompt(vocab, min(4, a.max), retry=True)
                o = call_ollama(base64.b64encode(vlm_img).decode(), retry_prompt,
                                a.model, retry=True)
                msg = o.get("message", {})
                text = (msg.get("content") or "") + "\n" + (msg.get("thinking") or "")
                items = normalize(extract_items(text, valid), valid, a.max)
            obj = to_labelme_json(items, f.name, ann_img, W, H, a.model)
            dest = (outdir or f.parent) / (f.stem + "_prelabel.json")
            dest.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
            ok += 1
            print("  %s → %d 个草稿框 | %.1fs | %s" %
                  (f.name[:42], len(items), time.time() - t0, dest.name))
        except Exception as e:
            print("  失败 %s：%s %s" % (f.name[:42], type(e).__name__, str(e)[:120]))
    print("完成 %d/%d 张。回到工作台点「导入 labelme」选这些 JSON，即可逐框复核。" % (ok, len(files)))


if __name__ == "__main__":
    sys.exit(main() or 0)
