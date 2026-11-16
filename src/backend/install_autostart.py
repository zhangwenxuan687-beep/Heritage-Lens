# -*- coding: utf-8 -*-
"""安装/卸载「VLM 本地转发服务」开机自启（无窗口后台）。

背景：内置预览面板是 about:// 不透明来源，Ollama 对 Origin:null 返回 403，
必须经 http://127.0.0.1:8765/ollama/* 转发才能调用本机视觉模型。

本脚本在当前用户的「启动」文件夹放一个纯文本 .vbs，开机由 Windows
脚本宿主静默执行 pythonw start_vlm_workbench.py --silent：
无窗口、不开浏览器、零依赖、不装任何东西。

用法：
    python install_autostart.py            # 安装（已装则覆盖更新）
    python install_autostart.py --remove  # 卸载（只删本脚本创建的启动项）
    python install_autostart.py --status  # 查看状态

不修改 Ollama 任何配置、不重启服务、不改环境变量。
"""
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE / "start_vlm_workbench.py"
STARTUP = pathlib.Path(os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs\Startup"
VBS = STARTUP / "Taishan-VLM-Proxy.vbs"
OLD_LNK = STARTUP / "Taishan-VLM-Proxy.lnk"   # 旧版 pylnk3 生成，Windows 不识别，顺手清掉

# 优先用 D 盘独立 Python（不随 WorkBuddy 升级变动），其次系统 Python310
PYTHONW_CANDIDATES = [
    r"D:\python3.11.9\pythonw.exe",
    r"C:\Users\Ray\AppData\Local\Programs\Python\Python310\pythonw.exe",
    r"C:\Users\Ray\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe",
]


def find_pythonw():
    for p in PYTHONW_CANDIDATES:
        if pathlib.Path(p).is_file():
            return p
    p = pathlib.Path(sys.executable).parent / "pythonw.exe"
    return str(p) if p.is_file() else None


def build_vbs(pyw, script, wd):
    return '\r\n'.join([
        "' Taishan VLM proxy autostart - hidden, no window",
        'Set sh = CreateObject("WScript.Shell")',
        'sh.CurrentDirectory = "' + wd + '"',
        'sh.Run """' + pyw + '"" """' + script + '"" --silent", 0, False',
    ]) + '\r\n'


def main():
    remove = '--remove' in sys.argv
    status = '--status' in sys.argv
    if status:
        print('启动项 :', VBS, '存在' if VBS.exists() else '不存在')
        print('旧 lnk :', OLD_LNK, '存在' if OLD_LNK.exists() else '已清理')
        return 0
    if remove:
        n = 0
        for p in (VBS, OLD_LNK):
            if p.exists():
                p.unlink()
                n += 1
                print('已删除：', p)
        print('已卸载 %d 个启动项。' % n if n else '本来就没有安装。')
        print('注意：已运行的转发进程需重启电脑，或在任务管理器结束 pythonw.exe 才停止。')
        return 0

    pyw = find_pythonw()
    if not pyw:
        print('找不到 pythonw.exe，无法安装自启。')
        return 1
    if not SCRIPT.is_file():
        print('找不到 start_vlm_workbench.py')
        return 1
    if not STARTUP.is_dir():
        print('找不到启动文件夹：', STARTUP)
        return 1

    VBS.write_bytes(build_vbs(pyw, str(SCRIPT), str(HERE)).encode('gbk'))
    if OLD_LNK.exists():
        OLD_LNK.unlink()
    print('已安装开机自启：')
    print('  启动项   :', VBS)
    print('  执行命令 :', pyw, '"%s" --silent' % SCRIPT)
    print('  方式     : 无窗口后台，开机自动运行；不弹窗、不开浏览器')
    print('  卸载     : python install_autostart.py --remove（或双击 卸载_VLM自启.bat）')
    print('  备注     : 端口 8765 已有实例时静默模式会自动退出，不会重复起进程')
    return 0


if __name__ == '__main__':
    sys.exit(main())
