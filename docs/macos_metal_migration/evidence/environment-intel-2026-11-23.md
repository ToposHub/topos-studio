# Topos Metal 迁移 · 环境记录（阶段 0.1）

采集时间: 2026-11-22T16:42:23Z  机器: Mac Pro

```
$ git rev-parse HEAD
3416549b6cc061194eb02db57368c9dd0cb40fff
$ sw_vers
ProductName:		macOS
ProductVersion:		15.3.1
BuildVersion:		24D70
$ uname -m
x86_64
$ python -V
Python 3.12.13
$ ffmpeg -version | head -n 1
ffmpeg version 9.0.2 Copyright (c) 2000-2026 the FFmpeg developers
$ ffprobe -version | head -n 1
ffprobe version 9.0.2 Copyright (c) 2007-2026 the FFmpeg developers
$ python -c 'import PyQt6, Metal, Quartz, VideoToolbox'
imports ok
$ python -c 'import av, moderngl, numpy; print(av.__version__, moderngl.__version__, numpy.__version__)'
PyAV 18.1.0 | moderngl 5.12.0 | numpy 2.5.2
```

## CPU / 内存
```
Intel(R) Core(TM) i9-9900K CPU @ 3.60GHz
ncpu: 16  mem: 32 GiB
```

## GPU 与显示器（system_profiler SPDisplaysDataType）
```
Graphics/Displays:

    AMD Radeon RX 6900 XT:

      Chipset Model: AMD Radeon RX 6900 XT
      Type: GPU
      Bus: PCIe
      PCIe Lane Width: x16
      VRAM (Total): 16 GB
      Vendor: AMD (0x1002)
      Device ID: 0x73bf
      Revision ID: 0x00c0
      ROM Revision: xxx-xxx-xxx
      Metal Support: Metal 3
      Displays:
        PHL 439P1:
          Resolution: 3840 x 2160 (2160p/4K UHD 1 - Ultra High Definition)
          UI Looks like: 3840 x 2160 @ 60.00Hz
          Framebuffer Depth: 24-Bit Color (ARGB8888)
          Display Serial Number: AU02211008791
          Main Display: Yes
          Mirror: Off
          Online: Yes
          Rotation: Supported
          Connection Type: Thunderbolt/DisplayPort
          Television: Yes

```

## DaVinci Resolve
```
21.2.0
```
