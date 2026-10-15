#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网易云音乐歌单 -> B站收藏夹 迁移工具（初版）

设计原则：
  阶段1 导出  纯网络，零依赖（只用 Python 标准库）
  阶段2 匹配  纯网络，零依赖，不需要登录
  阶段3 写入  需要 Chrome 处于 B站登录状态（专用 profile，程序不处理密码）

常用用法：
  python bili_sync.py --playlist "<歌单分享链接>"              # 只跑阶段1+2，生成对照表
  python bili_sync.py --launch-browser                        # 启动专用 Chrome，第一次扫码登录
  python bili_sync.py --push --folder "我的歌单"              # 跑阶段3，写入收藏夹
"""

import argparse
import base64
import csv
import difflib
import gzip
import html
import json
import os
import random
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------- 基础设置

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")

DEFAULT_WORKDIR = "bili_migrate"

# 网易云接口：这个 Cookie 是硬要求，不带它只能拿到前 6 首预览
NETEASE_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://music.163.com/",
    "Cookie": "os=pc;",
}

# 标题黑名单：命中即剔除（原方案 4.2，并按实测补上了漏网词）
# 用户可在界面里编辑，保存到工作目录的 blacklist.txt
DEFAULT_BLACKLIST_TERMS = [
    "翻唱", "翻弹", "演奏", "钢琴", "小提琴", "大提琴", "清唱", "改编",
    "纯音乐", "伴奏", "合集", "合辑", "串烧", "循环", "一小时", "1小时",
    "鬼畜", "remix", "慢速", "加速", "民乐", "吉他", "尤克里里", "口琴",
    "教学", "简谱", "动态谱", "铃声", "纯享", "cover", "双声道", "左右声道",
    "8d", "环绕", "笛子", "二胡", "古筝",
    "现场", "演唱会", "live", "歌单",
    "歌ってみた", "试着唱了", "試著唱了", "翻調", "翻调",
]
BLACKLIST_TERMS = list(DEFAULT_BLACKLIST_TERMS)   # 当前生效的词表
BLACKLIST_FILE = "blacklist.txt"

# 白名单：命中的候选优先选择（用户可在界面里编辑）
DEFAULT_WHITELIST_TERMS = ["无损"]
WHITELIST_TERMS = list(DEFAULT_WHITELIST_TERMS)
WHITELIST_FILE = "whitelist.txt"

# 合唱/合作标记：原曲是单人歌手时，命中这些词说明是别人的合唱版本
DUET_WORDS = re.compile(
    r"合唱|对唱|重唱|二人|双人|男女|男声版|女声版|feat\.?|ft\.?|with\s|合作版",
    re.IGNORECASE,
)
# 紧跟歌手名之后的连接符，用于识别 "AAA & BBB" 这种双歌手写法
ARTIST_JOIN = re.compile(r"^\s*[&＆×✕、,，+＋]\s*[^\s\-–—|｜/／【】\[\]()（）《》]{1,20}")

# 网易云自带的译名（tns / transNames）里混着"TV动画《xx》片尾曲"这类说明文字，
# 那不是歌名，拿去做搜索或标题比只会产生噪声，必须过滤掉。
DESC_MARK = re.compile(
    r"《|》|「|」|主题曲|插曲|片头|片尾|收录|片尾曲|片头曲|OP\d|ED\d|动画|游戏|电视剧|电影|专辑")


def pick_translations(names):
    """从网易云返回的译名列表里挑出能当歌名用的。"""
    out = []
    for n in names or []:
        n = (n or "").strip()
        if not n or len(n) > 24 or DESC_MARK.search(n):
            continue
        if n not in out:
            out.append(n)
    return out


EXPECTED_COLUMNS = ["序号", "歌名", "歌手", "专辑", "时长秒", "时长",
                    "网易云ID", "无版权标记", "译名"]
# 歌单列表改成逆序展示：歌单里最后一首排在最前面。
# 序号保留网易云歌单里的原始位置，方便对照。
ORDER_TAG = "逆序"


def set_blacklist(terms):
    """覆盖当前生效的黑名单词表。"""
    global BLACKLIST_TERMS
    BLACKLIST_TERMS = _clean_terms(terms)
    return BLACKLIST_TERMS


def set_whitelist(terms):
    """覆盖当前生效的白名单词表。"""
    global WHITELIST_TERMS
    WHITELIST_TERMS = _clean_terms(terms)
    return WHITELIST_TERMS


def _clean_terms(terms):
    clean = []
    for t in terms:
        t = (t or "").strip()
        if t and t not in clean:
            clean.append(t)
    return clean


def _build_term_re(terms):
    """把词表编译成正则。纯英文数字的词加词边界，所有词都转义。"""
    parts = []
    for t in terms:
        t = (t or "").strip()
        if not t:
            continue
        if re.fullmatch(r"[A-Za-z0-9]+", t):
            parts.append(r"\b" + re.escape(t) + r"\b")
        else:
            parts.append(re.escape(t))
    return re.compile("|".join(parts), re.IGNORECASE) if parts else None


def blacklist_path(workdir):
    return Path(workdir) / BLACKLIST_FILE


def whitelist_path(workdir):
    return Path(workdir) / WHITELIST_FILE


def _load_terms(workdir, filename, setter, fallback):
    path = Path(workdir) / filename
    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return list(fallback)
        terms = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
        if terms:
            return setter(terms)
    return list(fallback)


def _save_terms(workdir, filename, setter, terms):
    clean = setter(terms)
    try:
        (Path(workdir) / filename).write_text("\n".join(clean) + "\n", encoding="utf-8")
    except OSError:
        pass
    return clean


def load_blacklist(workdir):
    """从工作目录读取用户自定义黑名单；没有就返回默认词表。"""
    return _load_terms(workdir, BLACKLIST_FILE, set_blacklist, BLACKLIST_TERMS)


def save_blacklist(workdir, terms):
    return _save_terms(workdir, BLACKLIST_FILE, set_blacklist, terms)


def load_whitelist(workdir):
    """从工作目录读取用户自定义白名单；没有就返回默认（只有「无损」）。"""
    return _load_terms(workdir, WHITELIST_FILE, set_whitelist, WHITELIST_TERMS)


def save_whitelist(workdir, terms):
    return _save_terms(workdir, WHITELIST_FILE, set_whitelist, terms)


def whitelist_re():
    return _build_term_re(WHITELIST_TERMS)


def blacklist_for(song, alt_names=None):
    """歌名自己就含「翻唱/Cover」这类词时，不能拿这些词去挡自己。

    纯英文数字的词加词边界，避免 live 误伤 olive / deliver 这类单词；
    所有词都做正则转义，防止用户输入 ( [ 等字符把正则写坏。
    """
    ns = norm(song) + "".join(norm(a) for a in (alt_names or []))
    terms = [t for t in BLACKLIST_TERMS if norm(t) not in ns]
    return _build_term_re(terms)


def has_extra_artist(title, artists):
    """标题是否像"多人合作/合唱"版本。

    只在原曲歌手是单人的时候调用——原曲本身就是合唱时，标题出现合唱字样很正常。
    实测到的问题：原曲歌手只有一位，搜索出来的却是两人合唱版，标题和歌名还完全一样。
    """
    solo = [a.strip() for a in re.split(r"[/、,，&]", artists or "") if a.strip()]
    if len(solo) != 1:
        return False   # 原曲本来就是合作曲目，不做判断，避免误伤
    if DUET_WORDS.search(title):
        return True
    name = solo[0]
    idx = title.find(name)
    if idx < 0:
        return False
    return bool(ARTIST_JOIN.match(title[idx + len(name):]))

# 白名单加分（原方案 4.3）
WHITELIST = re.compile(
    r"官方|原唱|MV|唱片|专辑|索尼|华纳|环球|4K|无损|Hi-?res|完整版",
    re.IGNORECASE,
)

PUNCT = re.compile(r"[\s\-_/|｜【】\[\]()（）《》<>·.,、~～!！?？:：;；'\"“”‘’*＊#＃+＋@＆&%％]")
BRACKET = re.compile(r"[（(\[【][^）)\]】]*[）)\]】]")

# ---------------------------------------------------------------- 排序规则
# 用户可以自己增删、上下移动这些规则的优先级（界面上「排序…」按钮）。
SORT_RULE_LABELS = {
    "official_mv": "官方MV",
    "artist": "歌手匹配度",
    "whitelist": "白名单命中",
    "play": "最多播放",
    "strict_dur": "严格标准时长",
    "pubdate": "最新发布",
    "danmaku": "最多弹幕",
    "favorites": "最多收藏",
}
DEFAULT_SORT_ORDER = ["official_mv", "artist", "whitelist", "play", "strict_dur"]
DEFAULT_SORT_DISABLED = ["pubdate", "danmaku", "favorites"]
SORT_RULE_FILE = "sort_rules.json"

ACTIVE_SORT_ORDER = list(DEFAULT_SORT_ORDER)
ACTIVE_SORT_DISABLED = list(DEFAULT_SORT_DISABLED)
FRESH_WINDOW_DAYS = 30        # 「最新发布」的判定窗口（换用 sort_value 时用不到，留作说明）


def _dedupe(seq, allowed):
    out = []
    for r in seq or []:
        if r in allowed and r not in out:
            out.append(r)
    return out


def set_sort_rules(enabled, disabled=None):
    """设定启用/未启用的排序规则。"""
    global ACTIVE_SORT_ORDER, ACTIVE_SORT_DISABLED
    ACTIVE_SORT_ORDER = _dedupe(enabled, SORT_RULE_LABELS)
    if disabled is None:
        disabled = [r for r in SORT_RULE_LABELS if r not in ACTIVE_SORT_ORDER]
    ACTIVE_SORT_DISABLED = _dedupe(disabled, SORT_RULE_LABELS)
    return ACTIVE_SORT_ORDER, ACTIVE_SORT_DISABLED


def sort_rule_path(workdir):
    return Path(workdir) / SORT_RULE_FILE


def load_sort_rules(workdir):
    path = sort_rule_path(workdir)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            enabled = data.get("enabled") if isinstance(data, dict) else data
            if enabled:
                return set_sort_rules(enabled,
                                      (data or {}).get("disabled") if isinstance(data, dict) else None)
        except (OSError, ValueError):
            pass
    return list(ACTIVE_SORT_ORDER), list(ACTIVE_SORT_DISABLED)


def save_sort_rules(workdir, enabled, disabled=None):
    en, dis = set_sort_rules(enabled, disabled)
    try:
        sort_rule_path(workdir).write_text(
            json.dumps({"enabled": en, "disabled": dis}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError:
        pass
    return en, dis


def sort_value(rule, item):
    """把一条规则折算成"越小越优先"的可比较值。"""
    if rule == "official_mv":
        return 0 if item.get("official_mv") else 1
    if rule == "artist":
        return item.get("artist_rank", 99)
    if rule == "whitelist":
        return 0 if item.get("whitelist_hit") else 1
    if rule == "play":
        return -(item.get("播放量") or 0)
    if rule == "strict_dur":
        return (0 if item.get("level") == "strict" else 1, item.get("diff", 99))
    if rule == "pubdate":
        return -(item.get("发布时间") or 0)
    if rule == "danmaku":
        return -(item.get("弹幕") or 0)
    if rule == "favorites":
        return -(item.get("收藏量") or 0)
    return 0


def make_rank_key(order=None):
    """按配置顺序生成排序 key。

    「官方MV」排在第一位时有个特例：一旦存在官方 MV，就由它直接定选，
    多个官方 MV 之间比较播放量（用户指定），不再看后面的规则。

    另外：时长不合格（reject）的候选**永远排在最后**。它们只是给你手动挑的参考，
    不该挤进前面的位置、把能用的候选挤出候选框。
    """
    order = list(order or ACTIVE_SORT_ORDER or DEFAULT_SORT_ORDER)

    def key(x):
        vals = [0 if x.get("level") != "reject" else 1]
        for i, rule in enumerate(order):
            if rule == "official_mv":
                vals.append(0 if x.get("official_mv") else 1)
                if i == 0:
                    vals.append(-(x.get("播放量") or 0) if x.get("official_mv") else 0)
            else:
                vals.append(sort_value(rule, x))
        return tuple(vals)

    return key


# 时长规则（用户 2026-10-12 修改）：
#     严格：原曲 -3 秒 ~ 原曲 +3 秒
#     宽松：原曲的 80% ~ 120%
#     其余：不符合，不能作为默认选择
# 3 秒余量是因为网易云的 dt 含音频编码填充，通常比实际音频多算 1~2 秒。
STRICT_TOLERANCE = 3
LOOSE_MIN_RATIO = 0.80
LOOSE_MAX_RATIO = 1.20


def log(msg, logfile=None):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if logfile:
        try:
            with open(logfile, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def http_request(url, headers=None, data=None, method=None, timeout=25, retries=3, backoff=1.5):
    """带重试的 HTTP 请求，返回 bytes。"""
    req_headers = {"User-Agent": UA}
    if headers:
        req_headers.update(headers)
    body = data.encode("utf-8") if isinstance(data, str) else data
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (400, 403, 404, 412):
                # 412 是风控，交给调用方判断，不在这里无脑重试
                raise
            time.sleep(backoff * (attempt + 1))
        except Exception as e:  # noqa: BLE001 网络层统一重试
            last_err = e
            time.sleep(backoff * (attempt + 1))
    raise RuntimeError(f"请求失败: {url} -> {last_err}")


def http_json(url, headers=None, data=None, method=None, timeout=25, retries=3):
    raw = http_request(url, headers=headers, data=data, method=method,
                       timeout=timeout, retries=retries)
    return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------- 文本归一化

def norm(text):
    """去空格、标点、大小写，用于标题比对。"""
    return PUNCT.sub("", html.unescape(text or "").lower())


def core_name(name):
    """剥掉括号内容，例如「边错边爱（烟嗓版）」->「边错边爱」。"""
    return norm(BRACKET.sub("", name or ""))


def clean_title(title):
    """去掉 B站搜索结果里的 <em> 高亮标签和 HTML 实体。"""
    return html.unescape(re.sub(r"<[^>]+>", "", title or "")).strip()


def parse_duration(text):
    """B站时长 '6:6' / '1:02:33' -> 秒；解析不了返回 None。"""
    if text is None:
        return None
    parts = str(text).strip().split(":")
    if not parts or any(not p.isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return nums[0] if nums else None


def fmt_duration(sec):
    if sec is None or sec == "":
        return ""
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return ""
    if sec < 3600:
        return f"{sec // 60}:{sec % 60:02d}"
    return f"{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


# ---------------------------------------------------------------- 阶段1：网易云导出

def extract_playlist_id(text):
    """从各种形态的分享内容里抽歌单 id。"""
    if not text:
        return None
    text = text.strip()
    if text.isdigit():
        return int(text)
    m = re.search(r"[?&#/]id=(\d+)", text)
    if m:
        return int(m.group(1))
    m = re.search(r"playlist[/=](\d+)", text)
    if m:
        return int(m.group(1))
    return None


def resolve_short_link(text):
    """处理 163cn.tv 之类的短链：跟随跳转后重新抽取 id。"""
    m = re.search(r"https?://[^\s\"'<>]+", text or "")
    if not m:
        return None
    url = m.group(0).rstrip("。，,.)")
    try:
        raw = http_request(url, headers=NETEASE_HEADERS, retries=2)
    except Exception:  # noqa: BLE001
        return None
    return extract_playlist_id(raw.decode("utf-8", "ignore"))


def fetch_playlist(playlist_id):
    """拉取歌单，返回 (歌单名, playlist对象, 曲目列表)。"""
    url = f"https://music.163.com/api/v6/playlist/detail?id={playlist_id}&n=1000&s=0"
    data = http_json(url, headers=NETEASE_HEADERS)
    if data.get("code") != 200:
        raise RuntimeError(f"网易云返回 code={data.get('code')} msg={data.get('msg')}")

    pl = data["playlist"]
    name = pl.get("name") or f"playlist_{playlist_id}"
    tracks = pl.get("tracks") or []
    total = pl.get("trackCount", len(tracks))

    # 超过 1000 首时用 trackIds 分批补齐（实测批量接口每批不能超过 200）
    if total > len(tracks):
        got = {t["id"] for t in tracks}
        missing = [t["id"] for t in pl.get("trackIds", []) if t["id"] not in got]
        extra = []
        for i in range(0, len(missing), 200):
            batch = missing[i:i + 200]
            payload = urllib.parse.quote(json.dumps(batch, separators=(",", ":")))
            sub = http_json(f"https://music.163.com/api/song/detail/?ids={payload}",
                            headers=NETEASE_HEADERS)
            extra.extend(sub.get("songs") or [])
            time.sleep(0.5)
        tracks = tracks + extra

    return name, pl, tracks


def do_export(playlist_ref, workdir, logfile, refresh=False):
    csv_path = workdir / "playlist.csv"
    meta_path = workdir / "playlist.json"

    if csv_path.exists() and meta_path.exists() and not refresh:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        with open(csv_path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames or []
            if all(col in header for col in EXPECTED_COLUMNS) and meta.get("顺序") == ORDER_TAG:
                rows = list(reader)
                log(f"已存在 {csv_path.name}，跳过导出（加 --refresh 可强制重导）", logfile)
                return rows, meta
        log("歌单文件是旧格式或旧顺序，自动重新导出…", logfile)

    pid = extract_playlist_id(playlist_ref)
    if pid is None:
        log("链接里没找到歌单 id，尝试按短链跟随跳转…", logfile)
        pid = resolve_short_link(playlist_ref)
    if pid is None:
        raise RuntimeError("无法从输入中解析出歌单 id，请直接粘贴分享链接或纯数字 id")

    log(f"正在拉取网易云歌单 id={pid} …", logfile)
    name, pl, tracks = fetch_playlist(pid)
    if len(tracks) <= 6 and pl.get("trackCount", 0) > 6:
        raise RuntimeError("只拿到 6 首预览，说明请求头里的 os=pc Cookie 没生效")

    rows = []
    for i, t in enumerate(tracks, 1):
        artists = "/".join(a.get("name", "") for a in (t.get("ar") or []) if a.get("name"))
        album = (t.get("al") or {}).get("name", "")
        sec = round((t.get("dt") or 0) / 1000)
        # 网易云自带的官方中文译名。
        # 不限语种：日文歌、英文歌只要网易云给了中文译名都会用上
        # （很多外语歌的 B站 官方 MV 只用中文名）
        trans = pick_translations(list(t.get("tns") or [])
                                  + list((t.get("al") or {}).get("tns") or []))
        rows.append({
            "序号": i,
            "歌名": t.get("name", ""),
            "歌手": artists,
            "专辑": album,
            "时长秒": sec,
            "时长": fmt_duration(sec),
            "网易云ID": t.get("id", ""),
            "无版权标记": "是" if t.get("noCopyrightRcmd") else "",
            "译名": "|".join(trans),
        })

    # 逆序：歌单里最后一首放到列表最前面（序号仍保留原始位置）
    rows.reverse()

    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    meta = {"歌单id": pid, "歌单名": name, "顺序": ORDER_TAG}
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"导出完成：{name}，共 {len(rows)} 首 -> {csv_path.name}", logfile)
    return rows, meta


# ---------------------------------------------------------------- 阶段2：B站搜索与匹配

class BiliSearch:
    """B站搜索接口封装。不需要登录，只要首页发的 buvid3。"""

    def __init__(self, logfile=None):
        self.logfile = logfile
        self.cookie = ""
        self._bootstrap()

    def _bootstrap(self):
        req = urllib.request.Request("https://www.bilibili.com/",
                                     headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                raw = resp.headers.get_all("Set-Cookie") or []
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"无法访问 bilibili.com：{e}") from e
        jar = {}
        for item in raw:
            kv = item.split(";")[0]
            if "=" in kv:
                k, v = kv.split("=", 1)
                jar[k.strip()] = v
        self.cookie = "; ".join(f"{k}={v}" for k, v in jar.items())
        if "buvid3" not in jar:
            log("警告：没能拿到 buvid3，搜索接口可能被风控（412）", self.logfile)

    def search(self, keyword, order="click"):
        url = ("https://api.bilibili.com/x/web-interface/search/type"
               f"?search_type=video&keyword={urllib.parse.quote(keyword)}"
               f"&order={order}&page=1")
        headers = {"Referer": "https://search.bilibili.com/", "Cookie": self.cookie}
        try:
            data = http_json(url, headers=headers)
        except urllib.error.HTTPError as e:
            if e.code == 412:
                raise RuntimeError("搜索被风控拦截（412），请降低频率后重试") from e
            raise
        if data.get("code") != 0:
            raise RuntimeError(f"搜索接口返回 code={data.get('code')} msg={data.get('message')}")
        out = []
        for item in data.get("data", {}).get("result") or []:
            if item.get("type") != "video":
                continue
            out.append({
                "标题": clean_title(item.get("title")),
                "UP主": item.get("author"),
                "播放量": item.get("play"),
                "收藏量": item.get("favorites"),
                "发布时间": item.get("pubdate"),
                "弹幕": item.get("danmaku"),
                "时长秒": parse_duration(item.get("duration")),
                "BV号": item.get("bvid"),
                "aid": item.get("aid"),
            })
        return out


def duration_level(vsec, ne_sec):
    """给一条候选打时长等级：strict / loose / reject / unknown。

    严格：原曲 ±3 秒
    宽松：原曲的 80% ~ 120%
    """
    if not ne_sec or not vsec:
        return "unknown"
    if abs(vsec - ne_sec) <= STRICT_TOLERANCE:
        return "strict"
    if ne_sec * LOOSE_MIN_RATIO <= vsec <= ne_sec * LOOSE_MAX_RATIO:
        return "loose"
    return "reject"


def _song_name_variants(song, alt_names=None):
    """歌名的归一化写法（原名 + 中文译名），短于 2 字的按原样保留。"""
    out = []
    for nm in [song] + list(alt_names or []):
        v = core_name(nm)
        if len(v) < 2:
            v = norm(nm)
        if v and v not in out:
            out.append(v)
    return out


def title_points_to_song(ntitle, song, alt_names=None, artist_keys=None, album=""):
    """标题是否指向这首歌。

    实测踩过的坑：「光」这种 1~2 字的短歌名，用子串包含会命中「光辉岁月」，
    所以短歌名要求标题里**同时**出现歌手名才算数。
    """
    keys = artist_keys or []
    for v in _song_name_variants(song, alt_names):
        if len(v) >= 3:
            if v in ntitle:
                return True
        elif v in ntitle and any(k in ntitle for k in keys):
            return True
    al = norm(album)
    if al and len(al) >= 2 and al in ntitle:
        return True
    if any(k in ntitle for k in keys):
        return True
    # 模糊：走到这里说明歌手已被确认匹配，歌名允许小部分不同
    return any(fuzzy_contains(ntitle, v) for v in _song_name_variants(song, alt_names))


def is_official_mv(cand, song, artists, alt_names=None, album="",
                   want_fetch=True):
    """官方 MV（用户 2026-10-12 的重新定义，已取消时长判定）。

    名称匹配：标题含 歌名/译名 或 歌手名 或 专辑名
    身份匹配：发布者名 或 发布者个人签名 含歌手名
    两者都成立即算官方 MV。

    歌名允许"小部分不同"（模糊匹配），但**只在歌手或专辑已确认匹配上之后**才启用——
    这一条正好由"身份匹配"卡住，所以模糊判定天然是安全的。
    """
    keys = [norm(a) for a in re.split(r"[/、,，&]", artists or "") if norm(a)]
    if not keys:
        return False
    title = cand.get("标题") or ""
    ntitle = norm(title)
    up_name = norm(cand.get("UP主") or "")

    name_ok = up_name_matches(up_name, keys)
    d = {}
    sign_ok = False
    if not name_ok and want_fetch:
        d = video_detail(cand.get("BV号"))
        mid = d.get("mid") or cand.get("mid")
        sign = up_signature(mid)
        nsign = norm(sign)
        if nsign and not FAN_MARK.search(nsign):
            # 短歌手名在长签名里太容易蹭到，要求签名本身也短一点
            sign_ok = any(k in nsign and (len(k) >= 3 or len(nsign) <= 40)
                          for k in keys)
    if not (name_ok or sign_ok):
        return False

    return title_points_to_song(ntitle, song, alt_names, keys, album)


def search_candidates(searcher, song, artists, alt_names=None, pace=0.5):
    """多关键词搜索 + 合并去重，返回候选列表。

    为什么不能只用「歌名 + 歌手」搜一次：实测把歌手名拼进去之后，
    B站 反而会把一些更合适的视频挤出前 20 名——英文歌手名尤其明显。
    对方视频标题里往往只有歌名的中文译名，没有歌手名。

    例：`Soviet March`（歌名）能搜到播放 237 万的 Hi-Res 版本，
    但 `Soviet March James Hannigan`（歌名+歌手）的前 20 条里**一条都没有**。

    所以这里「歌名 + 歌手」和「只用歌名」两次都搜、合并去重，
    交给排序规则去决定用哪条。
    """
    first = (artists or "").split("/")[0].strip()
    keywords = [f"{song} {first}".strip()]
    if first:
        keywords.append(song)
    for a in (alt_names or [])[:2]:
        keywords.append(f"{a} {first}".strip() if first else a)

    merged, seen = [], set()
    for i, kw in enumerate(keywords):
        if not kw:
            continue
        try:
            items = searcher.search(kw)
        except Exception:  # noqa: BLE001 - 某个关键词失败不该整条断掉
            items = []
        for c in items:
            key = c.get("BV号")
            if key and key not in seen:
                seen.add(key)
                merged.append(c)
        if i < len(keywords) - 1:
            time.sleep(pace)
    return merged


def rank_candidates(candidates, song, artists, ne_sec, top=20, alt_names=None,
                    album="", want_fetch=True):
    """过滤 + 打分 + 排序，返回 (ranked, stats)。

    硬约束（相对最初方案最关键的改动）：标题里必须出现歌名，否则直接淘汰。
    实测单纯靠时长 + 黑名单 + 播放量，会把完全无关的歌配进来。
    alt_names 是官方中文译名之类的等价歌名——B站 上的官方 MV 常常改用中文译名
    （日文歌、英文歌都有），只认原名会把它们全部漏掉。
    ranked 里每一项是候选 dict，额外带 level / score / ratio 字段，供界面挑选。
    """
    stats = {"blocked": 0, "no_name": 0, "total": len(candidates or [])}
    if not candidates:
        return [], stats

    artist_keys = [norm(a) for a in re.split(r"[/、,，&]", artists or "") if norm(a)]
    n_orig = len(artist_keys)      # 网易云歌单里这首歌的歌手人数

    variants = []                  # [(归一化歌名, 是否为译名)]
    for nm, is_alias in [(song, False)] + [(a, True) for a in (alt_names or [])]:
        core_nm = core_name(nm)
        val = core_nm if len(core_nm) >= 2 else norm(nm)
        if val and all(val != v for v, _ in variants):
            variants.append((val, is_alias))

    scored = []
    bl = blacklist_for(song, alt_names)
    wl = whitelist_re()
    for c in candidates:
        title = c["标题"]
        ntitle = norm(title)
        if bl and bl.search(title):
            stats["blocked"] += 1
            continue
        matched, via_alias = "", False
        for val, is_alias in variants:
            if val in ntitle:
                matched, via_alias = val, is_alias
                break
        if not matched:
            stats["no_name"] += 1
            continue
        hits = [a for a in artist_keys if a and a in ntitle]
        nhit = len(hits)
        artist_hit = nhit > 0
        # 歌名很短（如「光」）时必须同时命中歌手，否则太容易误配
        if len(matched) <= 2 and not artist_hit:
            stats["no_name"] += 1
            continue
        total_sec = c.get("时长秒")
        eff_sec = total_sec
        part_name = ""
        level = duration_level(total_sec, ne_sec)
        # 多P识别：总时长大幅超出原曲时，去看分P，挑一个最贴近原曲的
        # （分P肯定比总时长短，所以只有"超长"才有救，明显偏短的不必查）
        if (level == "reject" and want_fetch and ne_sec and total_sec
                and total_sec > ne_sec * LOOSE_MAX_RATIO and c.get("BV号")):
            part = best_part_duration(video_detail(c["BV号"]), ne_sec)
            if part:
                psec, pname = part
                lv2 = duration_level(psec, ne_sec)
                if lv2 in ("strict", "loose"):
                    level, eff_sec, part_name = lv2, psec, pname
        diff = abs(eff_sec - ne_sec) if (ne_sec and eff_sec) else 99
        item = dict(c)
        item["level"] = level
        item["有效时长"] = eff_sec
        item["分P"] = part_name
        item["artist_hit"] = artist_hit
        item["artist_hits"] = nhit
        item["artist_total"] = n_orig
        # 原曲是多人合作时不做「合唱」判定，只比对是否命中原曲歌手
        item["multi"] = has_extra_artist(title, artists) if n_orig <= 1 else False
        # 歌手维度的排序权重（越小越优先），一律以网易云歌单的歌手信息为准：
        #   原曲单人 -> 优先标题里没有第二位歌手的版本
        #   原曲多人 -> 只有「标题命中全部原曲歌手」才享受优先
        #
        # 为什么不是"命中越多越优先"：实测一条只提到一位歌手的低质投稿
        # （Soviet March 那条 1105 播放的）会压过播放量 237 万、时长更准的正确版本。
        # 原来的意图（完整版优于单人版）由"全部命中"这一档保住了。
        if n_orig <= 1:
            artist_rank = (4 if item["multi"] else 0) + (0 if artist_hit else 2)
        else:
            artist_rank = 0 if nhit >= n_orig else 1
        item["artist_rank"] = artist_rank
        item["via_translation"] = via_alias
        # 只对"有可能被选中"的候选做官方 MV 深查，避免无谓的网络请求
        item["official_mv"] = (False if level == "reject" else
                               is_official_mv(c, song, artists, alt_names=alt_names,
                                              album=album, want_fetch=want_fetch))
        item["whitelist_hit"] = bool(wl and wl.search(title))
        item["diff"] = diff
        item["ratio"] = (eff_sec / ne_sec) if (ne_sec and eff_sec) else None
        scored.append(item)

    # 排序：完全按当前配置的规则顺序（界面「排序…」里可改）
    scored.sort(key=make_rank_key())
    return scored[:top], stats


LEVEL_CN = {"strict": "严格", "loose": "宽松", "unknown": "时长未知", "reject": "时长不符"}


def mark_text(item):
    """给报告用的一行标记：官方MV / 白名单 / 合唱 / 译名。"""
    marks = []
    if item.get("official_mv"):
        marks.append("官方MV")
    if item.get("whitelist_hit"):
        marks.append("白名单")
    if item.get("multi"):
        marks.append("合唱?")
    if item.get("via_translation"):
        marks.append("译名")
    return "·".join(marks)


def pick_best(candidates, song, artists, ne_sec, alt_names=None,
              album="", want_fetch=True):
    """返回 (选中项, 等级中文, 备注)。只接受通过时长校验的候选。"""
    ranked, stats = rank_candidates(candidates, song, artists, ne_sec,
                                    alt_names=alt_names, album=album,
                                    want_fetch=want_fetch)
    if not ranked:
        parts = []
        if stats["blocked"]:
            parts.append(f"{stats['blocked']} 条被黑名单剔除")
        if stats["no_name"]:
            parts.append(f"{stats['no_name']} 条标题不含歌名")
        return None, "无候选", "；".join(parts) or "搜索无结果"

    good = [r for r in ranked if r["level"] in ("strict", "loose", "unknown")]
    if not good:
        return None, "无候选", "所有候选时长都不匹配，可在界面上手动指定 BV 号"

    best = good[0]
    level_key = best["level"]
    # 原曲是单人歌手时，合唱/合作版本即使排在第一位也不自动勾选，留给你确认
    if best.get("multi") and level_key == "strict":
        level_key = "loose"
    level = LEVEL_CN[level_key]
    notes = []
    if best["level"] == "loose":
        if ne_sec and best["时长秒"] and best["时长秒"] < ne_sec - STRICT_TOLERANCE:
            notes.append(f'视频比原曲短 {ne_sec - best["时长秒"]} 秒，可能被剪过')
        else:
            notes.append(f'视频是原曲的 {best["ratio"]:.0%}，偏长')
    elif best["level"] == "unknown":
        notes.append("时长信息缺失")
    if best.get("multi"):
        notes.append("疑似合唱/合作版本")
    elif (best.get("artist_total") or 0) >= 2 and \
            (best.get("artist_hits") or 0) < best.get("artist_total"):
        notes.append(f'原曲 {best["artist_total"]} 位歌手，标题只出现 '
                     f'{best["artist_hits"]} 位')
    if best.get("via_translation"):
        notes.append("标题用的是中文译名匹配上的")
    if best.get("分P"):
        notes.append(f'多P视频，用的是分P「{best["分P"]}」的时长')
    if notes:
        notes.append("请确认")
    return best, level, "；".join(notes)


def do_match(rows, workdir, logfile, limit=None, refresh=False, translate=False,
             deep=True):
    report_path = workdir / "report.csv"
    cands_path = workdir / "candidates.json"
    bind_sign_cache(workdir)
    fields = ["序号", "歌名", "歌手", "原曲时长", "匹配状态", "B站标题", "UP主",
              "播放量", "收藏量", "视频时长", "BV号", "aid", "标记", "备注"]

    done = {}
    if report_path.exists() and not refresh:
        with open(report_path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                done[r["序号"]] = r
        log(f"发现已有对照表，已匹配 {len(done)} 首，本次跳过", logfile)

    searcher = BiliSearch(logfile)
    results = dict(done)
    cache = {}
    if cands_path.exists():
        try:
            cache = json.loads(cands_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
    todo = [r for r in rows if str(r["序号"]) not in done]
    if limit:
        todo = todo[:limit]
        log(f"本次只处理前 {len(todo)} 首（--limit）", logfile)

    def flush():
        with open(report_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                key = str(r["序号"])
                if key in results:
                    w.writerow(results[key])
        cands_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    for n, row in enumerate(todo, 1):
        song, artists = row["歌名"], row["歌手"]
        ne_sec = int(row["时长秒"]) if row.get("时长秒") else None
        first_artist = artists.split("/")[0] if artists else ""
        keyword = f"{song} {first_artist}".strip()
        # 官方中文译名：B站 官方上传的外语歌 MV 常常只用中文译名
        alt_names = [v for v in (row.get("译名") or "").split("|") if v] if translate else []

        status, title, up, play, vsec, bv, aid, note = "无候选", "", "", "", "", "", "", ""
        try:
            cands = search_candidates(searcher, song, artists, alt_names=alt_names,
                                      pace=random.uniform(0.4, 0.8))
            ranked, _ = rank_candidates(cands, song, artists, ne_sec, alt_names=alt_names,
                                        album=row.get("专辑", ""), want_fetch=deep)
            cache[str(row["序号"])] = ranked
            best, level, why = pick_best(cands, song, artists, ne_sec, alt_names=alt_names,
                                         album=row.get("专辑", ""), want_fetch=deep)
            status, note = level, why
            if best:
                title, up, play = best["标题"], best["UP主"], best["播放量"]
                vsec, bv, aid = best["时长秒"], best["BV号"], best["aid"]
        except Exception as e:  # noqa: BLE001
            status, note = "出错", str(e)[:120]

        results[str(row["序号"])] = {
            "序号": row["序号"], "歌名": song, "歌手": artists,
            "原曲时长": row.get("时长", ""), "匹配状态": status,
            "B站标题": title, "UP主": up, "播放量": play,
            "收藏量": ("" if not best or best.get("收藏量") is None
                       else best.get("收藏量")),
            "视频时长": fmt_duration(vsec), "BV号": bv, "aid": aid,
            "标记": mark_text(best) if best else "", "备注": note,
        }
        mark = {"严格": "OK", "宽松": "??", "无候选": "XX", "出错": "!!"}.get(status, "--")
        log(f"[{n}/{len(todo)}] {mark} {song[:26]} -> {title[:38] or note}", logfile)

        if n % 5 == 0 or n == len(todo):
            flush()
        time.sleep(random.uniform(0.6, 1.2))

    flush()
    flush_sign_cache()
    unmatched = [results[str(r["序号"])] for r in rows
                 if str(r["序号"]) in results and results[str(r["序号"])]["匹配状态"] != "严格"]
    if unmatched:
        with open(workdir / "unmatched.csv", "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(unmatched)

    ok = sum(1 for v in results.values() if v["匹配状态"] == "严格")
    loose = sum(1 for v in results.values() if v["匹配状态"] == "宽松")
    log(f"对照表完成：严格 {ok} 首 / 宽松待确认 {loose} 首 / 无候选 {len(results) - ok - loose} 首", logfile)
    return results


# ---------------------------------------------------------------- CDP：借已登录的浏览器

class CDPClient:
    """极简 Chrome DevTools Protocol 客户端（纯标准库 WebSocket）。"""

    def __init__(self, ws_url, timeout=20):
        m = re.match(r"ws://([^:/]+):(\d+)(/.*)", ws_url)
        if not m:
            raise RuntimeError(f"无法解析调试地址: {ws_url}")
        host, port, path = m.group(1), int(m.group(2)), m.group(3)
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("与 Chrome 的调试连接被拒绝")
            buf += chunk
        if b" 101 " not in buf.split(b"\r\n")[0] + b" ":
            raise RuntimeError("调试握手失败")
        self._id = 0

    def _send_frame(self, payload):
        header = bytearray([0x81])
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RuntimeError("调试连接已断开")
            buf += chunk
        return buf

    def _recv_frame(self):
        b1, b2 = self._recv_exact(2)
        opcode = b1 & 0x0F
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        payload = self._recv_exact(length) if length else b""
        return opcode, payload

    def call(self, method, params=None, timeout=20):
        self._id += 1
        msg_id = self._id
        self._send_frame(json.dumps({"id": msg_id, "method": method,
                                     "params": params or {}}).encode())
        deadline = time.time() + timeout
        while time.time() < deadline:
            opcode, payload = self._recv_frame()
            if opcode == 0x9:  # ping -> pong
                self.sock.sendall(b"\x8a\x80" + os.urandom(4))
                continue
            if opcode != 0x1:
                continue
            data = json.loads(payload.decode("utf-8"))
            if data.get("id") != msg_id:
                continue
            if "error" in data:
                raise RuntimeError(f"CDP {method} 失败: {data['error']}")
            return data.get("result", {})
        raise RuntimeError(f"CDP {method} 超时")

    def send(self, method, params=None):
        """只发命令、不等回复。用于 Browser.close 这种会把连接一起关掉的调用。"""
        self._id += 1
        self._send_frame(json.dumps({"id": self._id, "method": method,
                                     "params": params or {}}).encode())

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def debug_endpoint(port):
    """返回 (webSocketDebuggerUrl, browser版本)，连不上则 (None, None)。"""
    try:
        raw = http_request(f"http://127.0.0.1:{port}/json/version", retries=1, timeout=3)
        data = json.loads(raw.decode("utf-8"))
        return data.get("webSocketDebuggerUrl"), data.get("Browser")
    except Exception:  # noqa: BLE001
        return None, None


def open_in_debug_browser(port, url, timeout=10):
    """在带调试端口的那个 Chrome（也就是登录用的那个）里新开标签页打开 url。

    成功返回 True；那个浏览器没在运行则返回 False。
    """
    ws, _ = debug_endpoint(port)
    if not ws:
        return False
    client = CDPClient(ws)
    try:
        client.call("Target.createTarget", {"url": url}, timeout=timeout)
        return True
    finally:
        client.close()


def find_chrome():
    """只认 Google Chrome。Edge 没测过，不写进来免得给你错误的预期。"""
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def detect_browser_name():
    """本机有没有可用的 Chrome。"""
    return "Chrome" if find_chrome() else ""


# 抑制 Chrome 写缓存/遥测的启动参数。
# 一个正常用过的 Chrome profile 能涨到 500MB+，其中 90% 以上是缓存；
# 这个工具只需要一个能登录 B站 的浏览器，用不着这些。
CACHE_SUPPRESS_FLAGS = [
    "--disk-cache-size=10485760",        # HTTP 缓存上限 10MB
    "--media-cache-size=1048576",
    "--disable-gpu-shader-disk-cache",
    "--disable-application-cache",
    "--disable-background-networking",   # 后台更新/上报
    "--disable-component-update",
    "--disable-breakpad",                # 不写崩溃转储
    "--disable-domain-reliability",
    "--disable-client-side-phishing-detection",
    "--metrics-recording-only",
    "--no-pings",
    "--no-service-autorun",
    "--disable-default-apps",
    "--disable-sync",
    "--no-first-run",
    "--no-default-browser-check",
]

# 可以直接删掉的缓存/遥测目录（Chrome 会自己重建，删了不影响登录）
BROWSER_CACHE_DIRS = [
    "Default/Cache", "Default/Code Cache", "Default/GPUCache",
    "Default/DawnCache", "Default/DawnGraphiteCache", "Default/DawnWebGPUCache",
    "Default/GraphiteDawnCache", "Default/ShaderCache", "Default/GrShaderCache",
    "Default/optimization_guide_model_store",
    "Default/optimization_guide_hint_cache",
    "GrShaderCache", "ShaderCache", "GraphiteDawnCache",
    "BrowserMetrics", "Crashpad", "Safe Browsing",
    "OptimizationGuideModels", "OptimizationGuideModelsManifest",
    "OptimizationHints", "component_crx_cache", "extensions_crx_cache",
]

# 这些是登录态，绝对不能删（代码里也不会碰根目录下其他任何东西）
BROWSER_KEEP = [
    "Local State",                  # 加密密钥
    "Default/Network/Cookies",      # 登录 Cookie 就在这里
    "Default/Preferences",
    "Default/Local Storage",
    "Default/IndexedDB",
]


def _dir_size(path):
    total = 0
    try:
        for f in Path(path).rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def clean_browser_profile(profile_dir):
    """清掉专用 Chrome 的缓存目录，保留登录态。

    只删 BROWSER_CACHE_DIRS 里列出的目录，其余一律不动。
    返回 (释放的字节数, 因文件被占用而没删掉的字节数)。
    浏览器正在运行时部分文件会被锁定，所以第二个值可能不为零——
    在启动浏览器**之前**调用不会有这个问题。
    """
    profile = Path(profile_dir)
    if not profile.exists():
        return 0, 0
    freed = 0
    locked = 0
    for rel in BROWSER_CACHE_DIRS:
        target = profile / rel
        if not target.exists():
            continue
        before = _dir_size(target)
        shutil.rmtree(target, ignore_errors=True)
        after = _dir_size(target) if target.exists() else 0
        freed += max(before - after, 0)
        locked += after
    return freed, locked


def launch_browser(workdir, port):
    """启动带调试端口的专用 Chrome（独立 profile，不碰你日常浏览器）。

    启动前会清掉上一次遗留的缓存目录，并带上抑制缓存的参数，
    避免 profile 目录越用越大。返回 (profile 路径, 本次释放的字节数)。
    """
    exe = find_chrome()
    if not exe:
        raise RuntimeError("没找到 Google Chrome，请先安装 Chrome，"
                           "或手动改 find_chrome() 里的路径")
    profile = workdir / "bili_profile"
    profile.mkdir(parents=True, exist_ok=True)
    freed, locked = clean_browser_profile(profile)
    args = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile}"]
    args += CACHE_SUPPRESS_FLAGS
    args.append("https://www.bilibili.com/")
    subprocess.Popen(args, close_fds=True)
    return profile, freed, locked


def cleanup_browser_caches(workdir):
    """清理工作目录下 bili_profile 里的缓存（程序退出时调用）。

    **只删缓存目录**：登录态（`Default/Network/Cookies`、`Local State`）和
    其余任何文件都不动——反复重新登录会触发 B站 的登录风控，所以登录态必须留着。
    浏览器还开着时，被占用的文件会自动跳过。

    返回 (释放的字节数, 被占用没删掉的字节数)。
    """
    workdir = Path(workdir).resolve()
    profile = (workdir / "bili_profile").resolve()
    # 双保险：只允许在工作目录内部动手
    if not str(profile).startswith(str(workdir)) or not profile.exists():
        return 0, 0
    return clean_browser_profile(profile)


def close_debug_browser(port):
    """让那个带调试端口的 Chrome 自己退出。

    通过 CDP 的 Browser.close 命令，**不调用任何外部命令**。
    返回 True 表示命令已发出（浏览器稍后自己退出）。
    """
    ws, _ = debug_endpoint(port)
    if not ws:
        return False
    try:
        client = CDPClient(ws)
    except Exception:  # noqa: BLE001
        return False
    try:
        client.send("Browser.close")
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def wipe_browser_profile(workdir, wait_seconds=3.0):
    """彻底删除工作目录下的 bili_profile（缓存 + 登录态都删）。

    会先等着 Chrome 把文件句柄放开。返回 (是否删干净, 残留字节数)。
    """
    workdir = Path(workdir).resolve()
    profile = (workdir / "bili_profile").resolve()
    # 双保险：只允许在工作目录内部动手
    if not str(profile).startswith(str(workdir)) or not profile.exists():
        return True, 0
    deadline = time.time() + wait_seconds
    while True:
        shutil.rmtree(profile, ignore_errors=True)
        if not profile.exists():
            return True, 0
        if time.time() >= deadline:
            return False, _dir_size(profile)
        time.sleep(0.3)


def read_browser_cookies(port):
    """从调试端口读取 bilibili.com 的 Cookie；连不上返回 None。"""
    ws, _ = debug_endpoint(port)
    if not ws:
        return None
    client = CDPClient(ws)
    try:
        cookies = client.call("Storage.getCookies").get("cookies") or []
    finally:
        client.close()
    jar = {}
    for c in cookies:
        if "bilibili.com" in c.get("domain", ""):
            jar.setdefault(c["name"], c["value"])
    return jar


# ---------------------------------------------------------------- 阶段3：写入收藏夹

BILI_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}


class BiliWriter:
    def __init__(self, cookies, logfile=None):
        missing = [k for k in ("SESSDATA", "bili_jct") if not cookies.get(k)]
        if missing:
            raise RuntimeError(
                f"浏览器里没有 {'/'.join(missing)}，说明那个 Chrome 还没登录 B站。"
                "请先运行 --launch-browser，在弹出的窗口里扫码登录。")
        self.logfile = logfile
        self.csrf = cookies["bili_jct"]
        self.headers = dict(BILI_HEADERS)
        self.headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

    def _post(self, url, fields):
        fields = dict(fields, csrf=self.csrf)
        headers = dict(self.headers, **{"Content-Type": "application/x-www-form-urlencoded"})
        try:
            raw = http_request(url, headers=headers,
                               data=urllib.parse.urlencode(fields),
                               method="POST", retries=1, timeout=20)
        except urllib.error.HTTPError as e:
            if e.code == 412:
                return {"code": -412, "message": "被风控拦截（412）"}
            raise
        return json.loads(raw.decode("utf-8"))

    def _get(self, url):
        return http_json(url, headers=self.headers)

    def my_mid(self):
        nav = http_json("https://api.bilibili.com/x/web-interface/nav", headers=self.headers)
        if nav.get("code") != 0 or not (nav.get("data") or {}).get("isLogin"):
            raise RuntimeError("当前浏览器未登录 B站（nav 接口返回未登录）")
        return nav["data"]["mid"]

    def list_folders(self, mid):
        url = f"https://api.bilibili.com/x/v3/fav/folder/created/list-all?up_mid={mid}"
        data = http_json(url, headers=self.headers)
        if data.get("code") != 0:
            raise RuntimeError(f"读取收藏夹列表失败: {data.get('code')} {data.get('message')}")
        return (data.get("data") or {}).get("list") or []

    def folder_media(self, mlid):
        """取收藏夹里已有内容的 aid 和 bvid，用于写入前跳过重复项。

        返回 (aids, bvids) 两个集合。收藏夹为空时返回两个空集合。
        """
        url = f"https://api.bilibili.com/x/v3/fav/resource/ids?media_id={mlid}&platform=web"
        data = self._get(url)
        code = data.get("code")
        if code != 0:
            raise RuntimeError(f"读取收藏夹已有内容失败：code={code} {data.get('message')}")
        aids, bvids = set(), set()
        for item in data.get("data") or []:
            if item.get("id") is not None:
                aids.add(str(item["id"]))
            bv = item.get("bvid") or item.get("bv_id")
            if bv:
                bvids.add(bv)
        return aids, bvids

    def resolve_folder(self, mid, title=None, folder_id=None):
        folders = self.list_folders(mid)
        if folder_id:
            for f in folders:
                if str(f["id"]) == str(folder_id):
                    return f
            raise RuntimeError(f"你的收藏夹里没有 id={folder_id} 这个夹")
        if not folders:
            raise RuntimeError("你的账号还没有任何收藏夹，请先在 B站 手动建一个空的")
        exact = [f for f in folders if f["title"] == title]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise RuntimeError(f"有 {len(exact)} 个同名收藏夹「{title}」，请用 --folder-id 指定")
        near = [f for f in folders if norm(f["title"]) == norm(title or "")]
        if len(near) == 1:
            return near[0]
        log(f"没找到名为「{title}」的收藏夹。你现有的收藏夹：", self.logfile)
        for f in folders:
            log(f"  - {f['title']}  (id={f['id']}, 已有 {f['media_count']} 个内容)", self.logfile)
        raise RuntimeError("请确认收藏夹名，或用 --folder-id 直接指定 id")

    def add_video(self, aid, mlid):
        return self._post("https://api.bilibili.com/x/v3/fav/resource/deal",
                          {"rid": aid, "type": 2, "add_media_ids": mlid})

    def create_folder(self, title, privacy=0, intro=""):
        """新建收藏夹。privacy: 0=公开, 1=私密。"""
        resp = self._post("https://api.bilibili.com/x/v3/fav/folder/add",
                          {"title": title, "privacy": privacy, "intro": intro})
        code = resp.get("code")
        if code != 0:
            raise RuntimeError(f"新建收藏夹失败：code={code} {resp.get('message')}")
        return resp.get("data") or {}


BV_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")

# ---------------------------------------------------------------- 深查（官方 MV 判定用）
# 粉丝号/搬运号常把歌手名写进个人签名，实测这类误判比真官方还多，所以加否决词。
FAN_MARK = re.compile(r"粉丝|歌迷|后援|搬运|剪辑|私藏|安利|字幕组|民间|非官方|应援")
# 模糊歌名匹配的相似度门槛（仅在歌手或专辑已确认匹配时才启用）
FUZZY_MIN_RATIO = 0.85
# 短歌手名（≤2 字）要求它占 UP主 名字的比例，避免"小缘"命中"最喜欢小缘的香香"
UP_NAME_MIN_COVER = 0.6


def up_name_matches(up_name_norm, artist_keys):
    """UP主名是否算"就是这位歌手本人"。

    实测踩过的坑：粉丝号「最喜欢小缘的香香」「小缘粉丝俱乐部」都含歌手名「小缘」，
    单纯用子串包含会把它们当成官方。所以：
      · 名字里带粉丝/搬运/剪辑等词的，直接否决
      · 短歌手名（≤2 字）要求它占名字的 60% 以上
    """
    if not up_name_norm or not artist_keys:
        return False
    if FAN_MARK.search(up_name_norm):
        return False
    for k in artist_keys:
        if not k or k not in up_name_norm:
            continue
        if len(k) >= 3:
            return True
        if len(k) / len(up_name_norm) >= UP_NAME_MIN_COVER:
            return True
    return False

_detail_cache = {}
_sign_cache = {}
_sign_cache_path = None
_sign_dirty = 0
SIGN_FLUSH_EVERY = 25      # 每新增这么多条才写一次盘，避免每查一个 UP主 就写文件


def bind_sign_cache(workdir):
    """把 UP主签名缓存落到磁盘，重跑时不用再查一遍。"""
    global _sign_cache_path
    _sign_cache_path = Path(workdir) / "up_sign_cache.json"
    if _sign_cache_path.exists():
        try:
            data = json.loads(_sign_cache_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _sign_cache.update({str(k): v for k, v in data.items()})
        except (OSError, ValueError):
            pass
    return _sign_cache


def _flush_sign_cache(force=False):
    """把签名缓存写盘。默认只在攒够一批或有明确需要时才写。"""
    global _sign_dirty
    if not _sign_cache_path or (not force and not _sign_dirty):
        return
    try:
        _sign_cache_path.write_text(
            json.dumps(_sign_cache, ensure_ascii=False), encoding="utf-8")
        _sign_dirty = 0
    except OSError:
        pass


def flush_sign_cache():
    """外部在跑完一轮匹配后调用，把剩余的缓存落盘。"""
    _flush_sign_cache(force=True)


def video_detail(bvid, timeout=15):
    """视频详情（带缓存）：简介、分P列表、UP主 mid 和名字。

    多P识别和官方 MV 判定共用这一次请求。
    """
    if not bvid:
        return {}
    if bvid in _detail_cache:
        return _detail_cache[bvid]
    try:
        j = http_json(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
                      headers=BILI_HEADERS, retries=1, timeout=timeout)
        d = j.get("data") or {}
        info = {
            "desc": d.get("desc") or "",
            "pages": d.get("pages") or [],
            "videos": d.get("videos") or 1,
            "mid": (d.get("owner") or {}).get("mid"),
            "up": (d.get("owner") or {}).get("name") or "",
        }
    except Exception:  # noqa: BLE001
        info = {"desc": "", "pages": [], "videos": 1, "mid": None, "up": ""}
    _detail_cache[bvid] = info
    return info


def up_signature(mid, timeout=10):
    """UP主个人签名（带缓存）。card 接口不需要登录，实测不限流。"""
    global _sign_dirty
    if not mid:
        return ""
    key = str(mid)
    if key in _sign_cache:
        return _sign_cache[key]
    try:
        j = http_json(f"https://api.bilibili.com/x/web-interface/card?mid={mid}",
                      headers=BILI_HEADERS, retries=1, timeout=timeout)
        sign = ((j.get("data") or {}).get("card") or {}).get("sign") or ""
    except Exception:  # noqa: BLE001
        sign = ""
    _sign_cache[key] = sign
    _sign_dirty += 1
    if _sign_dirty >= SIGN_FLUSH_EVERY:
        _flush_sign_cache()
    return sign


def best_part_duration(detail, ne_sec):
    """多P视频：挑一个时长最贴近原曲的分P。返回 (秒, 分P标题) 或 None。"""
    pages = (detail or {}).get("pages") or []
    if len(pages) < 2 or not ne_sec:
        return None
    best = min(pages, key=lambda p: abs((p.get("duration") or 0) - ne_sec))
    return best.get("duration"), (best.get("part") or "")


def fuzzy_contains(title_norm, name_norm, cutoff=FUZZY_MIN_RATIO):
    """标题里是否含有"和歌名差一点点"的片段。

    实测：时间过得太快 / 时间啊过得太快了 = 0.86，能过；
    但短歌名必须禁用模糊，否则「光」会命中「光辉岁月」。
    """
    if not title_norm or not name_norm:
        return False
    n = len(name_norm)
    if n < 3:
        return False          # 短歌名一律不做模糊/包含判定，否则「光」会命中「光辉岁月」
    if name_norm in title_norm:
        return True
    best = 0.0
    for size in (n - 1, n, n + 1, n + 2):
        if size <= 0 or size > len(title_norm):
            continue
        for i in range(len(title_norm) - size + 1):
            r = difflib.SequenceMatcher(None, name_norm, title_norm[i:i + size]).ratio()
            if r > best:
                best = r
                if best >= cutoff:
                    return True
    return best >= cutoff


def bvid_to_info(bvid, timeout=20):
    """按 BV 号查视频信息（不需要登录）。实测：失效视频 -404，格式错 -400。"""
    bvid = (bvid or "").strip()
    if not BV_RE.match(bvid):
        raise RuntimeError("BV 号格式不对，应该形如 BV1bs4y1u7rk（BV + 10 位）")
    data = http_json(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
                     headers=BILI_HEADERS, timeout=timeout, retries=2)
    code = data.get("code")
    if code == -404:
        raise RuntimeError("这个 BV 号对应的视频不存在或已被删除")
    if code != 0:
        raise RuntimeError(f"查询失败：code={code} {data.get('message')}")
    d = data["data"]
    return {
        "标题": d.get("title", ""),
        "时长秒": d.get("duration"),
        "aid": d.get("aid"),
        "UP主": (d.get("owner") or {}).get("name", ""),
        "分P数": d.get("videos", 1),
    }


def do_push(rows, results, workdir, logfile, folder_title, folder_id,
            port, max_count, allow_loose=False, interval=(4, 8)):
    cookies = read_browser_cookies(port)
    if cookies is None:
        raise RuntimeError(
            f"连不上 127.0.0.1:{port} 的 Chrome。请先运行：\n"
            f"    {runner_name()} --launch-browser\n"
            f"然后在弹出的窗口里登录 B站，并保持窗口打开。")

    writer = BiliWriter(cookies, logfile)
    mid = writer.my_mid()
    folder = writer.resolve_folder(mid, folder_title, folder_id)
    log(f"已锁定收藏夹：{folder['title']} (id={folder['id']}, 现有 {folder['media_count']} 个内容)", logfile)
    if folder.get("attr", 0) & 1:
        log("提示：这个收藏夹是私密的（attr 第 0 位 = 1）", logfile)

    state_path = workdir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    pushed = state.setdefault("pushed", {})

    todo = []
    for r in rows:
        item = results.get(str(r["序号"]))
        if not item or not item.get("aid"):
            continue
        if item["匹配状态"] == "严格" or (item["匹配状态"] == "宽松" and allow_loose):
            if str(item["aid"]) not in pushed:
                todo.append(item)
    if max_count:
        todo = todo[:max_count]
    if not todo:
        log("没有需要新增的条目（已处理过，或没有合格匹配）", logfile)
        return
    log(f"本次待写入 {len(todo)} 首，预计 {max(1, len(todo) * 6 // 60)} 分钟", logfile)

    ok = fail = consec = 0
    for n, item in enumerate(todo, 1):
        try:
            resp = writer.add_video(item["aid"], folder["id"])
            code = resp.get("code")
        except Exception as e:  # noqa: BLE001
            code, resp = "EXC", {"message": str(e)[:100]}

        if code == 0:
            ok += 1
            consec = 0
            pushed[str(item["aid"])] = {
                "title": item["B站标题"], "folder": folder["id"],
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            log(f"[{n}/{len(todo)}] OK   {item['歌名'][:24]} -> {item['B站标题'][:34]}", logfile)
        else:
            fail += 1
            consec += 1
            message = resp.get("message")
            log(f"[{n}/{len(todo)}] FAIL code={code} {message}  {item['歌名'][:24]}", logfile)
            if code in (-352, -412, -509):
                (workdir / "halt_reason.txt").write_text(
                    f"接口返回 {code}（风控/限流），已停车。本次已完成 {ok} 首。\n"
                    f"信息：{message}\n", encoding="utf-8")
                log("检测到风控信号，已停车。休息几小时后再跑，会自动续上。", logfile)
                break
            if consec >= 5:
                log("连续 5 条失败，已停车。请查看 halt_reason.txt", logfile)
                break

        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        if n < len(todo):
            time.sleep(random.uniform(*interval))
            if n % 50 == 0:
                rest = random.uniform(60, 120)
                log(f"已写入 {n} 首，休息 {rest:.0f} 秒…", logfile)
                time.sleep(rest)

    log(f"阶段3 结束：成功 {ok} 首 / 失败 {fail} 首", logfile)


# ---------------------------------------------------------------- 入口

def pause_for_double_click():
    """双击 exe 运行时，避免窗口一闪而过看不到信息。"""
    if getattr(sys, "frozen", False) and sys.stdin and sys.stdin.isatty():
        try:
            input("\n按回车键退出…")
        except (EOFError, KeyboardInterrupt):
            pass


def runner_name():
    """用于提示信息里展示正确的调用方式（exe 还是 python xxx.py）。"""
    name = Path(sys.argv[0]).name
    return name if getattr(sys, "frozen", False) else f"python {name}"


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) == 1:
        # 双击运行：打印用法后停住，让窗口留着
        print(__doc__)
        print("这是命令行工具，需要一个参数才能开始。常用示例：")
        print('  bili_sync.exe --playlist "https://music.163.com/playlist?id=XXXXXXXXX"')
        print("详细说明见同目录下的 README.md")
        pause_for_double_click()
        return

    ap = argparse.ArgumentParser(
        description="网易云音乐歌单 -> B站收藏夹 迁移工具",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--playlist", help="网易云歌单分享链接或纯数字 id")
    ap.add_argument("--out", default=DEFAULT_WORKDIR, help=f"工作目录（默认 {DEFAULT_WORKDIR}）")
    ap.add_argument("--refresh", action="store_true", help="强制重新导出并重跑匹配")
    ap.add_argument("--limit", type=int, help="本次最多匹配多少首（试跑用）")
    ap.add_argument("--translate", action="store_true",
                    help="把网易云自带的中文译名也加入搜索（不限语种，日文歌英文歌都适用）")
    ap.add_argument("--sort", metavar="规则,规则",
                    help="自定义排序优先级，逗号分隔。可用：" +
                         "、".join(f"{k}({v})" for k, v in SORT_RULE_LABELS.items()))
    ap.add_argument("--no-deep", action="store_true",
                    help="跳过深查（不查多P、不查UP主签名），速度更快但官方MV判定会退化")
    ap.add_argument("--allow-loose", action="store_true",
                    help="把「宽松」匹配也一起写入（默认不写，留人工确认）")
    ap.add_argument("--launch-browser", action="store_true",
                    help="启动带调试端口的专用 Chrome（首次需扫码登录）")
    ap.add_argument("--port", type=int, default=9222, help="调试端口，默认 9222")
    ap.add_argument("--push", action="store_true", help="执行阶段3：写入收藏夹")
    ap.add_argument("--folder", help="目标 B站收藏夹名（建议与歌单同名）")
    ap.add_argument("--folder-id", type=int, help="目标收藏夹 id（mlid），优先于 --folder")
    ap.add_argument("--max", type=int, default=150, help="单次最多写入多少首，默认 150")
    args = ap.parse_args()

    workdir = Path(args.out)
    workdir.mkdir(parents=True, exist_ok=True)
    logfile = workdir / "log.txt"
    load_sort_rules(workdir)
    if args.sort:
        en, _dis = save_sort_rules(workdir, [r.strip() for r in args.sort.split(",") if r.strip()])
        log("排序优先级：" + " > ".join(SORT_RULE_LABELS[r] for r in en), logfile)

    if args.launch_browser:
        ws, ver = debug_endpoint(args.port)
        if ws:
            log(f"检测到 127.0.0.1:{args.port} 已经有调试中的浏览器（{ver}），直接复用。", logfile)
        else:
            profile, freed, locked = launch_browser(workdir, args.port)
            log(f"已启动专用 Chrome，profile 目录：{profile}", logfile)
            if freed:
                log(f"启动前清理了上次遗留的浏览器缓存，释放 {freed / 1024 / 1024:.1f} MB", logfile)
        log("请在窗口里登录 B站（扫码即可），登录完成后保持窗口打开。", logfile)
        log("提示：这是独立 profile，不影响你日常使用的 Chrome。", logfile)
        return

    if not args.playlist and not (args.push and (args.folder or args.folder_id)):
        ap.print_help()
        pause_for_double_click()
        return

    if args.playlist:
        rows, meta = do_export(args.playlist, workdir, logfile, args.refresh)
        results = do_match(rows, workdir, logfile, args.limit, args.refresh,
                           translate=args.translate, deep=not args.no_deep)
    else:
        csv_path = workdir / "playlist.csv"
        if not csv_path.exists():
            raise SystemExit("工作目录里没有 playlist.csv，请先用 --playlist 指定歌单链接")
        with open(csv_path, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        meta = {"歌单名": ""}
        report_path = workdir / "report.csv"
        results = {}
        if report_path.exists():
            with open(report_path, encoding="utf-8-sig") as f:
                results = {r["序号"]: r for r in csv.DictReader(f)}

    if args.push:
        folder = args.folder or meta.get("歌单名") or ""
        if not folder and not args.folder_id:
            raise SystemExit("请用 --folder 指定收藏夹名，或 --folder-id 指定 id")
        log(f"即将写入收藏夹「{folder or args.folder_id}」，单次上限 {args.max} 首", logfile)
        do_push(rows, results, workdir, logfile, folder, args.folder_id,
                args.port, args.max, allow_loose=args.allow_loose)
    else:
        log("以上只是对照表，没有写入任何东西。", logfile)
        log(f'确认无误后运行：{runner_name()} --push --folder "'
            f'{meta.get("歌单名", "收藏夹名")}"', logfile)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断。")
    except SystemExit:
        raise
    except Exception as err:  # noqa: BLE001 - 顶层统一兜底，避免双击时看不到错误
        print(f"\n[错误] {err}")
        pause_for_double_click()
        sys.exit(1)
