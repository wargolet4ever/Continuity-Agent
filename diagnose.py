"""一条命令查清 ffmpeg 到底出了什么事：python diagnose.py"""

import os
import subprocess
import sys

print(f"Python      {sys.version.split()[0]}  ({sys.platform})")

try:
    import imageio_ffmpeg
    print(f"imageio-ffmpeg  已安装 v{imageio_ffmpeg.__version__}")
except Exception as exc:
    print(f"imageio-ffmpeg  ❌ 导入失败：{exc}")
    print("→ pip install -r requirements.txt")
    raise SystemExit(1)

override = os.getenv("IMAGEIO_FFMPEG_EXE")
if override:
    print(f"IMAGEIO_FFMPEG_EXE  已设为 {override}")
    print("  ⚠️ 这个变量**不会被校验**，指错地方就会直接 WinError 2")
    print(f"  这个路径存在吗：{os.path.isfile(override)}")

try:
    exe = imageio_ffmpeg.get_ffmpeg_exe()
except Exception as exc:
    print(f"ffmpeg 位置    ❌ 找不到：{exc}")
    raise SystemExit(1)

print(f"ffmpeg 位置    {exe}")
print(f"  文件存在吗   {os.path.isfile(exe) if os.path.sep in exe else '（靠 PATH 查找）'}")

try:
    out = subprocess.run([exe, "-version"], capture_output=True, timeout=20)
    if out.returncode == 0:
        print(f"  真的能跑吗   ✅ {out.stdout.decode(errors='replace').splitlines()[0][:70]}")
    else:
        print(f"  真的能跑吗   ❌ 退出码 {out.returncode}")
        print(f"     {out.stderr.decode(errors='replace')[:200]}")
except FileNotFoundError:
    print("  真的能跑吗   ❌ WinError 2 / FileNotFoundError —— 这个路径上没有文件")
    print("     多半是：杀毒软件删了它，或者 pip 装的是不含二进制的包")
    print("     修：pip install --force-reinstall imageio-ffmpeg")
    print("     或：装一个系统 ffmpeg，再设 IMAGEIO_FFMPEG_EXE 指过去")
    raise SystemExit(1)
except Exception as exc:
    print(f"  真的能跑吗   ❌ {type(exc).__name__}: {exc}")
    raise SystemExit(1)

import video_audit
print(f"\nVIDEO_SUPPORTED = {video_audit.VIDEO_SUPPORTED}")
if not video_audit.VIDEO_SUPPORTED:
    print(f"  原因：{video_audit.VIDEO_UNAVAILABLE_REASON}")
else:
    from pathlib import Path
    clip = Path(__file__).resolve().parent / "assets" / "examples" / "shot21_lever_key_error.mp4"
    try:
        sample = video_audit.sample_video(clip)
        print(f"  抽帧实测    ✅ {len(sample.frames)} 帧 / {sample.duration_s} 秒")
    except Exception as exc:
        print(f"  抽帧实测    ❌ {type(exc).__name__}: {exc}")
