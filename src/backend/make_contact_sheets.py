# -*- coding: utf-8 -*-
"""
第 1 步：生成「每格叠编号」的缩略图拼图，供人工清洗筛选。
零第三方依赖：用本机 Chrome headless 渲染 HTML（编号由脚本直接写入 DOM，
不依赖 ffmpeg 的 %{n}，保证编号与「可用正立面清单.csv」一一对应）。

前置：先跑 build_index.py，再跑一次 ffmpeg 生成 thumbs/（见本目录 build_index.py 注释）。
    ffmpeg -y -loglevel error -f concat -safe 0 -i concat_list.txt \
      -vf "scale=300:210:force_original_aspect_ratio=decrease,pad=300:210:(ow-iw)/2:(oh-ih)/2:color=white" \
      -fps_mode passthrough -start_number 0 -q:v 4 "thumbs/%04d.jpg"

用法：python "04-正立面/make_contact_sheets.py"
产物：contact_sheets/sheet_001.jpg ... （每张 5 列 × 4 行 = 20 格）
      thumbs/0000.jpg ... （单张缩略图，编号即文件名，便于按编号放大查看）
"""
import json, os, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'contact_sheets'
THUMBS = ROOT / 'thumbs'
TMP = Path(r'D:\tmp\taishan-read\sheet_pages')
CHROME = r'C:\Program Files\Google\Chrome\Application\chrome.exe'
COLS, ROWS = 5, 4
PER = COLS * ROWS

HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8"><style>
*{box-sizing:border-box}
body{margin:0;background:#f6f5f1;font:14px system-ui,"Microsoft YaHei",sans-serif;color:#262a2e}
header{height:44px;display:flex;align-items:center;gap:14px;padding:0 14px;background:#fff;border-bottom:1px solid #e4e1d9}
header b{font-size:15px}header span{color:#8b9096;font-size:12px}
#grid{display:grid;grid-template-columns:repeat(5,306px);gap:6px;padding:8px 12px}
.cell{position:relative;width:306px;height:216px;background:#fff;border:1px solid #e4e1d9;border-radius:5px;overflow:hidden}
.cell img{width:300px;height:210px;display:block;margin:3px;object-fit:contain;background:#fff}
.no{position:absolute;left:6px;top:5px;background:#fff;border:1px solid #d3cfc4;border-radius:4px;
    padding:0 6px;font:bold 19px/26px Consolas,monospace;color:#a9552c;box-shadow:0 1px 4px rgba(0,0,0,.12)}
.empty{background:#eeece7;border-style:dashed}
</style></head><body>
<header><b>台山骑楼调研照 · 缩略图拼图</b><span id="ttl"></span>
<span style="margin-left:auto">编号与「可用正立面清单.csv」一致；在人工判定列填写 可用/展板/室内/特写/路面/斜拍/模糊/重复/待定</span></header>
<div id="grid"></div>
<script>
var q=new URLSearchParams(location.search), start=parseInt(q.get('s')||'0',10), total=parseInt(q.get('t')||'0',10);
var root=__ROOT__, h='';
for(var i=0;i<__PER__;i++){
  var n=start+i;
  if(n>=total){ h+='<div class="cell empty"></div>'; continue; }
  var fn=('0000'+n).slice(-4)+'.jpg';
  h+='<div class="cell"><img src="'+root+fn+'"><span class="no">'+n+'</span></div>';
}
document.getElementById('grid').innerHTML=h;
document.getElementById('ttl').textContent='第 '+(Math.floor(start/__PER__)+1)+' 张 · 编号 '+start+'–'+Math.min(start+__PER__-1,total-1);
</script></body></html>"""


def main():
    idx = json.loads((ROOT / 'file_index.json').read_text(encoding='utf-8'))
    total = idx['count']
    if not THUMBS.is_dir() or len(list(THUMBS.glob('*.jpg'))) < total:
        sys.exit('thumbs/ 缩略图不足，请先用 ffmpeg 生成（见文件头注释）')
    if not Path(CHROME).exists():
        sys.exit('找不到 Chrome：' + CHROME)
    OUT.mkdir(exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)
    root_uri = THUMBS.as_uri() + '/'
    html = (HTML.replace('__ROOT__', json.dumps(root_uri))
                .replace('__PER__', str(PER)))
    page = TMP / 'sheet.html'
    page.write_text(html, encoding='utf-8')
    page_uri = page.as_uri()

    # PNG 中间产物只写临时目录，交付目录里只留 JPG（避免批量删除中间文件）
    n = (total + PER - 1) // PER
    for k, start in enumerate(range(0, total, PER), 1):
        png = TMP / ('sheet_%03d.png' % k)
        cmd = [CHROME, '--headless=new', '--disable-gpu', '--no-sandbox',
               '--allow-file-access-from-files', '--hide-scrollbars',
               '--window-size=1580,960', '--virtual-time-budget=3500',
               '--screenshot=' + str(png), page_uri + '?s=%d&t=%d' % (start, total)]
        subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=90)
        if not png.exists() or png.stat().st_size < 5000:
            print('警告：第 %d 张拼图生成异常' % k)
    print('已渲染 %d 张 PNG（临时目录 %s）' % (len(list(TMP.glob('sheet_*.png'))), TMP))

    # PNG -> JPG（体积从约 50MB 降到约 13MB）
    r = subprocess.run(['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                        '-framerate', '1', '-start_number', '1',
                        '-i', str(TMP / 'sheet_%03d.png'),
                        '-start_number', '1', '-q:v', '3',
                        str(OUT / 'sheet_%03d.jpg')],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print('PNG→JPG 失败：', r.stderr[-400:])
        return
    jpgs = sorted(OUT.glob('sheet_*.jpg'))
    print('完成：%d 张拼图 -> %s' % (len(jpgs), OUT))
    print('每张 %d 格，编号连续；单张缩略图见 thumbs/0000.jpg …' % PER)


if __name__ == '__main__':
    main()
