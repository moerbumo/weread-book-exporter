#!/usr/bin/env python3
"""WeRead Full-Book Crawler (crawl_weread.py)

核心特性：
1. 自动切换至双栏水平翻页模式，解决网页端上下滚动模式下因虚拟滚动分片导致章节下半部分漏字的问题。
2. 捕获 Canvas 2D 绘制指令与分栏坐标，精准切分每一单页，消除跨页文字重复或交错。
3. 基于官方目录（_catalog.json）按阅读顺序精确切分章节；非目录小标题自动降级为章内 ## 子标题。
4. 可靠完结判定：只有「目录全部覆盖 + 末章后连续空页」才算完成；连续空页触发救援重试，
   救援失败只保存断点、绝不误标完成，重跑即自动续传。
5. 逐页实时落盘（raw/page_*.json）+ 状态文件双重断点保护；结束时以 raw 为准统一重建章节。
6. 抓取节奏三档可调（fast / normal / gentle，默认 normal 均衡档），空页自动退避降速。
7. 基于 Cookie (wr_vid/wr_skey) 精准鉴权，支持 300 秒扫码等待。
8. 自动抓取书籍元数据（书名、作者、简介）并下载原版高清封面（cover.jpg）。
9. 智能适配系统内置 Edge / Chrome，免去下载 200MB 浏览器的漫长等待。
"""
import asyncio
import collections
import json
import os
import random
import re
import sys
import time
import urllib.request

from playwright.async_api import async_playwright

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = os.path.dirname(os.path.abspath(__file__))
USER_DATA_DIR = os.environ.get("WEREAD_PROFILE", os.path.join(BASE, "cache", "browser_profile"))

# ---------------------------------------------------------------- 抓取节奏配置
# 三档速度：fast 快速 / normal 均衡（默认）/ gentle 温和
SPEED_PROFILES = {
    "fast":   {"page_delay": (2.0, 4.0),  "pause_every": (40, 60), "pause_range": (30.0, 60.0)},
    "normal": {"page_delay": (5.0, 8.0),  "pause_every": (25, 35), "pause_range": (45.0, 75.0)},
    "gentle": {"page_delay": (8.0, 20.0), "pause_every": (10, 20), "pause_range": (60.0, 120.0)},
}
DEFAULT_PROFILE = "normal"

# ---------------------------------------------------------------- 完结判定与救援
STALE_LIMIT = 6        # 未到末章时，连续空页达到该值触发救援
END_STALE_LIMIT = 3    # 目录已全部覆盖后，连续空页达到该值判定全书结束
RESCUE_TRIES = 4       # 救援最大次数
MAX_STEPS = 5000       # 安全上限，防止死循环


def resolve_profile(name=None):
    """解析速度档位：参数 > 环境变量 WEREAD_SPEED > 默认均衡档"""
    name = os.environ.get("WEREAD_SPEED") or name or DEFAULT_PROFILE
    if name not in SPEED_PROFILES:
        print(f"⚠️ 未知速度档位 {name}，使用默认均衡档 (normal)")
        name = DEFAULT_PROFILE
    return name


def next_long_pause_at(step):
    cfg_name = resolve_profile()
    lo, hi = SPEED_PROFILES[cfg_name]["pause_every"]
    return step + random.randint(lo, hi)


# ---------------------------------------------------------------- 状态与落盘
def get_crawl_state(book_dir):
    sf = os.path.join(book_dir, "_state.json")
    if os.path.exists(sf):
        try:
            with open(sf, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"completed": False, "saved_chapter_count": 0, "last_step": 0, "total_words": 0}


def save_crawl_state(book_dir, state):
    sf = os.path.join(book_dir, "_state.json")
    try:
        with open(sf, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 保存状态失败: {e}")


def load_catalog(book_dir):
    fp = os.path.join(book_dir, "_catalog.json")
    if os.path.isfile(fp):
        try:
            with open(fp, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [str(x) for x in data]
        except Exception:
            pass
    return []


def read_completed_titles(raw_dir):
    """从 raw/chap_*.json 读取已落盘章节标题列表（按落盘顺序）"""
    titles = []
    if os.path.isdir(raw_dir):
        for cf in sorted(os.listdir(raw_dir)):
            if cf.startswith("chap_") and cf.endswith(".json"):
                try:
                    with open(os.path.join(raw_dir, cf), encoding="utf-8") as f:
                        cdata = json.load(f)
                    if cdata.get("title"):
                        titles.append(cdata["title"])
                except Exception:
                    pass
    return titles


def max_page_step(raw_dir):
    """已落盘页面的最大编号 + 1（即下一次继续抓取的页号）"""
    steps = []
    if os.path.isdir(raw_dir):
        for f in os.listdir(raw_dir):
            m = re.match(r"^page_(\d+)\.json$", f)
            if m:
                steps.append(int(m.group(1)))
    return max(steps) + 1 if steps else 0


def drop_partial_chapter_pages(raw_dir, chapter_title):
    """断点续传时清理目标章节的历史残页，确保该章从头完整重抓，避免内容重复"""
    target = norm(chapter_title)
    removed = 0
    if os.path.isdir(raw_dir):
        for f in sorted(os.listdir(raw_dir)):
            if not (f.startswith("page_") and f.endswith(".json")):
                continue
            fp = os.path.join(raw_dir, f)
            try:
                with open(fp, encoding="utf-8") as fh:
                    chapter = json.load(fh).get("chapter") or ""
            except Exception:
                continue
            if norm(chapter) == target:
                try:
                    os.remove(fp)  # 先关闭句柄再删除（Windows 下打开状态的文件不可删）
                    removed += 1
                except Exception:
                    pass
    if removed:
        print(f"  🧹 已清理断点章节 {removed} 个历史残页，将从头完整重抓该章。", flush=True)


def catalog_from_chapters(md_dir):
    """_catalog.json 缺失时，从章节文件首行标题还原目录（避免已完成书籍被重抓）"""
    titles = []
    if os.path.isdir(md_dir):
        for fn in sorted(os.listdir(md_dir)):
            if fn.endswith(".md"):
                try:
                    with open(os.path.join(md_dir, fn), encoding="utf-8") as f:
                        first = f.readline().strip()
                    if first.startswith("# "):
                        titles.append(first[2:].strip())
                except Exception:
                    pass
    return titles


def _same_block(a, b):
    if a.get("type") != b.get("type"):
        return False
    if a["type"] == "img":
        return a.get("src") == b.get("src")
    return a.get("cls") == b.get("cls") and a.get("text") == b.get("text")


def resplit_blocks(catalog, stream):
    """
    按目录重新切分块流（与实时抓取同一套匹配逻辑），返回 [(章节标题, 块列表)]。
    不依赖页面记录的 chapter 字段，可修复实时切分滞后/错乱的历史数据。
    """
    sections = []
    cur_title = catalog[0] if catalog else "扉页"
    cat_pos = 0
    buf = []
    for b in stream:
        if b.get("type") == "img":
            buf.append(b)
            continue
        txt = (b.get("text") or "").strip()
        cls = b.get("cls") or "js"
        mtitle, remainder, new_pos = match_catalog_line(txt, cls, catalog, cat_pos)
        if mtitle is not None:
            if norm(mtitle) != norm(cur_title):
                if buf:
                    sections.append((cur_title, buf))
                buf = []
                cur_title = mtitle
                cat_pos = new_pos
            else:
                cat_pos = max(cat_pos, new_pos)
            if remainder:
                buf.append({"type": "line", "cls": "js", "text": remainder})
            continue
        if cls == "title" and norm(txt) == norm(cur_title):
            continue  # 当前章标题行本身
        if cls == "title":
            buf.append({"type": "line", "cls": "title", "text": txt})
        else:
            buf.append(b)
    if buf:
        sections.append((cur_title, buf))
    return sections


def rebuild_chapters_from_raw(book_id):
    """
    从 raw/page_*.json 恢复并重新生成所有章节 chapters/*.md 与 raw/chap_*.json。
    按页序重组、相邻重复页/重复块去重、子标题 (##) 还原。
    即使爬取过程异常终止，也可完整抢救出所有已捕获的章节内容。
    """
    book_dir = os.path.join(BASE, "output", book_id + "_v2")
    raw_dir = os.path.join(book_dir, "raw")
    md_dir = os.path.join(book_dir, "chapters")
    os.makedirs(md_dir, exist_ok=True)
    os.makedirs(raw_dir, exist_ok=True)

    if not os.path.isdir(raw_dir):
        return 0

    page_files = sorted([f for f in os.listdir(raw_dir) if f.startswith("page_") and f.endswith(".json")])
    if not page_files:
        return 0

    # 收集块流（页级相邻去重 + 块级相邻去重），同时记录页面章节字段作为无目录时的兜底分组
    stream = []
    page_groups = []
    cur_t = None
    cur_b = []
    last_sig = None
    last_blk = None

    for pf in page_files:
        fp = os.path.join(raw_dir, pf)
        try:
            with open(fp, "r", encoding="utf-8") as f:
                pdata = json.load(f)
        except Exception:
            continue

        page_ch = pdata.get("chapter") or "扉页"
        pblocks = pdata.get("blocks")
        if not pblocks:
            pblocks = []
            for ln in pdata.get("lines", []):
                pblocks.append({"type": "line", "cls": "js", "text": ln})
            for im in pdata.get("images", []):
                pblocks.append({"type": "img", "src": im.get("src", "")})

        # 相邻重复页去重（断点重叠重抓导致）
        sig = norm("".join(b.get("text", "") for b in pblocks if b.get("type") == "line"))
        if sig and len(sig) > 8 and sig == last_sig:
            continue
        if sig and len(sig) > 8:
            last_sig = sig

        # 兜底分组（按页面章节字段）
        if cur_t is None:
            cur_t = page_ch
        elif norm(page_ch) != norm(cur_t):
            if cur_b:
                page_groups.append((cur_t, cur_b))
            cur_t = page_ch
            cur_b = []
            last_blk = None

        for b in pblocks:
            if last_blk is not None and _same_block(last_blk, b):
                continue  # 页边界重复块
            stream.append(b)
            cur_b.append(b)
            last_blk = b

    if cur_t and cur_b:
        page_groups.append((cur_t, cur_b))

    # 优先按目录重新切分（修复实时切分错乱）；目录缺失时退回页面章节字段分组
    catalog = load_catalog(book_dir)
    if catalog:
        sections = resplit_blocks(catalog, stream)
    else:
        sections = page_groups

    # 合并同名相邻章节
    merged = []
    for t, b in sections:
        if merged and norm(merged[-1][0]) == norm(t):
            merged[-1][1].extend(b)
        else:
            merged.append((t, b))

    saved_idx = 0
    total_words = 0
    for t, blocks in merged:
        saved_idx += 1
        body, im_list = render_md(t, blocks, saved_idx)
        txt_len = len(re.sub(r"<[^>]+>", "", body))
        with open(os.path.join(md_dir, "%04d.md" % saved_idx), "w", encoding="utf-8") as f:
            f.write(body)
        with open(os.path.join(raw_dir, "chap_%04d.json" % saved_idx), "w", encoding="utf-8") as f:
            json.dump({"title": t, "images": im_list, "text_len": txt_len}, f, ensure_ascii=False)
        total_words += txt_len

    save_crawl_state(book_dir, {
        "book_id": book_id,
        "completed": False,
        "last_step": len(page_files),
        "saved_chapter_count": saved_idx,
        "last_title": cur_t or "",
        "total_words": total_words,
    })
    return saved_idx


def download_all_images(raw_dir, img_dir):
    """统一检查并补全下载所有章节配图"""
    os.makedirs(img_dir, exist_ok=True)
    tasks = []
    if os.path.isdir(raw_dir):
        for jf in sorted(os.listdir(raw_dir)):
            if jf.startswith("chap_") and jf.endswith(".json"):
                try:
                    with open(os.path.join(raw_dir, jf), encoding="utf-8") as f:
                        for im in json.load(f).get("images", []):
                            tasks.append((im["src"], im.get("name", "")))
                except Exception:
                    pass

    ok_img = 0
    for url, name in tasks:
        if not name:
            ext = "jpg"
            m = re.search(r"\.(jpg|jpeg|png|gif|webp)", url.lower())
            if m:
                ext = m.group(1).replace("jpeg", "jpg")
            name = f"img_{abs(hash(url)) % 1000000:06d}.{ext}"
        fp = os.path.join(img_dir, name)
        if os.path.exists(fp) and os.path.getsize(fp) > 500:
            ok_img += 1
            continue
        try:
            req = urllib.request.Request(url, headers={"Referer": "https://weread.qq.com/", "User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=20).read()
            if len(raw) > 500:
                with open(fp, "wb") as f:
                    f.write(raw)
                ok_img += 1
        except Exception:
            pass
    if tasks:
        print(f"🖼️ 章节配图就绪: {ok_img}/{len(tasks)}", flush=True)


def extract_book_id(raw_input):
    """从纯 ID、完整网页链接或带参数的分享链接中智能提取书籍 ID"""
    if not raw_input:
        return ""
    text = str(raw_input).strip()
    m = re.search(r"(?:reader|bookDetail)/([a-zA-Z0-9_-]+)", text)
    if m:
        return m.group(1)
    m_id = re.search(r"^[a-zA-Z0-9_-]{6,36}$", text)
    if m_id:
        return m_id.group(0)
    clean = re.sub(r"^https?://[^/]+/", "", text).split("?")[0].strip("/")
    return clean or text


HOOK = """
(function() {
    window.__wr_chars = [];
    window.__wr_seq = 0;
    var origClear = CanvasRenderingContext2D.prototype.clearRect;
    CanvasRenderingContext2D.prototype.clearRect = function(x, y, w, h) {
        if (w > 100 && h > 100) window.__wr_seq++;
        return origClear.apply(this, arguments);
    };
    var origFill = CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText = function(text, x, y) {
        if (text && text.trim()) {
            var st = (this.canvas && this.canvas.getAttribute('style')) || '';
            var side = st.includes('right:') ? 1 : 0;
            window.__wr_chars.push({
                t: text,
                x: Math.round(x * 10) / 10,
                y: Math.round(y * 10) / 10,
                f: String(this.font),
                side: side,
                seq: window.__wr_seq
            });
        }
        return origFill.apply(this, arguments);
    };
    window.__wr_drain = function() {
        var out = window.__wr_chars;
        window.__wr_chars = [];
        return out;
    };
    window.__wr_count = function() { return window.__wr_chars.length; };
})();
"""

IMGS_JS = """
() => {
    const H = window.innerHeight, W = window.innerWidth, out = [];
    document.querySelectorAll('img[class*="wr_readerImage"]').forEach(i => {
        const src = i.src || i.getAttribute('data-src') || '';
        if (!src.includes('res.weread.qq.com/wrepub')) return;
        const r = i.getBoundingClientRect();
        if (r.width > 40 && r.height > 40 && r.bottom > 0 && r.top < H &&
            r.right > 0 && r.left < W &&
            getComputedStyle(i).visibility !== 'hidden' && getComputedStyle(i).display !== 'none')
            out.push({src, top: Math.round(r.top), left: Math.round(r.left)});
    });
    return out;
}
"""

MEASURE_RE = re.compile(r'^[a-zA-Z`~!@#$%^&*()\-_=+\[\]{}|;:\',<.>/?\\"\s]{2,}$')
SENT_END_RE = re.compile(r"[。！？；：…][”」）】》]*$")


def norm(s):
    return re.sub(r"[\s​﻿　\-_:：,，、.·]+", "", s or "")


def size_of(f):
    m = re.search(r"([\d.]+)px", f or "")
    return float(m.group(1)) if m else 0.0


def cls_of(f):
    s = size_of(f)
    if s >= 24.5:
        return "title"
    if s >= 21.0:
        return "yw"
    if "bold" in (f or ""):
        return "fang"
    return "js"


def split_pages(chars):
    """按 (seq, side) 以及 y 坐标回跳切分为独立单页"""
    real = []
    for c in chars:
        t = c["t"].replace("​", "").replace("﻿", "")
        if not t.strip():
            continue
        if t.startswith("abcdefg"):
            continue
        if len(t) > 1 and MEASURE_RE.match(t):
            continue
        c = dict(c)
        c["t"] = t
        real.append(c)

    if not real:
        return []

    pages = []
    cur = []
    cur_key = None
    last_y = -1.0

    for c in real:
        key = (c["seq"], c["side"])
        if cur_key is None:
            cur_key = key
            cur = [c]
            last_y = c["y"]
            continue
        if key != cur_key or (len(c["t"]) == 1 and last_y > 250 and c["y"] < last_y - 80):
            if cur:
                pages.append((cur_key[1], cur))
            cur = [c]
            cur_key = key
            last_y = c["y"]
        else:
            cur.append(c)
            if len(c["t"]) == 1:
                last_y = c["y"]
    if cur:
        pages.append((cur_key[1], cur))
    return pages


def page_lines(chars):
    rows = {}
    for c in chars:
        rows.setdefault(round(c["y"] / 3) * 3, []).append(c)
    raw_lines = []
    for yk in sorted(rows):
        row = sorted(rows[yk], key=lambda c: c["x"])
        text = "".join(c["t"] for c in row).strip()
        if not text:
            continue
        f = collections.Counter(c["f"] for c in row).most_common(1)[0][0]
        raw_lines.append({"y": yk, "text": text, "cls": cls_of(f), "size": round(size_of(f), 1)})

    out = []
    for ln in raw_lines:
        if (out and out[-1]["cls"] == "title" and ln["cls"] == "title"
                and out[-1]["size"] == ln["size"] and ln["y"] - out[-1]["y"] < 55):
            out[-1]["text"] += ln["text"]
            out[-1]["y"] = ln["y"]
        else:
            out.append(ln)
    return out


def _match_title_text(text, cat):
    """单条目录标题匹配（忽略空白/标点差异）。返回剥离后的剩余正文；不匹配返回 None。"""
    clean_text = norm(text)
    clean_cat = norm(cat)
    if not clean_text or not clean_cat or len(clean_cat) < 2:
        return None

    # 1. 独立一行完全匹配目录标题
    if clean_text == clean_cat:
        return ""

    # 2. 标题在行首，且整行长度很短（<= 目录长度 + 4 个字）
    if clean_text.startswith(clean_cat) and len(clean_text) <= len(clean_cat) + 4:
        return ""

    # 3. 标题与首段正文粘连（例如：'第2章 般若品第二次日，韦使君请益...'）
    if clean_text.startswith(clean_cat) and len(clean_cat) >= 3:
        c_idx = t_idx = 0
        while c_idx < len(cat) and t_idx < len(text):
            if norm(text[t_idx]) == norm(cat[c_idx]):
                c_idx += 1
            t_idx += 1
        if c_idx >= len(cat) * 0.8:
            return text[t_idx:].lstrip("：:、.· 　")

    return None


def match_catalog_pos(text, catalog, cat_pos):
    """
    按阅读顺序匹配目录标题（从当前位置起，允许跳过至多 2 个空小节）。
    返回 (匹配标题, 剩余正文, 新目录位置)；无匹配返回 (None, "", cat_pos)。
    """
    if cat_pos >= len(catalog):
        return None, "", cat_pos
    for delta in (0, 1, 2):
        idx = cat_pos + delta
        if idx >= len(catalog):
            break
        remainder = _match_title_text(text, catalog[idx])
        if remainder is not None:
            return catalog[idx], remainder, idx + 1
    return None, "", cat_pos


def match_catalog_line(text, cls, catalog, cat_pos):
    """
    章节标题匹配（实时抓取与离线重建共用）：
    1. 优先位置匹配：从当前位置起，允许跳过至多 2 个空小节（处理标题与段首粘连的普通行）；
    2. 位置匹配失败且该行是大字号标题时，前向扫描整个目录兜底——
       修复扉页/版权信息/自序等不以画布文本渲染导致目录位置停滞的问题。
    返回 (匹配标题, 剩余正文, 新目录位置)；无匹配返回 (None, "", cat_pos)。
    """
    mtitle, remainder, new_pos = match_catalog_pos(text, catalog, cat_pos)
    if mtitle is not None:
        return mtitle, remainder, new_pos
    if cls == "title":
        clean = norm(text)
        if clean and len(clean) >= 2:
            for idx in range(cat_pos + 1, len(catalog)):
                r = _match_title_text(text, catalog[idx])
                if r is not None:
                    return catalog[idx], r, idx + 1
    return None, "", cat_pos


def render_md(title, blocks, ch_idx):
    out = ["# %s\n" % title]
    buf, buf_cls = [], None
    img_n = 0

    def flush():
        nonlocal buf, buf_cls
        if not buf:
            buf_cls = None
            return
        if buf_cls == "js":
            merged = []
            for line in buf:
                if merged and merged[-1] and not SENT_END_RE.search(merged[-1]):
                    merged[-1] += line
                else:
                    merged.append(line)
            for m in merged:
                if m.strip():
                    out.append(m.strip())
                    out.append("")
        elif buf_cls == "yw":
            joined = "".join(buf)
            out.append('<p class="yw">%s</p>' % joined.strip())
            out.append("")
        elif buf_cls == "fang":
            for line in buf:
                if line.strip():
                    out.append('<p class="fang">%s</p>' % line.strip())
                    out.append("")
        buf, buf_cls = [], None

    imgs = []
    for b in blocks:
        if b["type"] == "img":
            flush()
            img_n += 1
            ext = "jpg"
            m = re.search(r"\.(jpg|jpeg|png|gif|webp)", b["src"].lower())
            if m:
                ext = m.group(1).replace("jpeg", "jpg")
            name = "ch%04d_img%02d.%s" % (ch_idx, img_n, ext)
            out.append("![图](images/%s)\n" % name)
            imgs.append({"src": b["src"], "name": name})
            continue
        if b.get("cls") == "title":
            flush()
            sub = b["text"].strip()
            if sub and norm(sub) != norm(title):
                out.append("## %s\n" % sub)
            continue
        if b["cls"] != buf_cls:
            flush()
            buf_cls = b["cls"]
        buf.append(b["text"])
    flush()
    return "\n".join(out).rstrip() + "\n", imgs


async def wait_stable_chars(page, timeout=6.0):
    t0 = time.time()
    last_n = 0
    stable_ticks = 0
    while time.time() - t0 < timeout:
        try:
            n = await page.evaluate("() => window.__wr_count()")
        except Exception:
            n = 0
        if n > 0:
            if n == last_n:
                stable_ticks += 1
                if stable_ticks >= 2:
                    return n
            else:
                last_n = n
                stable_ticks = 0
        await asyncio.sleep(0.18)
    return last_n


async def check_cookies_login(ctx):
    """基于真实鉴权凭证检查登录态，彻底避免访客目录误判"""
    try:
        cookies = await ctx.cookies()
        names = {c["name"] for c in cookies}
        return "wr_vid" in names and ("wr_skey" in names or "wr_rt" in names)
    except Exception:
        return False


async def launch_browser_context(p, user_data_dir, headless=False):
    """自动检测可用浏览器：优先系统自带 Edge/Chrome，免下载 200MB 浏览器"""
    custom_ch = os.environ.get("PLAYWRIGHT_CHANNEL")
    candidates = []
    if custom_ch:
        candidates.append({"name": f"自定义 {custom_ch}", "channel": custom_ch})
    candidates.extend([
        {"name": "系统 Microsoft Edge", "channel": "msedge"},
        {"name": "系统 Google Chrome", "channel": "chrome"},
        {"name": "Playwright Chromium", "channel": None},
    ])
    last_err = None
    for c in candidates:
        try:
            kwargs = {
                "headless": headless,
                "viewport": {"width": 1200, "height": 900},
                "args": ["--disable-blink-features=AutomationControlled"]
            }
            if c["channel"]:
                kwargs["channel"] = c["channel"]
            ctx = await p.chromium.launch_persistent_context(user_data_dir, **kwargs)
            return ctx
        except Exception as e:
            last_err = e
            err_msg = str(e)
            if "ProcessSingleton" in err_msg or "Lock file" in err_msg:
                raise RuntimeError("检测到浏览器配置目录被占用！请先关闭其他正在运行的微信读书导出窗口或浏览器后台进程后重试。")
            continue
    raise RuntimeError(f"未能启动浏览器 ({last_err})。请安装 Google Chrome 或 Microsoft Edge，或执行: playwright install chromium")


async def verify_arrived(page, target_title):
    """导航后确认已到达目标章节：画布文本命中标题即视为到达（只预览不消耗缓冲）"""
    target_norm = norm(target_title)
    if not target_norm:
        return True
    for _ in range(3):
        await wait_stable_chars(page, timeout=5.0)
        try:
            n = await page.evaluate("() => window.__wr_count()")
            if n:
                chars = await page.evaluate("() => window.__wr_chars.slice(0, 600)")
                text = norm("".join(c.get("t", "") for c in chars))
                if target_norm in text:
                    return True
        except Exception:
            pass
        await asyncio.sleep(2.0)
    return False


async def navigate_to_catalog_item(page, nav_idx, retries=4):
    """
    健壮导航至目录第 nav_idx 项：
    目录面板可能已收起（元素仍在 DOM 中但不可见）、阅读器 UI 可能因点击中央
    而被隐藏、虚拟列表可能重渲染——每次失败都会恢复 UI、按可见性重新打开面板、
    重新定位元素再点击，最多重试 retries 次。返回是否成功。
    """
    for attempt in range(retries):
        try:
            # 按"可见性"判断面板是否打开（count() 无法区分收起状态）
            try:
                panel_open = await page.locator(".readerCatalog_list_item").first.is_visible()
            except Exception:
                panel_open = False
            if not panel_open:
                # 点击中央恢复可能被隐藏的阅读器 UI，再打开目录面板
                try:
                    await page.mouse.click(600, 450)
                except Exception:
                    pass
                try:
                    await page.click("button.readerControls_item.catalog", timeout=5000)
                except Exception:
                    await page.keyboard.press("Escape")
                    await page.mouse.click(600, 450)
                await asyncio.sleep(1.5)
            it = page.locator(".readerCatalog_list_item").nth(nav_idx)
            await it.wait_for(state="visible", timeout=5000)
            await it.scroll_into_view_if_needed(timeout=5000)
            await asyncio.sleep(0.3)
            await it.click(timeout=5000)
            return True
        except Exception as e:
            print(f"  ⚠️ 目录导航失败 ({attempt + 1}/{retries}): {str(e)[:80]}", flush=True)
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            try:
                await page.mouse.click(600, 450)
            except Exception:
                pass
            await asyncio.sleep(1.5)
    return False


async def main(book_id_or_url, force=False, profile=DEFAULT_PROFILE):
    book_id = extract_book_id(book_id_or_url)
    if not book_id:
        print("❌ 错误: 未能解析出有效的微信读书书籍 ID 或链接")
        return False

    profile = resolve_profile(profile)
    cfg = SPEED_PROFILES[profile]

    book_dir = os.path.join(BASE, "output", book_id + "_v2")
    md_dir = os.path.join(book_dir, "chapters")
    raw_dir = os.path.join(book_dir, "raw")
    img_dir = os.path.join(book_dir, "images")

    os.makedirs(md_dir, exist_ok=True)
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(img_dir, exist_ok=True)
    os.makedirs(USER_DATA_DIR, exist_ok=True)

    state = get_crawl_state(book_dir)
    existing_mds = sorted([f for f in os.listdir(md_dir) if f.endswith(".md")])
    raw_pages = sorted([f for f in os.listdir(raw_dir) if f.startswith("page_") and f.endswith(".json")])

    # 1. 自愈检查：若存在 raw 页面落盘但 chapters 为空，自动重构
    if not existing_mds and raw_pages:
        print(f"🔄 检测到存在历史逐页落盘数据 ({len(raw_pages)} 页)，正在恢复章节数据...")
        rebuild_chapters_from_raw(book_id)
        existing_mds = sorted([f for f in os.listdir(md_dir) if f.endswith(".md")])
        state = get_crawl_state(book_dir)

    # 2. 目录与已完成章节
    catalog = load_catalog(book_dir)
    if not catalog and existing_mds:
        catalog = catalog_from_chapters(md_dir)
        if catalog:
            with open(os.path.join(book_dir, "_catalog.json"), "w", encoding="utf-8") as f:
                json.dump(catalog, f, ensure_ascii=False, indent=2)
    completed_titles = read_completed_titles(raw_dir)

    # 3. 完成度判定（以目录位置为准，杜绝"假完成"误判）
    resume_cat_idx = 0
    if completed_titles and catalog:
        done_norms = {norm(t) for t in completed_titles}
        for ci, cat in enumerate(catalog):
            if norm(cat) in done_norms:
                resume_cat_idx = ci + 1

    if existing_mds and catalog and not force and resume_cat_idx >= len(catalog):
        print(f"📖 检测到书籍《{book_id}》此前已完整抓取（目录 {len(catalog)} 项全部覆盖，共 {len(existing_mds)} 章）。")
        print("⏩ 自动跳过重复抓取，直接复用已有数据进入后续导出流程。")
        print("💡 如需强制重新完整抓取，可在命令行传入 --force 参数。\n")
        download_all_images(raw_dir, img_dir)
        return True

    if state.get("completed") and existing_mds and not force:
        print(f"⚠️ 状态文件曾标记为已完成，但目录核对发现仅覆盖 {resume_cat_idx}/{len(catalog)} 项！")
        print(f"   （疑似此前被风控空页误判结束）自动从《{catalog[resume_cat_idx] if resume_cat_idx < len(catalog) else catalog[-1]}》续抓补齐...\n")

    print(f"📖 目标书籍 ID: {book_id} | 抓取节奏: {profile} 档")

    async with async_playwright() as p:
        ctx = await launch_browser_context(p, USER_DATA_DIR, headless=False)
        page = await ctx.new_page()
        await page.add_init_script(HOOK)

        target_url = f"https://weread.qq.com/web/reader/{book_id}"
        print(f"🌐 正在打开书籍阅读页: {target_url} ...", flush=True)
        await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)

        # 登录判定：严格使用 Cookie 凭证
        if not await check_cookies_login(ctx):
            print("\n" + "="*55)
            print("👉 检测到未登录或登录已过期，请在浏览器中使用【微信扫码】（限时 300 秒）...")
            print("="*55 + "\n", flush=True)
            for sec in range(1, 151):
                await asyncio.sleep(2)
                if await check_cookies_login(ctx):
                    print("✅ 登录成功，继续执行抓取...", flush=True)
                    break
            else:
                print("❌ 扫码登录超时（300秒），导出终止。请重新运行并在弹出的窗口中扫码！", flush=True)
                await ctx.close()
                return False

        # 确保处于双栏水平翻页模式
        if await page.locator("button.readerControls_item.isNormalReader").count() > 0:
            print("🔄 切换至双栏翻页模式...", flush=True)
            await page.click("button.readerControls_item.isNormalReader")
            await asyncio.sleep(3)

        # 抓取完整的书籍元数据与原版高清封面
        book_meta = await page.evaluate("""() => {
            let info = (window.__INITIAL_STATE__ && window.__INITIAL_STATE__.reader && window.__INITIAL_STATE__.reader.bookInfo) || {};
            return {
                title: info.title || document.title.replace(' - 微信读书', '').trim(),
                author: info.author || '',
                cover: info.cover || '',
                intro: info.intro || '',
                publisher: info.publisher || ''
            };
        }""")
        book_meta["book_id"] = book_id
        meta_file = os.path.join(book_dir, "meta.json")
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(book_meta, f, ensure_ascii=False, indent=2)

        print(f"📚 书名: {book_meta.get('title')}")
        if book_meta.get("author"):
            print(f"✍️ 作者: {book_meta.get('author')}")

        cover_url = book_meta.get("cover")
        if cover_url:
            cover_path = os.path.join(book_dir, "cover.jpg")
            try:
                req = urllib.request.Request(cover_url, headers={"Referer": "https://weread.qq.com/", "User-Agent": "Mozilla/5.0"})
                raw = urllib.request.urlopen(req, timeout=15).read()
                if len(raw) > 500:
                    with open(cover_path, "wb") as f:
                        f.write(raw)
                    print("🖼️ 原版高清封面下载完成")
            except Exception as e:
                print(f"⚠️ 封面下载跳过: {e}")

        # 读取完整目录列表（带重试，目录是章节切分与完结判定的基石）
        catalog = []
        items = None
        for attempt in range(5):
            try:
                await page.click("button.readerControls_item.catalog", timeout=8000)
            except Exception:
                await page.keyboard.press("Escape")
            await asyncio.sleep(1.5)
            items = page.locator(".readerCatalog_list_item")
            n = await items.count()
            if n > 0:
                try:
                    for i in range(n):
                        txt = (await items.nth(i).inner_text()).strip().replace("\n", " ")
                        txt = re.sub(r"\s*当前读到.*$", "", txt).strip()
                        if txt:
                            catalog.append(txt)
                    break
                except Exception:
                    catalog = []  # 读取中途列表重渲染，整轮重试
            await asyncio.sleep(3.0)

        if not catalog:
            print("❌ 无法读取书籍目录，无法保证章节切分与完结判定，任务终止。")
            await ctx.close()
            return False

        with open(os.path.join(book_dir, "_catalog.json"), "w", encoding="utf-8") as f:
            json.dump(catalog, f, ensure_ascii=False, indent=2)
        print(f"📑 目录共 {len(catalog)} 项: {catalog[:3]} ... {catalog[-2:] if len(catalog) > 3 else ''}", flush=True)

        # 断点续传：以刚读到的目录重算完成位置
        completed_titles = read_completed_titles(raw_dir)
        resume_cat_idx = 0
        if completed_titles and catalog:
            done_norms = {norm(t) for t in completed_titles}
            for ci, cat in enumerate(catalog):
                if norm(cat) in done_norms:
                    resume_cat_idx = ci + 1
        if resume_cat_idx >= len(catalog):
            print(f"🎉 目录所有 {len(catalog)} 个章节此前均已全部抓取完成！")
            save_crawl_state(book_dir, {
                "book_id": book_id,
                "completed": True,
                "saved_chapter_count": len(completed_titles),
                "total_words": state.get("total_words", 0),
                "profile": profile,
            })
            await ctx.close()
            download_all_images(raw_dir, img_dir)
            return True

        nav_idx = resume_cat_idx if resume_cat_idx > 0 else 0

        if resume_cat_idx > 0:
            print("\n" + "="*55)
            print(f"🔄 检测到未完成进度，执行【真·断点续传】：")
            print(f"   - 历史已完成: {len(completed_titles)} 章 (目录覆盖 {resume_cat_idx}/{len(catalog)} 项)")
            print(f"   - 续跑跳转点: 《{catalog[nav_idx]}》")
            print("="*55 + "\n", flush=True)
            drop_partial_chapter_pages(raw_dir, catalog[nav_idx])

        # 平稳导航至目标章节（首章或续跑断点），面板异常时自动重开重试
        nav_ok = await navigate_to_catalog_item(page, nav_idx)
        if not nav_ok:
            print(f"❌ 多次尝试后仍无法通过目录跳转到《{catalog[nav_idx]}》，任务终止。请检查浏览器窗口状态后重试。")
            await ctx.close()
            return False
        await asyncio.sleep(1.0)
        try:
            if await page.locator(".readerCatalog_list_item").first.is_visible():
                await page.click("button.readerControls_item.catalog", timeout=2000)
        except Exception:
            await page.keyboard.press("Escape")
        await asyncio.sleep(1.0)

        # 清空导航期间产生的残留缓冲，再等待目标章渲染
        await page.evaluate("() => window.__wr_drain()")
        arrived = await verify_arrived(page, catalog[nav_idx])
        if not arrived:
            print(f"⚠️ 未能确认已跳转到《{catalog[nav_idx]}》，继续抓取（目录匹配会自动纠正）...")

        # 状态初始化：以目录位置为唯一真相
        cur_title = catalog[nav_idx]
        cat_pos = nav_idx
        seen_title_norms = {norm(t) for t in completed_titles}
        seen_title_norms.add(norm(cur_title))
        saved_idx = len(completed_titles)
        buf = []
        stale = 0
        rescue_left = RESCUE_TRIES
        last_cat_seen = cat_pos >= len(catalog)
        seen_page_sigs = set()
        seen_imgs = set()

        total_words_accum = state.get("total_words", 0) if resume_cat_idx > 0 else 0
        t_start = time.time()

        def flush_cur(step):
            nonlocal saved_idx
            if not buf:
                return
            saved_idx += 1
            body, im_list = render_md(cur_title, buf, saved_idx)
            txt_len = len(re.sub(r"<[^>]+>", "", body))
            with open(os.path.join(md_dir, "%04d.md" % saved_idx), "w", encoding="utf-8") as f:
                f.write(body)
            with open(os.path.join(raw_dir, "chap_%04d.json" % saved_idx), "w", encoding="utf-8") as f:
                json.dump({"title": cur_title, "images": im_list, "text_len": txt_len}, f, ensure_ascii=False)
            print(f"\n  💾 [章节实时落盘] [{saved_idx:02d}] 《{cur_title}》已保存至 chapters/{saved_idx:04d}.md ({txt_len} 字)", flush=True)
            save_crawl_state(book_dir, {
                "book_id": book_id,
                "completed": False,
                "last_step": step,
                "saved_chapter_count": saved_idx,
                "last_title": cur_title,
                "total_words": total_words_accum,
            })

        # 🌟 卷首回补：目录第一项（如"扉页"）之前可能还有封面/自序前半等页面。
        # 向左翻页直到连续空页（封面），按阅读顺序落盘为最前面的 page 文件，
        # 补完后重新导航回目录第一项，再从那里开始正常向前抓取。
        if nav_idx == 0:
            print(f"\n🔙 回补卷首: 自《{catalog[0]}》向前翻页，补齐开卷内容...", flush=True)
            captures = []   # 按捕获顺序（从扉页向左）
            empties = 0
            for back_step in range(40):
                try:
                    await page.mouse.click(600, 450)
                    await page.keyboard.press("ArrowLeft")
                except Exception:
                    break
                await asyncio.sleep(random.uniform(3.0, 5.0))
                ok = await wait_stable_chars(page, timeout=2.5)
                chars = await page.evaluate("() => window.__wr_drain()") if ok else []
                pages = split_pages(chars)
                if not any(page_lines(pc) for _, pc in pages):
                    empties += 1
                    if empties >= 2:
                        print(f"  🔙 已到达封面（连续 {empties} 页无文字），回补结束", flush=True)
                        break
                    continue
                empties = 0
                captures.append(pages)
            # 按阅读顺序（与向前抓取相反的捕获顺序）处理并落盘
            front_records = []
            for pages in reversed(captures):
                page_parts = []
                page_blocks = []
                step_words = 0
                for side, pchars in pages:
                    lines = page_lines(pchars)
                    if not lines:
                        continue
                    sig = norm("".join(ln["text"] for ln in lines))
                    if len(sig) > 8 and sig in seen_page_sigs:
                        continue
                    if len(sig) > 8:
                        seen_page_sigs.add(sig)
                    for ln in lines:
                        txt = ln["text"].strip()
                        step_words += len(txt)
                        mtitle, remainder, new_pos = match_catalog_line(txt, ln["cls"], catalog, cat_pos)
                        if mtitle is not None:
                            if norm(mtitle) != norm(cur_title):
                                if page_blocks:
                                    page_parts.append((cur_title, page_blocks))
                                    page_blocks = []
                                flush_cur(0)
                                buf = []
                                cur_title = mtitle
                                cat_pos = new_pos
                                seen_title_norms.add(norm(cur_title))
                                print(f"  🔙 [回补] 识别新章节: 《{cur_title}》", flush=True)
                            else:
                                cat_pos = max(cat_pos, new_pos)
                            if cat_pos >= len(catalog):
                                last_cat_seen = True
                            if remainder:
                                rem_blk = {"type": "line", "cls": "js", "text": remainder}
                                buf.append(rem_blk)
                                page_blocks.append(rem_blk)
                            continue
                        if ln["cls"] == "title" and norm(txt) == norm(cur_title):
                            continue
                        if ln["cls"] == "title" and len(txt) <= 40:
                            blk = {"type": "line", "cls": "title", "text": txt}
                        else:
                            cls = "js" if ln["cls"] == "title" else ln["cls"]
                            blk = {"type": "line", "cls": cls, "text": txt}
                        if buf and buf[-1]["type"] == "line" and buf[-1]["text"] == txt and buf[-1]["cls"] == blk["cls"]:
                            continue
                        buf.append(blk)
                        page_blocks.append(blk)
                total_words_accum += step_words
                page_parts.append((cur_title, page_blocks))
                for pch, pblks in page_parts:
                    if pblks:
                        front_records.append((pch, pblks))
            for idx, (pch, pblks) in enumerate(front_records):
                with open(os.path.join(raw_dir, "page_%04d.json" % idx), "w", encoding="utf-8") as pf:
                    json.dump({
                        "step": idx,
                        "chapter": pch,
                        "words": sum(len(b["text"]) for b in pblks if b.get("type") == "line"),
                        "blocks": pblks,
                        "lines": [b["text"] for b in pblks if b.get("type") == "line"],
                    }, pf, ensure_ascii=False)
            if front_records:
                print(f"  🔙 卷首回补完成: {len(front_records)} 页已落盘（页码 0~{len(front_records) - 1}）", flush=True)
            else:
                print("  🔙 卷首无额外页面，无需回补", flush=True)
            # 重新导航回目录第一项，从那里开始正常向前抓取（避免重复遍历已回补页面）。
            # 若导航失败不终止：此时阅读器就在封面/扉页附近，直接从当前位置向前抓是安全的
            # （已回补页面的签名已登记，重走会被去重；章节由目录前向扫描自动纠正）。
            nav_ok = await navigate_to_catalog_item(page, nav_idx)
            if not nav_ok:
                print("⚠️ 回补后重新导航失败，直接从当前位置继续向前抓取（目录匹配会自动纠正）。", flush=True)
            await asyncio.sleep(1.0)
            try:
                if await page.locator(".readerCatalog_list_item").first.is_visible():
                    await page.click("button.readerControls_item.catalog", timeout=2000)
            except Exception:
                await page.keyboard.press("Escape")
            await page.evaluate("() => window.__wr_drain()")
            await asyncio.sleep(1.0)

        next_step = max_page_step(raw_dir)
        long_pause_at = next_long_pause_at(next_step)

        print(f"\n🚀 开始遍历抓取页面 (从第 {next_step} 页开始，档位 {profile}: 每页 {cfg['page_delay'][0]:.0f}~{cfg['page_delay'][1]:.0f} 秒)...", flush=True)

        reached_end = False
        step = next_step
        first_page = True
        try:
            for step in range(next_step, next_step + MAX_STEPS):
                if not first_page:
                    await page.mouse.click(600, 450)
                    await page.keyboard.press("ArrowRight")
                    # 空页退避：连续空页越多，翻页间隔越长（自动降速保护）
                    backoff = 1.0 + 0.5 * min(stale, 6)
                    delay = random.uniform(*cfg["page_delay"]) * backoff
                    await asyncio.sleep(delay)
                    if step >= long_pause_at:
                        pause = random.uniform(*cfg["pause_range"])
                        print(f"  ⏸ 已抓取 {step} 页，触发温和长停顿 {pause:.0f} 秒...", flush=True)
                        await asyncio.sleep(pause)
                        long_pause_at = next_long_pause_at(step)
                first_page = False

                ok = await wait_stable_chars(page, timeout=5.0 if step == next_step else 2.2)
                chars = await page.evaluate("() => window.__wr_drain()") if ok else []
                imgs = await page.evaluate(IMGS_JS)

                pages = split_pages(chars)
                left_imgs = [im for im in imgs if im["left"] < 600]
                right_imgs = [im for im in imgs if im["left"] >= 600]

                got_any = False
                step_words = 0
                page_blocks = []
                page_parts = []  # 一页内出现章节切换时，按章节拆分的多个部分

                for side, pchars in pages:
                    lines = page_lines(pchars)
                    if not lines:
                        continue
                    sig = norm("".join(ln["text"] for ln in lines))
                    if len(sig) > 8 and sig in seen_page_sigs:
                        continue
                    if len(sig) > 8:
                        seen_page_sigs.add(sig)
                    got_any = True

                    for ln in lines:
                        txt = ln["text"].strip()
                        step_words += len(txt)

                        mtitle, remainder, new_pos = match_catalog_line(txt, ln["cls"], catalog, cat_pos)
                        if mtitle is not None:
                            if norm(mtitle) != norm(cur_title):
                                # 章节切换：先保存当前页已捕获的上一章内容
                                if page_blocks:
                                    page_parts.append((cur_title, page_blocks))
                                    page_blocks = []
                                flush_cur(step)
                                buf = []
                                cur_title = mtitle
                                cat_pos = new_pos
                                seen_title_norms.add(norm(cur_title))
                                print(f"\n  📑 [第 {step:03d} 页] 识别新章节: 《{cur_title}》", flush=True)
                            else:
                                cat_pos = max(cat_pos, new_pos)
                            if cat_pos >= len(catalog):
                                last_cat_seen = True
                            if remainder:
                                rem_blk = {"type": "line", "cls": "js", "text": remainder}
                                buf.append(rem_blk)
                                page_blocks.append(rem_blk)
                            continue

                        # 当前章标题行本身（跨页重复渲染等）→ 跳过
                        if ln["cls"] == "title" and norm(txt) == norm(cur_title):
                            continue

                        # 非目录大字号标题 → 章内 ## 子标题
                        if ln["cls"] == "title" and len(txt) <= 40:
                            blk = {"type": "line", "cls": "title", "text": txt}
                        else:
                            cls = "js" if ln["cls"] == "title" else ln["cls"]
                            blk = {"type": "line", "cls": cls, "text": txt}

                        if buf and buf[-1]["type"] == "line" and buf[-1]["text"] == txt and buf[-1]["cls"] == blk["cls"]:
                            continue

                        buf.append(blk)
                        page_blocks.append(blk)

                    side_imgs = left_imgs if side == 0 else right_imgs
                    for im in side_imgs:
                        if im["src"] not in seen_imgs:
                            seen_imgs.add(im["src"])
                            img_blk = {"type": "img", "src": im["src"]}
                            buf.append(img_blk)
                            page_blocks.append(img_blk)
                            got_any = True

                if not pages and imgs:
                    for im in imgs:
                        if im["src"] not in seen_imgs:
                            seen_imgs.add(im["src"])
                            img_blk = {"type": "img", "src": im["src"]}
                            buf.append(img_blk)
                            page_blocks.append(img_blk)
                            got_any = True

                total_words_accum += step_words
                page_parts.append((cur_title, page_blocks))

                # 逐页完整结构化落盘（真·断点保护；页内跨章时拆分为多个部分）
                if got_any:
                    for pi, (pch, pblks) in enumerate(page_parts):
                        if not pblks:
                            continue
                        name = f"page_{step:04d}.json" if pi == 0 else f"page_{step:04d}{chr(96 + pi)}.json"
                        with open(os.path.join(raw_dir, name), "w", encoding="utf-8") as pf:
                            json.dump({
                                "step": step,
                                "chapter": pch,
                                "words": step_words,
                                "blocks": pblks,
                                "lines": [b["text"] for b in pblks if b.get("type") == "line"],
                            }, pf, ensure_ascii=False)

                # 实时可见的进度输出
                elapsed_sec = int(time.time() - t_start)
                elapsed_str = f"{elapsed_sec // 60}分{elapsed_sec % 60}秒"
                if got_any:
                    stale = 0
                    rescue_left = RESCUE_TRIES
                    print(f"  [进度 第 {step:03d} 页] 本页 +{step_words:3d} 字 | 累计 {total_words_accum:6d} 字 | 所在: 《{cur_title[:12]}》 | 目录覆盖 {cat_pos}/{len(catalog)} | 已用时 {elapsed_str}", flush=True)
                else:
                    stale += 1
                    if last_cat_seen and stale >= END_STALE_LIMIT:
                        reached_end = True
                        print("  🏁 目录已全部覆盖且末页连续无新内容，全书抓取完成", flush=True)
                        break
                    if stale >= STALE_LIMIT:
                        if rescue_left <= 0:
                            print("\n" + "="*55)
                            print(f"🆘 连续 {stale} 页未获取到新内容，且救援重试 {RESCUE_TRIES} 次失败！")
                            print("   可能原因: 触发风控 / 网络波动 / 阅读器停止翻页")
                            print(f"   当前进度: 目录覆盖 {cat_pos}/{len(catalog)} 项，已落盘 {saved_idx} 章")
                            print("   ✅ 进度已保存（未标记完成）。建议等待 10~30 分钟风控冷静期后，")
                            print("      重新运行同一条命令即可自动断点续传，无需重头开始。")
                            print("="*55 + "\n", flush=True)
                            if cur_title and buf:
                                flush_cur(step)
                            save_crawl_state(book_dir, {
                                "book_id": book_id,
                                "completed": False,
                                "last_step": step,
                                "saved_chapter_count": saved_idx,
                                "last_title": cur_title,
                                "total_words": total_words_accum,
                            })
                            await ctx.close()
                            download_all_images(raw_dir, img_dir)
                            return False
                        rescue_left -= 1
                        wait_sec = 20 + (RESCUE_TRIES - rescue_left) * 15
                        print(f"  🆘 连续 {stale} 页无新内容，第 {RESCUE_TRIES - rescue_left}/{RESCUE_TRIES} 次救援: 静候 {wait_sec} 秒后重试...", flush=True)
                        try:
                            await page.mouse.click(600, 450)
                        except Exception:
                            pass
                        await asyncio.sleep(wait_sec)
                    else:
                        print(f"  [进度 第 {step:03d} 页] ⚠️ 无新内容 (连续 {stale} 页) | 所在: 《{cur_title[:12]}》 | 目录覆盖 {cat_pos}/{len(catalog)} | 已用时 {elapsed_str}", flush=True)
        except KeyboardInterrupt:
            print("\n⚠️ 检测到手动中断 (Ctrl+C)，正在保存进度...", flush=True)
            if cur_title and buf:
                flush_cur(step)
            save_crawl_state(book_dir, {
                "book_id": book_id,
                "completed": False,
                "last_step": step,
                "saved_chapter_count": saved_idx,
                "last_title": cur_title,
                "total_words": total_words_accum,
            })
            await ctx.close()
            download_all_images(raw_dir, img_dir)
            return False
        except Exception as e:
            print(f"\n❌ 抓取过程中出现未预期错误: {str(e)[:200]}", flush=True)
            print("   ✅ 已保存断点进度（未标记完成）。重新运行同一条命令即可自动续传。\n")
            if cur_title and buf:
                flush_cur(step)
            save_crawl_state(book_dir, {
                "book_id": book_id,
                "completed": False,
                "last_step": step,
                "saved_chapter_count": saved_idx,
                "last_title": cur_title,
                "total_words": total_words_accum,
            })
            try:
                await ctx.close()
            except Exception:
                pass
            download_all_images(raw_dir, img_dir)
            return False

        if not reached_end:
            print(f"\n❌ 已遍历 {MAX_STEPS} 页仍未到达末章，判定抓取未完成（异常）。")
            save_crawl_state(book_dir, {
                "book_id": book_id,
                "completed": False,
                "last_step": step,
                "saved_chapter_count": saved_idx,
                "last_title": cur_title,
                "total_words": total_words_accum,
            })
            await ctx.close()
            download_all_images(raw_dir, img_dir)
            return False

        # 收尾：冲刷最后章节
        if cur_title and buf:
            flush_cur(step)

        # 以 raw 逐页数据为准统一重建全部章节（去重、排序、子标题还原）
        print("\n🔨 以逐页原始数据统一重建全部章节（去重、排序、子标题还原）...", flush=True)
        rebuilt = rebuild_chapters_from_raw(book_id)
        print(f"✅ 重建完成: {rebuilt} 个章节文件已生成。", flush=True)

        save_crawl_state(book_dir, {
            "book_id": book_id,
            "completed": True,
            "last_step": step,
            "saved_chapter_count": rebuilt,
            "last_title": cur_title,
            "total_words": total_words_accum,
            "profile": profile,
        })

        print(f"\n" + "="*50)
        print(f"🎉 抓取成功！全书共 {rebuilt} 个章节，累计 {total_words_accum} 字，目录覆盖 {len(catalog)}/{len(catalog)} 项")
        print("="*50)
        await ctx.close()

    # 统一补齐配图下载
    download_all_images(raw_dir, img_dir)
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python crawl_weread.py <微信读书链接或ID> [--force] [--profile fast|normal|gentle] [--rebuild]")
        sys.exit(1)

    force_flag = "--force" in sys.argv or "-f" in sys.argv
    rebuild_flag = "--rebuild" in sys.argv
    profile = DEFAULT_PROFILE
    if "--profile" in sys.argv:
        pi = sys.argv.index("--profile")
        if pi + 1 < len(sys.argv):
            profile = sys.argv[pi + 1]

    bid = [a for a in sys.argv[1:] if not a.startswith("-")][0]
    extracted = extract_book_id(bid)

    if rebuild_flag:
        cnt = rebuild_chapters_from_raw(extracted)
        print(f"🎉 已成功从 raw/ 页面落盘数据恢复 {cnt} 个章节！")
    else:
        asyncio.run(main(extracted, force=force_flag, profile=profile))
