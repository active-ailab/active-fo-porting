"""重新生成两个「双击即用」的启动文件。

wscript.exe 只认 ANSI 或 UTF-16(带 BOM)，PowerShell 5.1 只认 ANSI 或 UTF-8(带 BOM)，
而文件里有中文提示 —— 编码搞错会直接导致语法错误。所以改完源文件后跑一次本脚本：

    python _rebuild_launcher.py

  launch.vbs.utf8 (UTF-8, 可读源)  ->  launch.vbs (UTF-16LE + BOM)
  launch.ps1      (UTF-8, 可读源)  ->  launch.ps1 (原地补 BOM + CRLF)
"""
import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def rebuild_vbs():
    src = os.path.join(HERE, "launch.vbs.utf8")
    dst = os.path.join(HERE, "launch.vbs")
    with io.open(src, encoding="utf-8") as f:
        text = f.read()
    with io.open(dst, "w", encoding="utf-16", newline="\r\n") as f:
        f.write(text)
    with open(dst, "rb") as f:
        ok = f.read(2) == b"\xff\xfe"
    print("launch.vbs   %d bytes  BOM=%s" % (os.path.getsize(dst), "ok" if ok else "BAD"))


def rebuild_ps1():
    path = os.path.join(HERE, "launch.ps1")
    with io.open(path, encoding="utf-8-sig") as f:
        text = f.read()
    with io.open(path, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(text)
    with open(path, "rb") as f:
        ok = f.read(3) == b"\xef\xbb\xbf"
    print("launch.ps1   %d bytes  BOM=%s" % (os.path.getsize(path), "ok" if ok else "BAD"))


if __name__ == "__main__":
    rebuild_vbs()
    rebuild_ps1()
