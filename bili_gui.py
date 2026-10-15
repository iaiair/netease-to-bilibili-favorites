#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图形界面：网易云歌单 -> B站收藏夹 搬运工具。

所有网络请求都在后台线程执行，通过队列回传结果，不会卡住窗口。
"""

import json
import os
import queue
import sys
import threading
import time
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox

import bili_sync as core

APP_TITLE = "网易云歌单 → B站收藏夹 搬运工具"
DEFAULT_PORT = 9222

# 匹配列表的列定义：(标题, 默认宽度, 对齐)
# 宽度是按「表头和最长的一条数据都放得下」实测出来的，再窄就会截断。
# 用户可以拖动调整，调整结果记在 gui_config.json 里。
TREE_COLUMNS = {
    "sel":   ("勾选", 48, "center"),
    "song":  ("原曲（歌名 - 歌手）", 240, "w"),
    "title": ("B站视频标题", 330, "w"),
    "dur":   ("时长对比", 96, "center"),
    "data":  ("播放 / 收藏", 112, "center"),
    "bv":    ("BV号", 106, "center"),
    "state": ("匹配状态", 78, "center"),
    "flag":  ("标记", 96, "center"),
    "play":  ("播放", 42, "center"),
}

TAG_COLORS = {
    "loose": {"background": "#FFF6D6"},     # 宽松：浅黄
    "none": {"foreground": "#9A9A9A"},      # 无候选：灰字
    "skipped": {"foreground": "#9A9A9A"},   # 已在收藏夹里、被跳过：灰字
    "manual": {"background": "#E4F1FF"},    # 手动指定：浅蓝
    "done": {"background": "#E6F7E6"},      # 已写入：浅绿
    "fail": {"background": "#FFE6E6"},      # 写入失败：浅红
}


def app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def default_workdir():
    """工作目录：默认在程序旁边，可用环境变量 BILI_SYNC_WORKDIR 指定到别处。"""
    override = os.environ.get("BILI_SYNC_WORKDIR")
    if override:
        return Path(override)
    return app_dir() / "bili_migrate"


def set_dpi_awareness():
    """Windows 缩放 125%/150% 下避免界面发虚。"""
    if sys.platform != "win32":
        return
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass


def hide_console():
    """打包成 exe 双击运行时，把后面的黑色控制台窗口收掉。"""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    try:
        from ctypes import windll
        hwnd = windll.kernel32.GetConsoleWindow()
        if hwnd:
            windll.user32.ShowWindow(hwnd, 0)
    except Exception:  # noqa: BLE001
        pass


class App:
    def __init__(self, root):
        self.root = root
        self.workdir = default_workdir()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.cfg_path = app_dir() / "gui_config.json"

        self.q = queue.Queue()
        self.rows = []        # 歌单原始行
        self.items = {}       # iid(str) -> 该行状态
        self.cands = {}       # iid(str) -> 候选列表
        self.raw_cands = {}   # iid(str) -> 搜索原始结果（候选全被过滤时兜底）
        self.folders = []     # 收藏夹列表
        self.playlist_name = ""
        self.busy = False
        self.stop_flag = threading.Event()

        root.title(APP_TITLE)
        root.geometry("1180x720")
        root.minsize(940, 560)

        self._build_style()
        self._build_top()
        self._build_tree()
        self._build_prepare()
        self._build_bottom()
        self._load_config()
        core.load_blacklist(self.workdir)
        core.load_whitelist(self.workdir)
        core.load_sort_rules(self.workdir)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(80, self._pump)

        self.log("就绪。第一步：粘贴网易云歌单链接，点「读取歌单」。")
        self.log("提示：全程不会写入任何东西，直到你点「写入收藏夹」。")
        self.log(f"黑名单词表已载入：{len(core.BLACKLIST_TERMS)} 个词，"
                 f'可在「黑名单…」里编辑。')
        self.log(f"白名单词表已载入：{len(core.WHITELIST_TERMS)} 个词"
                 f'（{"/".join(core.WHITELIST_TERMS) or "空"}），可在「白名单…」里编辑。')
        self.log("排序优先级：" +
                 " > ".join(core.SORT_RULE_LABELS[r] for r in core.ACTIVE_SORT_ORDER) +
                 "（可在「排序…」里调整）")
        self.log(f"工作目录：{self.workdir}")

    # ------------------------------------------------------------ 界面搭建
    def _build_style(self):
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=26)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TLabel", font=("Microsoft YaHei UI", 9))
        style.configure("TButton", font=("Microsoft YaHei UI", 9))
        style.configure("Big.TButton", font=("Microsoft YaHei UI", 10, "bold"))

    def _build_top(self):
        top = ttk.Frame(self.root, padding=(10, 10, 10, 6))
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)

        # ---------- ① 载入歌单信息 ----------
        ttk.Label(top, text="① 载入歌单信息").grid(row=0, column=0, sticky="w")
        self.var_link = tk.StringVar()
        ttk.Entry(top, textvariable=self.var_link).grid(row=0, column=1, sticky="ew", padx=8)
        self.btn_load = ttk.Button(top, text="读取歌单", command=self.load_playlist, width=12)
        self.btn_load.grid(row=0, column=2)

        self.var_playlist = tk.StringVar(value="尚未读取歌单")
        ttk.Label(top, textvariable=self.var_playlist, foreground="#1B5E20").grid(
            row=1, column=1, columnspan=2, sticky="w", padx=8, pady=(4, 0))

        # ---------- ② 匹配 ----------
        m = ttk.LabelFrame(top, text="② 匹配歌单和视频", padding=(10, 6, 10, 8))
        m.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        m.columnconfigure(1, weight=1)

        self.var_translate = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            m,
            text="将歌曲名翻译为中文添加匹配（用网易云自带的中文译名，日文歌英文歌都适用）",
            variable=self.var_translate,
        ).grid(row=0, column=0, columnspan=3, sticky="w")

        self.var_deep = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            m,
            text="深查视频详情（识别多P视频、查 UP主签名判定官方MV；会明显变慢）",
            variable=self.var_deep,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 0))

        self.btn_match = ttk.Button(m, text="开始匹配", command=self.start_match,
                                    style="Big.TButton", width=14)
        self.btn_match.grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.btn_stop = ttk.Button(m, text="停止", command=self.stop_work, width=8,
                                   state="disabled")
        self.btn_stop.grid(row=2, column=1, sticky="w", padx=(6, 0), pady=(8, 0))

    def _build_prepare(self):
        """③ 准备写入：放在匹配列表下面，和「先匹配、后写入」的实际流程一致。"""
        p = ttk.LabelFrame(self.root, text="③ 准备写入", padding=(10, 6, 10, 8))
        p.pack(fill="x", padx=10, pady=(8, 0))
        p.columnconfigure(1, weight=1)

        self.has_browser = bool(core.detect_browser_name())
        ttk.Label(p, text="启动独立浏览器").grid(row=0, column=0, sticky="w")
        self.var_browse_hint = tk.StringVar(
            value="需要启动 Chrome 浏览器打开新窗口，自行登录 B站"
            if self.has_browser else "没找到 Google Chrome，请先安装")
        ttk.Label(p, textvariable=self.var_browse_hint, foreground="#666666").grid(
            row=0, column=1, sticky="w", padx=8)
        self.btn_browse = ttk.Button(
            p, text="启动 Chrome 登录" if self.has_browser else "未找到 Chrome",
            command=self.launch_browser, width=16,
            state="normal" if self.has_browser else "disabled")
        self.btn_browse.grid(row=0, column=2, sticky="e")

        ttk.Label(p, text="目标收藏夹").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.var_folder = tk.StringVar()
        self.cmb_folder = ttk.Combobox(p, textvariable=self.var_folder, state="readonly")
        self.cmb_folder.grid(row=1, column=1, sticky="ew", padx=8, pady=(10, 0))
        self.cmb_folder.bind("<<ComboboxSelected>>", self._on_folder_selected)

        box = ttk.Frame(p)
        box.grid(row=1, column=2, sticky="e", pady=(10, 0))
        ttk.Button(box, text="刷新", command=self.refresh_folders, width=6).pack(side="left")
        ttk.Button(box, text="新建…", command=self.create_folder_dialog, width=7).pack(
            side="left", padx=(4, 0))

        self.var_folder_hint = tk.StringVar(
            value="留空则自动新建一个与歌单同名的收藏夹。")
        ttk.Label(p, textvariable=self.var_folder_hint, foreground="#666666").grid(
            row=2, column=1, columnspan=2, sticky="w", padx=8, pady=(4, 0))

    def _build_tree(self):
        wrap = ttk.Frame(self.root, padding=(10, 0, 10, 0))
        wrap.pack(fill="both", expand=True)

        cols = tuple(TREE_COLUMNS)
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        for key, (text, width, anchor) in TREE_COLUMNS.items():
            self.tree.heading(key, text=text, command=lambda k=key: self._sort_by(k))
            # 全部 stretch=False：否则拖动靠后的列时，宽度会被前面可伸缩的列吃掉，
            # 表现出来就是"后面几列改不动"。溢出交给横向滚动条。
            self.tree.column(key, width=width, minwidth=28, anchor=anchor, stretch=False)

        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        for name, opts in TAG_COLORS.items():
            self.tree.tag_configure(name, **opts)

        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-3>", self._on_right_click)
        self.tree.bind("<Control-c>", lambda _e: (self.copy_bv(), "break")[1])
        self.tree.bind("<Control-C>", lambda _e: (self.copy_bv(), "break")[1])

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="复制 BV 号", command=self.copy_bv)
        self.menu.add_separator()
        self.menu.add_command(label="换一个候选视频…", command=self.pick_candidate_dialog)
        self.menu.add_command(label="重新搜索（改关键词）…", command=self.research_dialog)
        self.menu.add_command(label="手动填写 BV 号…", command=self.manual_bv_dialog)
        self.menu.add_separator()
        self.menu.add_command(label="切换勾选状态", command=self.toggle_selected)
        self.menu.add_command(label="在登录浏览器里打开视频", command=self.open_video)

    def _build_bottom(self):
        bottom = ttk.Frame(self.root, padding=(10, 6, 10, 10))
        bottom.pack(fill="x")

        line1 = ttk.Frame(bottom)
        line1.pack(fill="x")
        self.var_stats = tk.StringVar(value="已勾选 0 │ 严格 0 · 宽松 0 · 无候选 0")
        ttk.Label(line1, textvariable=self.var_stats).pack(side="left")
        ttk.Button(line1, text="仅选严格", command=lambda: self.select_bulk("strict"),
                   width=10).pack(side="right")
        ttk.Button(line1, text="全选", command=lambda: self.select_bulk("all"),
                   width=8).pack(side="right", padx=4)
        ttk.Button(line1, text="全不选", command=lambda: self.select_bulk("none"),
                   width=8).pack(side="right")

        line2 = ttk.Frame(bottom)
        line2.pack(fill="x", pady=(8, 0))
        self.progress = ttk.Progressbar(line2, mode="determinate", length=260)
        self.progress.pack(side="left")
        self.var_status = tk.StringVar(value="等待操作")
        ttk.Label(line2, textvariable=self.var_status).pack(side="left", padx=10)

        line3 = ttk.Frame(bottom)
        line3.pack(fill="x", pady=(8, 0))
        self.btn_export = ttk.Button(line3, text="导出对照表", command=self.export_report,
                                     width=12)
        self.btn_export.pack(side="left")
        self.btn_blacklist = ttk.Button(line3, text="黑名单…",
                                        command=self.edit_blacklist_dialog, width=11)
        self.btn_blacklist.pack(side="left", padx=6)
        self.btn_whitelist = ttk.Button(line3, text="白名单…",
                                        command=self.edit_whitelist_dialog, width=11)
        self.btn_whitelist.pack(side="left")
        self.btn_sort = ttk.Button(line3, text="排序…",
                                   command=self.edit_sort_dialog, width=9)
        self.btn_sort.pack(side="left", padx=6)
        self.btn_push = ttk.Button(line3, text="写入收藏夹 (0)", command=self.start_push,
                                   style="Big.TButton", width=16)
        self.btn_push.pack(side="right")

        self.var_hint = tk.StringVar(
            value="按 ①→②→③ 的顺序操作：读歌单 → 开始匹配 → 启动浏览器登录并选收藏夹；"
                  "核对无误后点右下角「写入收藏夹」。")
        ttk.Label(bottom, textvariable=self.var_hint, foreground="#666666").pack(
            anchor="w", pady=(6, 0))

    # ------------------------------------------------------------ 基础工具
    def log(self, msg):
        self.q.put(("log", msg))

    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.var_hint.set(payload)
                elif kind == "playlist":
                    self._fill_rows(payload)
                elif kind == "row":
                    self._update_row(*payload)
                elif kind == "progress":
                    cur, total = payload
                    self.progress.configure(maximum=max(total, 1), value=cur)
                    self.var_status.set(f"{cur}/{total}")
                elif kind == "status":
                    self.var_status.set(payload)
                elif kind == "folders":
                    self._fill_folders(payload)
                elif kind == "busy":
                    self._set_busy(payload)
                elif kind == "error":
                    self._set_busy(False)
                    messagebox.showerror("出错了", payload)
                elif kind == "info":
                    messagebox.showinfo("提示", payload)
        except queue.Empty:
            pass
        self.root.after(80, self._pump)

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for btn in (self.btn_load, self.btn_match, self.btn_push):
            btn.configure(state=state)
        self.btn_browse.configure(
            state=state if getattr(self, "has_browser", False) else "disabled")
        self.btn_stop.configure(state="normal" if busy else "disabled")

    def _load_config(self):
        try:
            cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.var_link.set(cfg.get("link", ""))
        self.var_translate.set(bool(cfg.get("translate", True)))
        self.var_tip_enabled = bool(cfg.get("show_match_tip", True))
        for col, width in (cfg.get("col_widths") or {}).items():
            try:
                if col in self.tree["columns"]:
                    self.tree.column(col, width=int(width))
            except (tk.TclError, TypeError, ValueError):
                pass
        geo = cfg.get("geometry")
        if geo:
            try:
                self.root.geometry(geo)
            except tk.TclError:
                pass

    def _save_config(self):
        try:
            widths = {}
            for col in self.tree["columns"]:
                try:
                    widths[col] = int(self.tree.column(col, "width"))
                except (tk.TclError, TypeError, ValueError):
                    pass
            self.cfg_path.write_text(json.dumps({
                "link": self.var_link.get(),
                "translate": bool(self.var_translate.get()),
                "show_match_tip": bool(getattr(self, "var_tip_enabled", True)),
                "col_widths": widths,
                "geometry": self.root.geometry(),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------ 列表渲染
    def _fill_rows(self, rows):
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        self.cands.clear()
        self.raw_cands.clear()
        self.rows = rows
        for r in rows:
            idx = str(r["序号"])
            ne_sec = int(r["时长秒"]) if r.get("时长秒") else None
            it = {
                "idx": int(r["序号"]), "song": r["歌名"], "artists": r["歌手"],
                "album": r.get("专辑", ""),
                "ne_sec": ne_sec, "title": "", "vsec": None, "bv": "", "aid": None,
                "up": "", "level": "", "checked": False, "wrote": False, "note": "",
                "trans": [v for v in (r.get("译名") or "").split("|") if v],
                "play": None, "fav": None, "official_mv": False,
                "whitelist_hit": False, "via_translation": False, "multi": False,
                "skipped": False, "skipped_reason": "", "match_note": "",
                "发布时间": None, "弹幕": None,
                "part": "",
            }
            self.items[idx] = it
            self.tree.insert("", "end", iid=idx, values=(
                "☐", self._song_label(it),
                "", core.fmt_duration(ne_sec), "", "", "待匹配", "", ""))
        self.var_playlist.set(f'✓ {self.playlist_name} · 共 {len(rows)} 首')
        self.refresh_stats()
        self.log(f"已载入歌单：{self.playlist_name}，共 {len(rows)} 首。下一步点「开始匹配」。")

    def _update_row(self, iid, patch):
        it = self.items.get(iid)
        if not it:
            return
        it.update(patch)
        if not self.tree.exists(iid):
            return
        self.tree.item(iid, values=(
            "☑" if it["checked"] else "☐",
            self._song_label(it),
            it["title"] or it["note"] or "",
            f'{core.fmt_duration(it["ne_sec"])} → {core.fmt_duration(it["vsec"])}',
            self._data_label(it),
            it["bv"], self._state_text(it), self._flag_text(it),
            "▶" if it["bv"] else "",
        ))
        tags = []
        if it.get("wrote"):
            tags.append("done")
        if it.get("failed"):
            tags.append("fail")
        if it.get("skipped"):
            tags.append("skipped")
        if it.get("manual"):
            tags.append("manual")
        if it["level"] == "loose":
            tags.append("loose")
        elif it["level"] == "none":
            tags.append("none")
        self.tree.item(iid, tags=tuple(tags))
        self.refresh_stats()

    def _song_label(self, it):
        """原曲列：歌名 - 歌手，如果有中文译名就附在后面，方便核对。"""
        base = f'{it["song"]} - {it["artists"]}'.strip(" -")
        trans = it.get("trans") or []
        return f"{base}　〔译名：{trans[0]}〕" if trans else base

    @staticmethod
    def _short_num(n):
        if not n:
            return "--"
        if n >= 10000:
            return f"{n / 10000:.1f}万"
        return str(n)

    def _data_label(self, it):
        return f'{self._short_num(it.get("play"))} / {self._short_num(it.get("fav"))}'

    def _flag_text(self, it):
        marks = []
        if it.get("official_mv"):
            marks.append("官方MV")
        if it.get("whitelist_hit"):
            marks.append("白名单")
        if it.get("multi"):
            marks.append("合唱?")
        if it.get("part"):
            marks.append("多P")
        if it.get("manual"):
            marks.append("手动")
        if it.get("skipped"):
            marks.append("已收藏跳过")
        if it.get("via_translation"):
            marks.append("译名")
        return "·".join(marks)

    def _state_text(self, it):
        if it.get("wrote"):
            return "本轮已写入"
        if it.get("failed"):
            return "写入失败"
        if it.get("skipped"):
            return "已收藏跳过"
        if it.get("manual"):
            return "手动指定"
        return {"strict": "严格", "loose": "宽松", "none": "无候选"}.get(it["level"], "待匹配")

    def refresh_stats(self):
        cnt = {"strict": 0, "loose": 0, "none": 0}
        checked = 0
        for it in self.items.values():
            cnt[it["level"]] = cnt.get(it["level"], 0) + 1
            if it["checked"]:
                checked += 1
        self.var_stats.set(
            f'已勾选 {checked} │ 严格 {cnt.get("strict", 0)}'
            f' · 宽松 {cnt.get("loose", 0)} · 无候选 {cnt.get("none", 0)}')
        self.btn_push.configure(text=f"写入收藏夹 ({checked})")

    def _sort_by(self, key):
        order = getattr(self, "_sort_state", None)
        reverse = not (order == key and getattr(self, "_sort_rev", False))
        self._sort_state, self._sort_rev = key, reverse

        def sort_key(iid):
            it = self.items[iid]
            if key == "sel":
                return it["checked"]
            if key == "song":
                return it["song"]
            if key == "title":
                return it["title"]
            if key == "bv":
                return it["bv"]
            if key == "dur":
                return it["vsec"] or 0
            return self._state_text(it)

        for k, iid in enumerate(sorted(self.items.keys(), key=sort_key, reverse=reverse)):
            self.tree.move(iid, "", k)

    # ------------------------------------------------------------ 鼠标交互
    def _iid_at(self, event):
        iid = self.tree.identify_row(event.y)
        return iid if iid in self.items else None

    def _on_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        iid = self._iid_at(event)
        if not iid:
            return
        col = self.tree.identify_column(event.x)
        if col == "#1":
            it = self.items[iid]
            it["checked"] = not it["checked"]
            self._update_row(iid, {})
            return "break"
        if col == "#9":                      # 播放小按钮
            self.open_video(iid)
            return "break"

    def _on_double_click(self, event):
        iid = self._iid_at(event)
        if not iid:
            return
        col = self.tree.identify_column(event.x)
        if col == "#6":
            self._inline_edit_bv(iid)
        elif col == "#3":
            self.pick_candidate_dialog()

    def _on_right_click(self, event):
        iid = self._iid_at(event)
        if not iid:
            return
        self.tree.selection_set(iid)
        self.menu.tk_popup(event.x_root, event.y_root)

    def _selected_iid(self):
        sel = self.tree.selection()
        return sel[0] if sel and sel[0] in self.items else None

    def toggle_selected(self):
        iid = self._selected_iid()
        if iid:
            self.items[iid]["checked"] = not self.items[iid]["checked"]
            self._update_row(iid, {})

    def select_bulk(self, mode):
        for iid, it in self.items.items():
            if mode == "all":
                it["checked"] = bool(it["bv"])
            elif mode == "strict":
                it["checked"] = (it["level"] == "strict" and bool(it["bv"]))
            else:
                it["checked"] = False
            self._update_row(iid, {})

    def open_in_browser(self):
        self.open_video()

    def copy_bv(self):
        """把选中那行的 BV 号复制到剪贴板。"""
        iid = self._selected_iid()
        if not iid:
            messagebox.showinfo("提示", "请先在列表里选中一行。")
            return
        bv = self.items[iid].get("bv") or ""
        if not bv:
            messagebox.showinfo("提示", "这一条还没有 BV 号，没法复制。")
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(bv)
            self.root.update()      # 让剪贴板内容在程序关闭后仍然保留
            self.log(f"已复制 BV 号：{bv}")
        except tk.TclError as e:
            messagebox.showerror("复制失败", str(e))

    def open_video(self, iid=None):
        """在取登录信息的那个 Chrome 里新开标签页打开视频，方便直接看对不对。"""
        if iid is None:
            iid = self._selected_iid()
        if not iid:
            messagebox.showinfo("提示", "请先在列表里选中一行。")
            return
        bv = self.items[iid].get("bv")
        if not bv:
            messagebox.showinfo("提示", "这一条还没有 BV 号，没法打开。\n"
                                        "可以先「换一个候选」或手动填 BV 号。")
            return
        url = f"https://www.bilibili.com/video/{bv}"
        threading.Thread(target=self._open_worker, args=(url,), daemon=True).start()

    def _open_worker(self, url):
        try:
            if core.open_in_debug_browser(DEFAULT_PORT, url):
                self.q.put(("log", f"已在登录用的浏览器窗口里打开新标签：{url}"))
                return
        except Exception as e:  # noqa: BLE001
            self.q.put(("log", f"在登录浏览器里打开失败（{e}），改用系统默认浏览器。"))
        try:
            webbrowser.open(url)
            self.q.put(("log", f"登录浏览器没在运行，已用系统默认浏览器打开：{url}"))
        except Exception as e:  # noqa: BLE001
            self.q.put(("error", f"打开浏览器失败：\n{e}"))

    # ------------------------------------------------------------ 阶段1：读取歌单
    def load_playlist(self):
        link = self.var_link.get().strip()
        if not link:
            messagebox.showwarning("提示", "请先粘贴网易云歌单的分享链接。")
            return
        self._set_busy(True)
        self.q.put(("status", "正在读取歌单…"))
        threading.Thread(target=self._load_worker, args=(link,), daemon=True).start()

    def _load_worker(self, link):
        try:
            rows, meta = core.do_export(link, self.workdir, None, refresh=True)
            self.playlist_name = meta.get("歌单名", "")
            self.q.put(("playlist", rows))
            self.q.put(("status", f"歌单已载入：{len(rows)} 首"))
        except Exception as e:  # noqa: BLE001
            self.q.put(("error", f"读取歌单失败：\n{e}"))
        finally:
            self.q.put(("busy", False))

    # ------------------------------------------------------------ 阶段2：匹配
    def show_match_tip(self):
        """点「开始匹配」后提示一次：不放心程序写入的话可以自己收藏。"""
        if not getattr(self, "var_tip_enabled", True):
            return
        win = tk.Toplevel(self.root)
        win.title("开始匹配")
        win.transient(self.root)
        win.grab_set()
        win.resizable(False, False)

        bar = ttk.Frame(win, padding=10)
        bar.pack(side="bottom", fill="x")
        body = ttk.Frame(win, padding=(16, 14, 16, 6))
        body.pack(side="top", fill="both", expand=True)

        ttk.Label(body, text="如果你不放心让程序写入收藏夹，可以自己动手：",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", pady=(0, 6))
        for line in (
            "· 匹配完在列表里右键某一行 →「复制 BV 号」，",
            "  把 BV 号粘到 B站 搜索框里自己收藏",
            "· 或点每行最右边的「▶」，程序会在登录用的浏览器里打开该视频，",
            "  你自己点收藏即可",
            "",
            "程序只在你点「写入收藏夹」的那一刻才会动你的账号。",
            "在那之前它只做两件事：读歌单、搜视频。",
        ):
            ttk.Label(body, text=line).pack(anchor="w")

        var_no = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="以后不再提示", variable=var_no).pack(side="left")

        def close(_e=None):
            if var_no.get():
                self.var_tip_enabled = False
                self._save_config()
            win.destroy()

        ttk.Button(bar, text="知道了", command=close, width=10).pack(side="right")
        win.bind("<Return>", close)
        win.bind("<Escape>", close)

        win.update_idletasks()
        w = max(win.winfo_reqwidth(), 470)
        h = win.winfo_reqheight()
        x = self.root.winfo_rootx() + max((self.root.winfo_width() - w) // 2, 0)
        y = self.root.winfo_rooty() + max((self.root.winfo_height() - h) // 2, 0)
        win.geometry(f"{w}x{h}+{x}+{y}")
        win.wait_window()

    def start_match(self):
        if not self.items:
            messagebox.showwarning("提示", "请先读取歌单。")
            return
        self.show_match_tip()
        self.stop_flag.clear()
        self._set_busy(True)
        self.q.put(("status", "开始匹配…"))
        threading.Thread(target=self._match_worker, daemon=True).start()

    def _match_worker(self):
        try:
            searcher = core.BiliSearch()
            translate = bool(self.var_translate.get())
            deep = bool(self.var_deep.get())
            core.bind_sign_cache(self.workdir)
            todo = [it for it in self.items.values() if not it.get("wrote")]
            total = len(todo)
            for n, it in enumerate(todo, 1):
                if self.stop_flag.is_set():
                    break
                iid = str(it["idx"])
                first = it["artists"].split("/")[0] if it["artists"] else ""
                keyword = f'{it["song"]} {first}'.strip()
                alt_names = list(it.get("trans") or []) if translate else []
                try:
                    cands = core.search_candidates(
                        searcher, it["song"], it["artists"],
                        alt_names=alt_names, pace=0.5)
                    ranked, _ = core.rank_candidates(
                        cands, it["song"], it["artists"], it["ne_sec"],
                        alt_names=alt_names, album=it.get("album", ""),
                        want_fetch=deep)
                    self.cands[iid] = ranked
                    self.raw_cands[iid] = cands
                    best, level, note = core.pick_best(
                        cands, it["song"], it["artists"], it["ne_sec"],
                        alt_names=alt_names, album=it.get("album", ""),
                        want_fetch=deep)
                except Exception as e:  # noqa: BLE001
                    best, level, note = None, "none", f"搜索出错：{str(e)[:60]}"

                patch = {"level": {"严格": "strict", "宽松": "loose"}.get(level, "none"),
                         "note": note, "match_note": note}
                if best:
                    patch.update({
                        "title": best["标题"], "vsec": best["时长秒"], "bv": best["BV号"],
                        "aid": best["aid"], "up": best["UP主"],
                        "play": best.get("播放量"), "fav": best.get("收藏量"),
                        "发布时间": best.get("发布时间"), "弹幕": best.get("弹幕"),
                        "official_mv": bool(best.get("official_mv")),
                        "whitelist_hit": bool(best.get("whitelist_hit")),
                        "via_translation": bool(best.get("via_translation")),
                        "multi": bool(best.get("multi")),
                        "part": best.get("分P") or "",
                    })
                    # 严格匹配默认勾选；宽松和无候选留给你人工确认
                    patch["checked"] = (level == "严格")
                else:
                    patch.update({"title": "", "vsec": None, "bv": "", "aid": None,
                                  "play": None, "fav": None, "official_mv": False,
                                  "whitelist_hit": False, "via_translation": False,
                        "multi": False, "part": "", "match_note": note})
                    patch["checked"] = False
                self.q.put(("row", (iid, patch)))
                self.q.put(("progress", (n, total)))
                if n % 3 == 0:
                    self.q.put(("log", f"匹配中… {n}/{total}"))
                time.sleep(1.0)

            self._save_report()
            core.flush_sign_cache()
            self.q.put(("log", "匹配完成。请逐条核对，尤其注意黄色的「宽松」项。"))
        except Exception as e:  # noqa: BLE001
            self.q.put(("error", f"匹配失败：\n{e}"))
        finally:
            self.q.put(("busy", False))

    def _save_report(self):
        try:
            fields = ["序号", "歌名", "歌手", "原曲时长", "匹配状态", "B站标题", "UP主",
                      "播放量", "收藏量", "视频时长", "BV号", "aid", "标记", "备注"]
            path = self.workdir / "report.csv"
            import csv
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                # 和列表显示顺序保持一致，方便对照
                order = [self.items[i] for i in self.tree.get_children()
                         if i in self.items]
                if not order:
                    order = sorted(self.items.values(), key=lambda x: x["idx"])
                for it in order:
                    w.writerow({
                        "序号": it["idx"], "歌名": it["song"], "歌手": it["artists"],
                        "原曲时长": core.fmt_duration(it["ne_sec"]),
                        "匹配状态": self._state_text(it),
                        "B站标题": it["title"], "UP主": it["up"],
                        "播放量": it.get("play") or "", "收藏量": it.get("fav") or "",
                        "视频时长": core.fmt_duration(it["vsec"]),
                        "BV号": it["bv"], "aid": it["aid"],
                        "标记": self._flag_text(it), "备注": it["note"],
                    })
            (self.workdir / "candidates.json").write_text(
                json.dumps(self.cands, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            self.q.put(("log", f"写出对照表失败：{e}"))

    def export_report(self):
        if not self.items:
            messagebox.showwarning("提示", "还没有数据可以导出。")
            return
        self._save_report()
        messagebox.showinfo("已导出", f"对照表已保存到：\n{self.workdir / 'report.csv'}")

    # ------------------------------------------------------------ 词表编辑
    def edit_blacklist_dialog(self):
        self.edit_terms_dialog("blacklist")

    def edit_whitelist_dialog(self):
        self.edit_terms_dialog("whitelist")

    def edit_sort_dialog(self):
        """左右两栏的规则配置：左边未启用，右边已启用（上=优先级高）。"""
        enabled = list(core.ACTIVE_SORT_ORDER)
        disabled = list(core.ACTIVE_SORT_DISABLED)
        if not disabled:
            disabled = [r for r in core.SORT_RULE_LABELS if r not in enabled]

        win = tk.Toplevel(self.root)
        win.title("默认选择的排序规则")
        win.transient(self.root)
        win.grab_set()
        win.minsize(720, 470)

        # 底部按钮栏先钉住，避免被中间内容挤出可视区
        bar = ttk.Frame(win, padding=10)
        bar.pack(side="bottom", fill="x")

        ttk.Label(win, justify="left", wraplength=690, padding=(12, 10, 12, 2), text=(
            "左边是未启用的规则，右边是已启用的规则。\n"
            "右边从上到下就是优先级：越靠上越优先选中，官方 MV 排第一时直接定选。\n"
            "选中一项后点中间箭头移动；双击也能直接启用或停用。"
        )).pack(side="top", anchor="w")

        mid = ttk.Frame(win, padding=(12, 6, 12, 6))
        mid.pack(side="top", fill="both", expand=True)
        mid.columnconfigure(0, weight=1)
        mid.columnconfigure(2, weight=1)
        mid.rowconfigure(1, weight=1)

        ttk.Label(mid, text="未启用").grid(row=0, column=0, sticky="w")
        ttk.Label(mid, text="已启用（上 = 优先级高）").grid(row=0, column=2, sticky="w")

        lb_left = tk.Listbox(mid, exportselection=False, activestyle="dotbox",
                             font=("Microsoft YaHei UI", 10), height=9)
        lb_right = tk.Listbox(mid, exportselection=False, activestyle="dotbox",
                              font=("Microsoft YaHei UI", 10), height=9)
        lb_left.grid(row=1, column=0, sticky="nsew", pady=(2, 0))
        lb_right.grid(row=1, column=2, sticky="nsew", pady=(2, 0))

        def label_to_rule(label):
            for key, text in core.SORT_RULE_LABELS.items():
                if text == label:
                    return key
            return None

        def refresh():
            lb_left.delete(0, "end")
            lb_right.delete(0, "end")
            for r in disabled:
                lb_left.insert("end", core.SORT_RULE_LABELS[r])
            for r in enabled:
                lb_right.insert("end", core.SORT_RULE_LABELS[r])

        def picked(lb):
            sel = lb.curselection()
            return label_to_rule(lb.get(sel[0])) if sel else None

        def enable_rule(_e=None):
            rule = picked(lb_left)
            if not rule:
                return
            disabled.remove(rule)
            enabled.append(rule)          # 追加到末尾 = 优先级最低
            refresh()
            lb_right.selection_clear(0, "end")
            lb_right.selection_set("end")

        def disable_rule(_e=None):
            rule = picked(lb_right)
            if not rule:
                return
            enabled.remove(rule)
            disabled.append(rule)
            refresh()
            lb_left.selection_clear(0, "end")
            lb_left.selection_set("end")

        def move(delta):
            sel = lb_right.curselection()
            if not sel:
                return
            i = sel[0]
            j = i + delta
            if 0 <= j < len(enabled):
                enabled[i], enabled[j] = enabled[j], enabled[i]
                refresh()
                lb_right.selection_clear(0, "end")
                lb_right.selection_set(j)

        arrows = ttk.Frame(mid)
        arrows.grid(row=1, column=1, padx=12)
        ttk.Label(arrows, text="→", font=("Microsoft YaHei UI", 13, "bold")).pack(pady=(0, 2))
        ttk.Button(arrows, text="启用", command=enable_rule, width=8).pack(pady=2)
        ttk.Button(arrows, text="停用", command=disable_rule, width=8).pack(pady=(10, 2))
        ttk.Label(arrows, text="←", font=("Microsoft YaHei UI", 13, "bold")).pack(pady=(2, 0))

        updown = ttk.Frame(mid)
        updown.grid(row=2, column=2, sticky="e", pady=(6, 0))
        ttk.Button(updown, text="↑ 上移", command=lambda: move(-1), width=9).pack(side="left")
        ttk.Button(updown, text="↓ 下移", command=lambda: move(1), width=9).pack(
            side="left", padx=6)

        lb_left.bind("<Double-1>", enable_rule)
        lb_right.bind("<Double-1>", disable_rule)
        refresh()

        def restore():
            enabled[:] = list(core.DEFAULT_SORT_ORDER)
            disabled[:] = list(core.DEFAULT_SORT_DISABLED)
            refresh()

        def save():
            if not enabled:
                messagebox.showwarning("提示", "至少要启用一条规则。", parent=win)
                return
            core.save_sort_rules(self.workdir, enabled, disabled)
            win.destroy()
            self.log("排序优先级：" +
                     " > ".join(core.SORT_RULE_LABELS[r] for r in core.ACTIVE_SORT_ORDER))
            messagebox.showinfo("已保存", "排序规则已更新。\n\n重新点「开始匹配」即可生效。")

        ttk.Button(bar, text="恢复默认", command=restore, width=10).pack(side="left")
        ttk.Button(bar, text="取消", command=win.destroy, width=10).pack(side="right")
        ttk.Button(bar, text="保存", command=save, width=10).pack(side="right", padx=6)
        ttk.Label(bar, text="（Ctrl+S 也能保存）", foreground="#888888").pack(
            side="right", padx=(0, 10))
        for seq in ("<Control-s>", "<Control-S>", "<Control-Return>"):
            win.bind(seq, lambda _e: (save(), "break")[1])

        win.update_idletasks()
        w, h = 760, 540
        x = self.root.winfo_rootx() + max((self.root.winfo_width() - w) // 2, 0)
        y = self.root.winfo_rooty() + max((self.root.winfo_height() - h) // 2, 0)
        win.geometry(f"{w}x{h}+{x}+{y}")

    def edit_terms_dialog(self, kind):
        is_white = (kind == "whitelist")
        if is_white:
            terms = list(core.WHITELIST_TERMS)
            defaults = core.DEFAULT_WHITELIST_TERMS
            save_fn, path_fn = core.save_whitelist, core.whitelist_path
            title = "编辑优先白名单"
            desc = ("每行一个词。标题里出现其中任何一个词，这条候选就会被优先选择\n"
                    "（排在播放量更高的候选前面）。用来优先挑「无损」「Hi-Res」这类版本。\n"
                    "不区分大小写。以 # 开头的行会被忽略。")
        else:
            terms = list(core.BLACKLIST_TERMS)
            defaults = core.DEFAULT_BLACKLIST_TERMS
            save_fn, path_fn = core.save_blacklist, core.blacklist_path
            title = "编辑排除黑名单"
            desc = ("每行一个词。视频标题里只要出现其中任何一个词，这条候选会被直接排除。\n"
                    "不区分大小写。以 # 开头的行会被忽略（可当注释用）。\n"
                    "注意：如果歌名本身就含某个词（例如歌名里带「翻唱」），该词会自动跳过，"
                    "不会误伤自己。")

        win = tk.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        win.grab_set()
        win.minsize(520, 380)

        # 先把按钮栏钉在底部，再让文本区吃掉剩余空间。
        # 反过来的话，文本区（Text 默认 24 行高）会把按钮栏挤出窗口。
        bar = ttk.Frame(win, padding=10)
        bar.pack(side="bottom", fill="x")

        ttk.Label(win, justify="left", padding=(10, 10, 10, 4),
                  text=desc, wraplength=620).pack(side="top", anchor="w")

        frame = ttk.Frame(win, padding=(10, 0, 10, 0))
        frame.pack(side="top", fill="both", expand=True)
        txt = tk.Text(frame, wrap="none", font=("Microsoft YaHei UI", 10),
                      undo=True, relief="solid", borderwidth=1,
                      height=16, width=60)
        sb = ttk.Scrollbar(frame, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        txt.insert("1.0", "\n".join(terms))
        txt.focus_set()

        var_count = tk.StringVar()

        def read_terms():
            return [ln.strip() for ln in txt.get("1.0", "end").splitlines()
                    if ln.strip() and not ln.strip().startswith("#")]

        def refresh_count(*_a):
            var_count.set(f"当前 {len(read_terms())} 个词")

        txt.bind("<KeyRelease>", refresh_count)
        refresh_count()

        def save():
            saved = save_fn(self.workdir, read_terms())
            win.destroy()
            self.log(f'{"白" if is_white else "黑"}名单已保存：{len(saved)} 个词 -> '
                     f'{path_fn(self.workdir)}')
            messagebox.showinfo(
                "已保存",
                f'{"白" if is_white else "黑"}名单已更新，共 {len(saved)} 个词。\n\n'
                "重新点「开始匹配」即可生效。")

        def restore():
            txt.delete("1.0", "end")
            txt.insert("1.0", "\n".join(defaults))
            refresh_count()

        ttk.Label(bar, textvariable=var_count, foreground="#666666").pack(side="left")
        ttk.Button(bar, text="恢复默认", command=restore, width=10).pack(side="left", padx=10)
        ttk.Button(bar, text="取消", command=win.destroy, width=10).pack(side="right")
        ttk.Button(bar, text="保存", command=save, width=10).pack(side="right", padx=6)
        ttk.Label(bar, text="（Ctrl+S 也能保存）", foreground="#888888").pack(
            side="right", padx=(0, 10))

        # Ctrl+S / Ctrl+回车 都能保存
        for seq in ("<Control-s>", "<Control-S>", "<Control-Return>"):
            win.bind(seq, lambda _e: (save(), "break")[1])

        win.update_idletasks()
        w, h = 660, 560
        x = self.root.winfo_rootx() + max((self.root.winfo_width() - w) // 2, 0)
        y = self.root.winfo_rooty() + max((self.root.winfo_height() - h) // 2, 0)
        win.geometry(f"{w}x{h}+{x}+{y}")

    # ------------------------------------------------------------ 浏览器与收藏夹
    def launch_browser(self):
        ws, ver = core.debug_endpoint(DEFAULT_PORT)
        if ws:
            messagebox.showinfo("浏览器已在运行",
                                f"检测到调试浏览器（{ver}）。\n"
                                "请在窗口里确认已经登录 B站，然后点「刷新」读取收藏夹。")
            return
        try:
            profile, freed, _locked = core.launch_browser(self.workdir, DEFAULT_PORT)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("启动失败", str(e))
            return
        if freed:
            self.log(f"启动前清理了上次遗留的浏览器缓存，释放 {freed / 1024 / 1024:.1f} MB。")
        messagebox.showinfo(
            "请在打开的窗口里登录",
            "已启动专用 Chrome（独立配置，不影响你日常的浏览器）。\n\n"
            "1. 在窗口里扫码登录 B站\n"
            "2. 登录后保持窗口打开\n"
            "3. 回到本程序点「刷新」读取你的收藏夹\n\n"
            f'配置目录：{profile}')

    def refresh_folders(self):
        threading.Thread(target=self._folders_worker, daemon=True).start()

    def _folders_worker(self):
        try:
            cookies = core.read_browser_cookies(DEFAULT_PORT)
            if cookies is None:
                self.q.put(("error",
                            "连不上浏览器。请先点「启动浏览器登录」，\n"
                            "在弹出的窗口里登录 B站，并保持窗口打开。"))
                return
            writer = core.BiliWriter(cookies)
            folders = writer.list_folders(writer.my_mid())
            self.q.put(("folders", folders))
        except Exception as e:  # noqa: BLE001
            self.q.put(("error", f"读取收藏夹失败：\n{e}"))

    def _fill_folders(self, folders):
        self.folders = folders
        self.folder_map = {}
        items = ["自动新建（与歌单同名）"]
        self.folder_map[items[0]] = None
        for f in folders:
            label = (f'{f["title"]}｜{f["media_count"]}个·'
                     f'{"私密" if f.get("attr", 0) & 1 else "公开"}')
            items.append(label)
            self.folder_map[label] = f
        self.cmb_folder.configure(values=items)
        pick = items[0]
        if self.playlist_name:
            for label in items[1:]:
                if self.folder_map[label]["title"] == self.playlist_name:
                    pick = label
                    break
        self.var_folder.set(pick)
        self.var_folder_hint.set(
            f"已读取 {len(folders)} 个收藏夹。同一个歌单请始终选同一个夹，避免重复导入。")

    def _on_folder_selected(self, _event=None):
        f = (self.folder_map or {}).get(self.var_folder.get())
        if f:
            self.var_folder_hint.set(
                f'已选：{f["title"]}（id={f["id"]}，现有 {f["media_count"]} 个内容）')
        else:
            self.var_folder_hint.set("将自动新建一个与歌单同名的收藏夹。")

    def create_folder_dialog(self):
        name = self.playlist_name or ""
        win = tk.Toplevel(self.root)
        win.title("新建收藏夹")
        win.transient(self.root)
        win.grab_set()
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="收藏夹名称").grid(row=0, column=0, sticky="w")
        var_name = tk.StringVar(value=name)
        ttk.Entry(frm, textvariable=var_name, width=40).grid(row=0, column=1, padx=8)

        ttk.Label(frm, text="可见性").grid(row=1, column=0, sticky="w", pady=(10, 0))
        var_priv = tk.IntVar(value=0)
        box = ttk.Frame(frm)
        box.grid(row=1, column=1, sticky="w", pady=(10, 0), padx=8)
        ttk.Radiobutton(box, text="公开", variable=var_priv, value=0).pack(side="left")
        ttk.Radiobutton(box, text="私密", variable=var_priv, value=1).pack(side="left", padx=10)

        def do_create():
            title = var_name.get().strip()
            if not title:
                messagebox.showwarning("提示", "请填写收藏夹名称。", parent=win)
                return
            win.destroy()
            threading.Thread(target=self._create_folder_worker,
                             args=(title, var_priv.get()), daemon=True).start()

        btns = ttk.Frame(frm)
        btns.grid(row=2, column=0, columnspan=2, sticky="e", pady=(16, 0))
        ttk.Button(btns, text="取消", command=win.destroy, width=10).pack(side="right")
        ttk.Button(btns, text="创建", command=do_create, width=10).pack(side="right", padx=6)

    def _create_folder_worker(self, title, privacy):
        try:
            cookies = core.read_browser_cookies(DEFAULT_PORT)
            if cookies is None:
                self.q.put(("error", "连不上浏览器，请先点「启动浏览器登录」。"))
                return
            writer = core.BiliWriter(cookies)
            data = writer.create_folder(title, privacy)
            self.q.put(("info", f'收藏夹「{title}」创建成功（id={data.get("id")}）。'))
            self.q.put(("folders", writer.list_folders(writer.my_mid())))
        except Exception as e:  # noqa: BLE001
            self.q.put(("error", f"新建收藏夹失败：\n{e}"))

    # ------------------------------------------------------------ 阶段3：写入
    def start_push(self):
        todo = [it for it in self.items.values() if it["checked"] and it["aid"]]
        if not todo:
            messagebox.showwarning("提示", "没有勾选任何有效条目。\n\n"
                                           "只有勾选状态、而且有对应 BV 号的条目才会被写入。")
            return
        sel = self.var_folder.get()
        target = (self.folder_map or {}).get(sel)
        if target:
            desc = f'收藏夹「{target["title"]}」（现有 {target["media_count"]} 个内容）'
            privacy = None
        else:
            desc = f'新建收藏夹「{self.playlist_name}」'
            privacy = None
        ok = messagebox.askokcancel(
            "确认写入",
            f"即将把 {len(todo)} 个视频加入：\n\n{desc}\n\n"
            "写入过程中每首会间隔几秒，请不要关闭窗口。\n"
            "开始后可以随时点「停止」，已写入的不会重复。")
        if not ok:
            return
        if not target:
            ans = messagebox.askyesnocancel(
                "收藏夹可见性", "新建的收藏夹要设为公开吗？\n\n是 = 公开\n否 = 私密\n取消 = 不创建")
            if ans is None:
                return
            privacy = 0 if ans else 1
        self.stop_flag.clear()
        self._set_busy(True)
        self.q.put(("status", "开始写入…"))
        threading.Thread(target=self._push_worker, args=(target, privacy), daemon=True).start()

    def _push_worker(self, target, privacy):
        try:
            cookies = core.read_browser_cookies(DEFAULT_PORT)
            if cookies is None:
                raise RuntimeError(
                    "连不上浏览器。请先点「启动浏览器登录」，\n"
                    "在弹出的窗口里登录 B站，并保持窗口打开。")
            writer = core.BiliWriter(cookies)
            mid = writer.my_mid()
            if target:
                folder = writer.resolve_folder(mid, None, target["id"])
            else:
                folder = writer.create_folder(self.playlist_name or "网易云歌单", privacy)
                self.q.put(("log", f'已新建收藏夹「{folder.get("title")}」'))
            fid = folder["id"]
            self.q.put(("log", f'目标收藏夹：{folder.get("title")} (id={fid})'))

            # 每一轮开始先清掉上一轮的写入标记，让绿/灰/红只代表本轮结果
            for it in self.items.values():
                if it.get("wrote") or it.get("failed") or it.get("skipped"):
                    self.q.put(("row", (str(it["idx"]), {
                        "wrote": False, "failed": False, "skipped": False,
                        "note": it.get("match_note", ""),
                    })))

            # 读收藏夹里已有的内容，重复的直接跳过
            existing_aids, existing_bvids = set(), set()
            try:
                existing_aids, existing_bvids = writer.folder_media(fid)
                self.q.put(("log", f"收藏夹里已有 {len(existing_aids)} 个内容，"
                                   f"重复的会自动跳过。"))
            except Exception as e:  # noqa: BLE001
                self.q.put(("log", f"读取收藏夹已有内容失败，本轮不做去重：{e}"))

            todo, skipped = [], []
            # 按列表从上到下的实际显示顺序写入。
            # 列表是倒序的（歌单最后一首在最上面），所以写入顺序也必须跟着倒过来，
            # 否则收藏夹里的顺序会和显示顺序（以及网易云歌单顺序）相反。
            for iid in self.tree.get_children():
                it = self.items.get(iid)
                if not it or not (it["checked"] and it["aid"]):
                    continue
                if (str(it["aid"]) in existing_aids
                        or (it.get("bv") and it["bv"] in existing_bvids)):
                    skipped.append(it)
                else:
                    todo.append(it)

            for it in skipped:
                self.q.put(("row", (str(it["idx"]), {
                    "skipped": True, "wrote": False, "failed": False,
                    "skipped_reason": "已收藏跳过",
                    "note": "该视频已经在收藏夹里，本次跳过",
                })))
            if skipped:
                self.q.put(("log", f"{len(skipped)} 个视频已经在收藏夹里，已跳过。"))
            if not todo:
                self.q.put(("log", "没有需要新增的条目：勾选的视频都已经在收藏夹里了。"))
                self.q.put(("status", f"无需写入（{len(skipped)} 个已存在）"))
                return

            total = len(todo)
            self.q.put(("log", f"本轮实际需要写入 {total} 个，"
                               f'按列表从上到下的顺序（第一条：序号 {todo[0]["idx"]}）。'))
            ok = fail = consec = 0
            for n, it in enumerate(todo, 1):
                if self.stop_flag.is_set():
                    self.q.put(("log", "已被手动停止。"))
                    break
                iid = str(it["idx"])
                try:
                    resp = writer.add_video(it["aid"], fid)
                    code = resp.get("code")
                except Exception as e:  # noqa: BLE001
                    code, resp = "EXC", {"message": str(e)[:80]}

                if code == 0:
                    ok += 1
                    consec = 0
                    self.q.put(("row", (iid, {"wrote": True, "failed": False,
                                              "skipped": False,
                                              "note": "本轮已写入"})))
                    self.q.put(("progress", (n, total)))
                    self.q.put(("status", f"写入中 {n}/{total}｜新增 {ok}·跳过 {len(skipped)}·失败 {fail}"))
                else:
                    fail += 1
                    consec += 1
                    msg = resp.get("message")
                    self.q.put(("row", (iid, {"failed": True, "wrote": False,
                                              "skipped": False,
                                              "note": f"写入失败 {code} {msg}"})))
                    self.q.put(("status", f"写入中 {n}/{total}｜新增 {ok}·跳过 {len(skipped)}·失败 {fail}"))
                    if code in (-352, -412, -509):
                        self.q.put(("error",
                                    f"接口返回 {code}（风控/限流），已自动停车。\n"
                                    f"本次成功 {ok} 首，休息几小时后再跑，会从断点继续。"))
                        break
                    if consec >= 5:
                        self.q.put(("error",
                                    "连续 5 条写入失败，已停车。\n\n"
                                    "常见原因：收藏夹已满、视频已失效、或该视频已经在收藏夹里。\n"
                                    "可以点「换一个候选」换别的视频，或先清理收藏夹。"))
                        break
                if n % 5 == 0:
                    self.q.put(("log", f"写入中… 新增 {ok} / 跳过 {len(skipped)} "
                                       f"/ 失败 {fail} / 共 {total}"))
                if n < total:
                    time.sleep(4 + 4 * (n % 2))
                    if n % 50 == 0:
                        time.sleep(60)
            summary = (f"本轮结束：新增 {ok} 首，跳过 {len(skipped)} 首（已收藏），"
                       f"失败 {fail} 首。")
            self.q.put(("log", summary))
            self.q.put(("status", summary))
            self._save_report()
        except Exception as e:  # noqa: BLE001
            self.q.put(("error", f"写入失败：\n{e}"))
        finally:
            self.q.put(("busy", False))

    def stop_work(self):
        self.stop_flag.set()
        self.var_status.set("正在停止…")

    # ------------------------------------------------------------ 单条修正
    def _apply_candidate(self, iid, cand):
        it = self.items[iid]
        # 手动挑选也如实反映时长情况：差太多的仍然标灰，但依然算你选定了
        lvl = core.duration_level(cand.get("时长秒"), it["ne_sec"])
        ui_level = {"strict": "strict", "loose": "loose", "unknown": "strict"}.get(lvl, "none")
        self._update_row(iid, {
            "title": cand["标题"], "vsec": cand["时长秒"], "bv": cand["BV号"],
            "aid": cand["aid"], "up": cand["UP主"], "level": ui_level,
            "manual": True, "checked": True, "skipped": False,
            "note": "手动从候选中挑选", "match_note": "手动从候选中挑选",
            "play": cand.get("播放量"), "fav": cand.get("收藏量"),
            "发布时间": cand.get("发布时间"), "弹幕": cand.get("弹幕"),
            "official_mv": bool(cand.get("official_mv")),
            "whitelist_hit": bool(cand.get("whitelist_hit")),
            "via_translation": bool(cand.get("via_translation")),
            "multi": bool(cand.get("multi")),
        })

    def pick_candidate_dialog(self, iid=None):
        if iid is None:
            iid = self._selected_iid()
        if not iid:
            messagebox.showinfo("提示", "请先在列表里选中一行。")
            return

        it = self.items[iid]
        ranked = self.cands.get(iid)
        raw = self.raw_cands.get(iid) or []

        if ranked is None and not raw:
            # 真的没搜过
            messagebox.showinfo("提示", "这一条还没有候选缓存。\n"
                                        "请先点「开始匹配」，或右键选「重新搜索」。")
            return

        # 候选被过滤光了，就把搜索的原始结果端出来让你手动挑，而不是只回一句"没有"
        fallback = not ranked and bool(raw)
        cands = list(ranked) if ranked else list(raw)
        if not cands:
            messagebox.showinfo(
                "提示",
                f'「{it["song"]}」这一次没搜到任何结果。\n\n'
                "可以右键选「重新搜索」换个关键词，或右键「手动填写 BV 号」。")
            return

        win = tk.Toplevel(self.root)
        win.title(f'选择候选 - {it["song"]}')
        win.geometry("1000x500")
        win.transient(self.root)

        ttk.Label(win, text=f'原曲时长 {core.fmt_duration(it["ne_sec"])}'
                            f'　·　双击某行，或选中后点「使用这条」',
                  padding=(8, 8, 8, 2)).pack(anchor="w")
        cols = ("title", "dur", "ratio", "play", "fav", "up", "flag")
        tv = ttk.Treeview(win, columns=cols, show="headings")
        for key, text, width in (("title", "标题", 380), ("dur", "时长", 62),
                                 ("ratio", "占原曲", 62), ("play", "播放", 78),
                                 ("fav", "收藏", 78), ("up", "UP主", 130),
                                 ("flag", "标记", 150)):
            tv.heading(key, text=text)
            tv.column(key, width=width, anchor="w" if key in ("title", "up") else "center")

        var_note = tk.StringVar()
        ttk.Label(win, textvariable=var_note, foreground="#8A5A00",
                  padding=(8, 2, 8, 2), wraplength=970, justify="left").pack(anchor="w")

        mapping = {}
        tv.pack(fill="both", expand=True, padx=8)
        tv.tag_configure("reject", foreground="#9A9A9A")
        tv.tag_configure("multi", foreground="#B36B00")

        def render(items, is_raw):
            tv.delete(*tv.get_children())
            mapping.clear()
            for i, c in enumerate(items):
                key = str(i)
                mapping[key] = c
                ratio = f'{c["ratio"]:.0%}' if c.get("ratio") else "--"
                flag = []
                if c.get("official_mv"):
                    flag.append("官方MV")
                if c.get("whitelist_hit"):
                    flag.append("白名单")
                if c.get("multi"):
                    flag.append("合唱?")
                elif (c.get("artist_total") or 0) >= 2:
                    flag.append(f'歌手{c.get("artist_hits", 0)}/{c["artist_total"]}')
                if c.get("via_translation"):
                    flag.append("译名")
                if c.get("level") == "reject":
                    flag.append("时长不符")
                tv.insert("", "end", iid=key, values=(
                    c["标题"], core.fmt_duration(c["时长秒"]), ratio,
                    self._short_num(c.get("播放量")), self._short_num(c.get("收藏量")),
                    c["UP主"], "·".join(flag)))
                if c.get("level") == "reject":
                    tv.item(key, tags=("reject",))
                elif c.get("multi"):
                    tv.item(key, tags=("multi",))
            var_note.set(
                "⚠ 以下是搜索的原始结果，没有经过歌名/时长过滤，里面可能有完全不相干的视频。\n"
                "点了「使用这条」仍会被采纳，请自己确认是不是你要的那首。"
                if is_raw else "")

        render(cands, fallback)

        def use(_event=None):
            sel = tv.selection()
            if not sel:
                return
            cand = mapping.get(sel[0])
            if not cand:
                return
            self._apply_candidate(iid, cand)
            win.destroy()

        tv.bind("<Double-1>", use)
        tv.focus_set()
        bar = ttk.Frame(win, padding=8)
        bar.pack(fill="x")
        if ranked and raw:
            ttk.Button(bar, text=f"显示全部 {len(raw)} 条原始结果",
                       command=lambda: render(list(raw), True), width=22).pack(side="left")
        ttk.Button(bar, text="取消", command=win.destroy, width=10).pack(side="right")
        ttk.Button(bar, text="使用这条", command=lambda: use(), width=12).pack(
            side="right", padx=6)

    def research_dialog(self):
        iid = self._selected_iid()
        if not iid:
            messagebox.showinfo("提示", "请先在列表里选中一行。")
            return
        from tkinter import simpledialog
        it = self.items[iid]
        default = f'{it["song"]} {it["artists"].split("/")[0] if it["artists"] else ""}'.strip()
        kw = simpledialog.askstring("重新搜索", "输入新的搜索关键词：",
                                    initialvalue=default, parent=self.root)
        if not kw:
            return
        threading.Thread(target=self._research_worker, args=(iid, kw), daemon=True).start()

    def _research_worker(self, iid, keyword):
        try:
            it = self.items[iid]
            searcher = core.BiliSearch()
            raw = searcher.search(keyword)
            alt = list(it.get("trans") or []) if self.var_translate.get() else []
            ranked, _ = core.rank_candidates(raw, it["song"], it["artists"],
                                             it["ne_sec"], alt_names=alt,
                                             album=it.get("album", ""),
                                             want_fetch=bool(self.var_deep.get()))
            self.cands[iid] = ranked
            self.raw_cands[iid] = raw
            if ranked:
                self.q.put(("log", f"重新搜索完成：{len(ranked)} 条可用候选"
                                   f"（共拉到 {len(raw)} 条）。"))
            elif raw:
                self.q.put(("log", f"重新搜索拉到 {len(raw)} 条，但没有一条通过自动过滤"
                                   f"（标题不含歌名或时长不符），候选框里已按原始结果列出。"))
            else:
                self.q.put(("log", "重新搜索没有返回任何结果，换个关键词再试。"))
            self.root.after(200, lambda: self.pick_candidate_dialog(iid))
        except Exception as e:  # noqa: BLE001
            self.q.put(("error", f"重新搜索失败：\n{e}"))

    def manual_bv_dialog(self):
        iid = self._selected_iid()
        if not iid:
            messagebox.showinfo("提示", "请先在列表里选中一行。")
            return
        from tkinter import simpledialog
        bv = simpledialog.askstring("手动指定 BV 号", "粘贴 BV 号：",
                                    initialvalue=self.items[iid]["bv"], parent=self.root)
        if bv:
            self._apply_bv(iid, bv)

    def _inline_edit_bv(self, iid):
        x, y, w, h = self.tree.bbox(iid, "#6")
        if not w:
            return
        var = tk.StringVar(value=self.items[iid]["bv"])
        entry = ttk.Entry(self.tree, textvariable=var)
        entry.place(x=x, y=y, width=w, height=h)
        entry.focus_set()
        entry.select_range(0, "end")

        def commit(_event=None):
            value = var.get().strip()
            entry.destroy()
            if value and value != self.items[iid]["bv"]:
                self._apply_bv(iid, value)

        entry.bind("<Return>", commit)
        entry.bind("<FocusOut>", commit)
        entry.bind("<Escape>", lambda e: entry.destroy())

    def _apply_bv(self, iid, bv):
        bv = bv.strip()
        if not core.BV_RE.match(bv):
            messagebox.showwarning("格式不对",
                                   "BV 号应该是 BV + 10 位字母数字，例如 BV1bs4y1u7rk。")
            return
        self.var_status.set(f"正在查询 {bv} …")
        threading.Thread(target=self._bv_worker, args=(iid, bv), daemon=True).start()

    def _bv_worker(self, iid, bv):
        try:
            info = core.bvid_to_info(bv)
            note = "手动指定"
            if (info.get("分P数") or 1) > 1:
                note = f'手动指定（多P视频，共 {info["分P数"]} P，收藏的是整个稿件）'
            self.q.put(("row", (iid, {
                "bv": bv, "title": info["标题"], "vsec": info["时长秒"],
                "aid": info["aid"], "up": info["UP主"], "level": "strict",
                "manual": True, "checked": True, "skipped": False,
                "note": note, "match_note": note,
            })))
            self.q.put(("status", "已更新"))
        except Exception as e:  # noqa: BLE001
            self.q.put(("error", f"查询 BV 号失败：\n{e}"))

    # ------------------------------------------------------------ 收尾
    def ask_exit_options(self):
        """退出前询问是否清除登录状态。

        返回 True=清除、False=只清缓存、None=取消退出。
        勾选框**默认勾选**（清除），符合"尽量不留数据"的默认预期。
        """
        self._exit_choice = None
        win = tk.Toplevel(self.root)
        win.title("退出程序")
        win.transient(self.root)
        win.grab_set()
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", lambda: self._cancel_exit(win))

        bar = ttk.Frame(win, padding=10)
        bar.pack(side="bottom", fill="x")
        body = ttk.Frame(win, padding=(16, 14, 16, 8))
        body.pack(side="top", fill="both", expand=True)

        var_clear = tk.BooleanVar(value=True)      # 默认勾选
        var_hint = tk.StringVar()

        def refresh_hint(*_a):
            var_hint.set(
                "会先关掉那个 Chrome 窗口，再把 bili_profile 整个目录删掉。\n"
                "下次使用需要重新点「启动 Chrome 登录」并扫码。"
                if var_clear.get() else
                "只清理浏览器缓存，保留登录状态，下次不用重新扫码。")

        ttk.Checkbutton(body, text="清除浏览器缓存和登录状态", variable=var_clear,
                        command=refresh_hint).pack(anchor="w")
        ttk.Label(body, textvariable=var_hint, foreground="#666666",
                  justify="left", wraplength=420).pack(anchor="w", padx=(22, 0), pady=(2, 12))
        refresh_hint()

        ttk.Label(body, justify="left", wraplength=470, text=(
            "提示：反复重新扫码登录有触发 B站 登录风控的可能。\n"
            "如果这台电脑只有你自己用，不勾选会更省事。"
        )).pack(anchor="w")

        def ok(_e=None):
            self._exit_choice = bool(var_clear.get())
            win.destroy()

        ttk.Button(bar, text="确定退出", command=ok, width=12).pack(side="right")
        ttk.Button(bar, text="取消", command=lambda: self._cancel_exit(win),
                   width=8).pack(side="right", padx=6)
        win.bind("<Return>", ok)
        win.bind("<Escape>", lambda _e: self._cancel_exit(win))

        win.update_idletasks()
        w = max(win.winfo_reqwidth(), 520)
        h = win.winfo_reqheight()
        x = self.root.winfo_rootx() + max((self.root.winfo_width() - w) // 2, 0)
        y = self.root.winfo_rooty() + max((self.root.winfo_height() - h) // 2, 0)
        win.geometry(f"{w}x{h}+{x}+{y}")
        win.wait_window()
        return self._exit_choice

    def _cancel_exit(self, win):
        self._exit_choice = None
        win.destroy()

    def on_close(self):
        if self.busy:
            if not messagebox.askokcancel("确认退出", "任务还在进行中，确定要中断并退出吗？"):
                return
            self.stop_flag.set()
        choice = self.ask_exit_options()
        if choice is None:            # 用户取消退出
            return
        self._save_config()
        try:
            if choice:
                # 想彻底删干净，得先让那个 Chrome 自己退出（文件被占用是删不掉的）
                self.var_status.set("正在关闭浏览器并清除数据…")
                self.root.update_idletasks()
                core.close_debug_browser(DEFAULT_PORT)
                clean, left = core.wipe_browser_profile(self.workdir)
                if clean:
                    self.var_status.set("已清除浏览器缓存和登录状态")
                else:
                    self.var_status.set(
                        f"有 {left / 1024 / 1024:.1f} MB 没删掉，"
                        f"浏览器可能还在运行，请手动关闭后再删 bili_profile")
            else:
                freed, _locked = core.cleanup_browser_caches(self.workdir)
                if freed:
                    self.var_status.set(
                        f"退出前清理浏览器缓存：释放 {freed / 1024 / 1024:.1f} MB")
            self.root.update_idletasks()
        except Exception:  # noqa: BLE001 - 清理失败不该挡住退出
            pass
        self.root.destroy()


def main():
    set_dpi_awareness()
    hide_console()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
