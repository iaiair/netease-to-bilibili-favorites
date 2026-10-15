#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一入口：

  双击（无参数）        -> 打开图形界面
  带参数（--playlist …）-> 走命令行模式
"""

import sys


def run_cli():
    import bili_sync
    try:
        bili_sync.main()
    except KeyboardInterrupt:
        print("\n已中断。")
    except SystemExit:
        raise
    except Exception as err:  # noqa: BLE001
        print(f"\n[错误] {err}")
        if getattr(sys, "frozen", False):
            try:
                input("\n按回车键退出…")
            except (EOFError, KeyboardInterrupt):
                pass
        sys.exit(1)


def main():
    if len(sys.argv) > 1:
        run_cli()
    else:
        import bili_gui
        bili_gui.main()


if __name__ == "__main__":
    main()
