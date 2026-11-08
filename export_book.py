#!/usr/bin/env python3
"""WeRead Book Exporter (export_book.py)

将已抓取的书籍章节导出为 Obsidian 知识库友好的 Markdown 结构：
1. 每章一个 .md 文件（含 YAML frontmatter，纯 Markdown 语法，无 HTML 排版标签）
2. 一本书一个文件夹：章节文件 + 「_目录」索引（wikilink 双链跳转）+ images/ 插图文件夹
3. 可选：标准 EPUB 电子书 (.epub) 与大字号精排 PDF (.pdf)（--epub / --pdf 开关）

章节文件直接整体复制进 Obsidian 仓库即可使用。
"""
import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import sys

from playwright.async_api import async_playwright

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = os.path.dirname(os.path.abspath(__file__))
FANG_P = re.compile(r'^<p class="fang">(.*)</p>$')
SENT_END = re.compile(r'[。！？；：…”）》」]$')


def extract_book_id(raw_input):
    """从纯 ID、完整网页链接或带参数的分享链接中提取书籍 ID"""
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


def sanitize_filename(name, max_len=60):
    """去除文件名非法字符（Windows/macOS/Linux 通用）"""
    name = re.sub(r'[<>:"/\\|?*\r\n\t]+', "_", name or "")
    name = name.strip().strip(".").strip()
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name or "untitled"


def refine_chapter_text(text):
    """通用文本清洗：合并被画布折行打断的跨段句子，修复插图穿插导致的断句"""
    raw_lines = [l.strip() for l in text.split("\n") if l.strip()]
    blocks = []
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]
        m = FANG_P.match(line)
        if m:
            fang_parts = []
            while i < len(raw_lines):
                m2 = FANG_P.match(raw_lines[i])
                if not m2:
                    break
                fang_parts.append(m2.group(1).strip())
                i += 1
            joined = "".join(fang_parts)
            prev = blocks[-1] if blocks else ""

            # 若前一行是未闭合的标题/段落，则根据语意并入
            if prev.endswith("</p>"):
                blocks.append(f'<p class="fang">{joined}</p>')
            else:
                bold_seg = f"**{joined}**"
                if (
                    blocks
                    and not prev.startswith(("#", "<p", "!["))
                    and not SENT_END.search(re.sub(r"\*+$", "", prev))
                ):
                    blocks[-1] = prev + bold_seg
                else:
                    blocks.append(bold_seg)
        else:
            if (
                blocks
                and not line.startswith(("#", "<p", "!["))
                and not blocks[-1].startswith(("#", "<p", "!["))
                and blocks[-1].endswith("**")
                and not SENT_END.search(blocks[-1][:-2])
            ):
                blocks[-1] = blocks[-1] + line
            else:
                blocks.append(line)
            i += 1

    merged = []
    idx = 0
    while idx < len(blocks):
        cur = blocks[idx]
        if (
            idx + 2 < len(blocks)
            and not cur.startswith(("#", "<p", "!["))
            and not SENT_END.search(re.sub(r"\*+$", "", cur))
            and blocks[idx + 1].startswith("![")
            and not blocks[idx + 2].startswith(("#", "<p", "!["))
        ):
            merged.append(cur + blocks[idx + 2])
            merged.append(blocks[idx + 1])
            idx += 3
            continue
        merged.append(cur)
        idx += 1

    res = "\n\n".join(merged)
    res = re.sub(r"([^\n。！？；：…<>][”）》」])\n\n(?=[^#<\[!\n*])", r"\1", res)
    res = re.sub(r"([，、；——])\n\n(?=[^#<\[!\n*])", r"\1", res)
    res = re.sub(r"([。！？，、；：][“《「][^”》」\n]{1,10}[”》」])\n\n(?=[^#<\[!\n*])", r"\1", res)
    return res


def read_book(book_id):
    root = os.path.join(BASE, "output", book_id + "_v2")
    ch_dir = os.path.join(root, "chapters")
    raw_dir = os.path.join(root, "raw")

    # 🌟 断点数据自愈重构：若 chapters 目录为空但 raw 存在逐页落盘记录，自动恢复所有章节
    existing_mds = [fn for fn in os.listdir(ch_dir) if fn.endswith(".md")] if os.path.isdir(ch_dir) else []
    if not existing_mds and os.path.isdir(raw_dir):
        raw_pages = [f for f in os.listdir(raw_dir) if f.startswith("page_") and f.endswith(".json")]
        if raw_pages:
            print(f"🔄 检测到 chapters 章节目录为空，正在从 raw/ 逐页落盘数据 ({len(raw_pages)} 页) 自动恢复章节...")
            try:
                import crawl_weread
                rebuilt_cnt = crawl_weread.rebuild_chapters_from_raw(book_id)
                print(f"✅ 成功从历史落盘记录中重构恢复了 {rebuilt_cnt} 个完整章节！")
            except Exception as e:
                print(f"⚠️ 自动重构章节提示: {e}")

    seen_hash = set()
    chaps = []
    if os.path.isdir(ch_dir):
        for fn in sorted(os.listdir(ch_dir)):
            if not fn.endswith(".md"):
                continue
            p = os.path.join(ch_dir, fn)
            with open(p, encoding="utf-8") as f:
                text = f.read()
            h = hashlib.md5(text.encode("utf-8")).hexdigest()
            if h in seen_hash:
                continue
            seen_hash.add(h)
            chaps.append(text)
    return root, chaps


def _yaml_str(s):
    return (s or "").replace('"', "'").replace("\n", " ")


def build_obsidian_markdown(book_id, root, title, author, texts):
    """生成 Obsidian 知识库友好结构：每章一个 .md + 目录索引 + images/ 插图文件夹"""
    safe_title = sanitize_filename(title) or book_id
    out_dir = os.path.join(BASE, "output", safe_title)
    img_out = os.path.join(out_dir, "images")
    os.makedirs(img_out, exist_ok=True)

    # 1. 复制插图到书籍文件夹（章节内 ![](images/xxx.jpg) 相对路径即可正确显示）
    copied = 0
    src_img_dir = os.path.join(root, "images")
    if os.path.isdir(src_img_dir):
        for fn in sorted(os.listdir(src_img_dir)):
            fp = os.path.join(src_img_dir, fn)
            if os.path.isfile(fp) and os.path.getsize(fp) > 100:
                shutil.copy2(fp, os.path.join(img_out, fn))
                copied += 1

    # 2. 逐章生成纯 Markdown 章节文件（含 YAML frontmatter）
    chapter_files = []
    for i, text in enumerate(texts, 1):
        text = refine_chapter_text(text)
        # 转为纯 Markdown 语法：引文 → 引用块，重点段落 → 斜体引用块
        text = re.sub(r'<p class="yw">(.*?)</p>', r'> **\1**', text)
        text = re.sub(r'<p class="fang">(.*?)</p>', r'> *\1*', text)
        first = text.split("\n", 1)[0]
        ch_title = re.sub(r"^#\s*", "", first).strip() or f"第{i}节"
        fname = f"{i:03d} {sanitize_filename(ch_title)}.md"
        fm = (
            "---\n"
            f'title: "{_yaml_str(ch_title)}"\n'
            f'book: "{_yaml_str(title)}"\n'
            f'author: "{_yaml_str(author)}"\n'
            f"chapter: {i}\n"
            "tags:\n"
            "  - weread\n"
            "---\n\n"
        )
        with open(os.path.join(out_dir, fname), "w", encoding="utf-8") as f:
            f.write(fm + text)
        chapter_files.append(fname)

    # 3. 目录索引（wikilink 双链跳转）
    lines = [
        "---",
        f'title: "{_yaml_str(title)}"',
        f'author: "{_yaml_str(author)}"',
        "type: book-index",
        "tags:",
        "  - weread",
        "---",
        "",
        f"# {title}",
        "",
        f"共 {len(chapter_files)} 章",
        "",
    ]
    for fname in chapter_files:
        lines.append(f"- [[{fname[:-3]}]]")
    index_path = os.path.join(out_dir, f"_目录 {safe_title}.md")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"📄 Obsidian Markdown 章节文件生成完成: {out_dir} ({len(chapter_files)} 章, {copied} 张插图)", flush=True)
    return out_dir


def build_epub(book_id, root, title, author, texts):
    try:
        import markdown
        from ebooklib import epub
    except ImportError:
        print("💡 提示: 未安装 markdown 或 EbookLib，跳过 EPUB 格式生成（可通过 pip install markdown EbookLib 安装）", flush=True)
        return None

    book = epub.EpubBook()
    book.set_identifier("weread-" + book_id)
    book.set_title(title)
    book.set_language("zh")
    if author:
        book.add_author(author)

    css_text = """
    body { font-family: "Songti SC", "SimSun", "Noto Serif CJK SC", serif; line-height: 1.85; font-size: 1em; margin: 0.6em 0.8em; }
    h1 { font-size: 1.35em; margin: 1.2em 0 0.9em; text-align: center; }
    h2 { font-size: 1.12em; margin: 1em 0 0.45em; color: #7c2d12; border-left: 3px solid #9e2a2b; padding-left: 0.4em; }
    p { text-indent: 2em; margin: 0.55em 0; }
    p.yw { font-family: "Kaiti SC", "KaiTi", "STKaiti", "楷体", serif; font-size: 1.05em; text-indent: 0; margin: 0.9em 0; padding: 0.5em 0.75em; border-left: 3px solid #9e2a2b; background-color: #f7f5ef; }
    p.fang { font-family: "Kaiti SC", "KaiTi", "STKaiti", "楷体", serif; font-weight: bold; text-indent: 0; margin: 0.45em 0 0.45em 1.2em; }
    img { max-width: 100%; height: auto; display: block; margin: 1em auto; text-indent: 0; }
    """
    style = epub.EpubItem(uid="style", file_name="style/main.css", media_type="text/css", content=css_text.encode("utf-8"))
    book.add_item(style)

    img_dir = os.path.join(root, "images")
    if os.path.isdir(img_dir):
        for fn in sorted(os.listdir(img_dir)):
            fp = os.path.join(img_dir, fn)
            if not os.path.isfile(fp) or os.path.getsize(fp) < 100:
                continue
            mt = "image/png" if fn.lower().endswith(".png") else "image/jpeg"
            with open(fp, "rb") as f:
                book.add_item(epub.EpubItem(uid="img_" + fn, file_name="images/" + fn, media_type=mt, content=f.read()))

    items, toc = [], []
    for i, text in enumerate(texts, 1):
        text = refine_chapter_text(text)
        first = text.split("\n", 1)[0]
        ch_title = re.sub(r"^#\s*", "", first).strip() or f"第{i}节"
        html = markdown.markdown(text)
        c = epub.EpubHtml(title=ch_title, file_name="chap_%04d.xhtml" % i, lang="zh")
        c.content = "<html><head><link rel='stylesheet' href='style/main.css'/></head><body>%s</body></html>" % html
        c.add_item(style)
        book.add_item(c)
        items.append(c)
        toc.append(c)

    book.toc = tuple(toc)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + items

    safe = sanitize_filename(title) or book_id
    out_epub = os.path.join(BASE, "output", f"{safe}（双字体精排）.epub")
    epub.write_epub(out_epub, book)
    print(f"📘 EPUB 电子书生成完成: {out_epub}", flush=True)
    return out_epub


def img_to_data_uri(path):
    if not os.path.isfile(path):
        return ""
    ext = "png" if path.lower().endswith(".png") else "jpeg"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/{ext};base64,{b64}"


def format_yw_html(inner):
    """智能格式化引文/条文卡片：若内容显式以数字编号开头则提取徽章，否则优雅作为高亮引文块展示"""
    m = re.match(r"^(?:第\s*)?(\d{1,4})\s*(?:条)?[:：\s]*(.*)$", inner)
    if m:
        num, rest = m.group(1), m.group(2)
        return f'<p class="yw"><span class="yw-badge">第 {num} 条</span>{rest}</p>'
    return f'<p class="yw">{inner}</p>'


def md_to_html_simple(md_text, img_dir):
    paras = [p.strip() for p in md_text.split("\n\n") if p.strip()]
    out = []
    for line in paras:
        m_img = re.match(r"^!\[.*?\]\(images/([^)]+)\)$", line)
        if m_img:
            fn = m_img.group(1)
            uri = img_to_data_uri(os.path.join(img_dir, fn))
            if uri:
                out.append(f'<div class="img-wrap"><img src="{uri}" alt="{fn}"/></div>')
            continue
        m_h = re.match(r"^(#{2,6})\s+(.*)$", line)
        if m_h:
            level = min(len(m_h.group(1)), 3)
            out.append(f'<h{level} class="sub-head">{m_h.group(2)}</h{level}>')
            continue
        if line.startswith('<p class="yw">') and line.endswith("</p>"):
            inner = line[len('<p class="yw">'):-len("</p>")].strip()
            out.append(format_yw_html(inner))
            continue
        if line.startswith('<p class="fang">') and line.endswith("</p>"):
            inner = line[len('<p class="fang">'):-len("</p>")].strip()
            out.append(f'<div class="highlight-card">{inner}</div>')
            continue
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        if re.match(r"^<strong>[^。！？]{2,28}</strong>$", line):
            out.append(f'<p class="sub-head">{line}</p>')
        else:
            out.append(f"<p>{line}</p>")
    return "\n".join(out)


async def launch_browser_headless(p):
    """自动选择可用浏览器生成 PDF：优先 Playwright Chromium，缺失时免下载直连系统 Edge / Chrome"""
    for ch in [None, "msedge", "chrome"]:
        try:
            kwargs = {"headless": True}
            if ch:
                kwargs["channel"] = ch
            return await p.chromium.launch(**kwargs)
        except Exception:
            continue
    raise RuntimeError("未检测到可用浏览器渲染 PDF，请安装 Google Chrome 或 Microsoft Edge")


async def build_pdf(book_id, root, title, author, intro, texts):
    img_dir = os.path.join(root, "images")
    cover_path = os.path.join(root, "cover.jpg")
    cover_uri = img_to_data_uri(cover_path)

    toc_items, chap_htmls = [], []
    for i, text in enumerate(texts, 1):
        text = refine_chapter_text(text)
        first = text.split("\n", 1)[0]
        ch_title = re.sub(r"^#\s*", "", first).strip() or f"第{i}节"
        body_md = text.split("\n", 1)[1] if "\n" in text else ""
        body_html = md_to_html_simple(body_md, img_dir)
        toc_items.append(
            f'<li><a href="#chap-{i}">'
            f'<span class="toc-num">{i:02d}</span>'
            f'<span class="toc-title">{ch_title}</span>'
            f'</a></li>'
        )
        chap_htmls.append(
            f'<section class="chapter" id="chap-{i}">'
            f'<div class="chap-header">'
            f'<div class="chap-idx">SECTION {i:02d}</div>'
            f'<h1>{ch_title}</h1>'
            f'<div class="chap-rule"></div>'
            f'</div>\n{body_html}</section>'
        )

    cover_img_tag = f'<img class="cover-img" src="{cover_uri}" alt="封面"/>' if cover_uri else ""
    intro_html = "".join(f"<p>{p.strip()}</p>" for p in intro.split("\n") if p.strip())

    # 跨平台字体加载（Mac/Windows/Linux 全局兼容字体栈）
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>{title}</title>
<style>
@page {{ size: A4; margin: 20mm 16.5mm 19mm 16.5mm; }}
* {{ box-sizing: border-box; -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", "WenQuanYi Micro Hei", sans-serif;
    font-size: 13.2pt; line-height: 1.95; letter-spacing: 0.018em; color: #24272b; margin: 0; padding: 0;
}}
.cover-page {{
    page-break-after: always; text-align: center; padding: 12mm 10mm 8mm 10mm;
    border: 1.5px solid #d8cebe; border-radius: 6px;
    background: linear-gradient(180deg, #fdfbf7 0%, #f7f3ea 100%);
    min-height: 248mm; display: flex; flex-direction: column; justify-content: center; align-items: center;
}}
.cover-img {{ max-width: 68mm; max-height: 98mm; height: auto; border-radius: 6px; box-shadow: 0 8px 24px rgba(0,0,0,0.16); margin-bottom: 9mm; border: 2px solid #fff; }}
.cover-title {{ font-family: "STKaiti", "KaiTi", "Kaiti SC", "SimSun", serif; font-size: 24pt; font-weight: bold; color: #1c1917; margin: 0 0 3.5mm 0; }}
.cover-author {{ font-size: 13.5pt; font-weight: 500; color: #7c2d12; margin-bottom: 8mm; letter-spacing: 0.12em; }}
.cover-intro {{ text-align: justify; max-width: 148mm; margin: 0 auto; font-size: 11.2pt; line-height: 1.82; color: #3f3c38; background: rgba(255,255,255,0.78); padding: 5.5mm 7mm; border-left: 4px solid #9e2a2b; border-radius: 4px; }}
.cover-intro p {{ text-indent: 2em; margin: 0.45em 0; }}
.toc-page {{ page-break-after: always; padding: 4mm 2mm; }}
.toc-header {{ text-align: center; margin-bottom: 8mm; }}
.toc-header h2 {{ font-family: "STKaiti", "KaiTi", "Kaiti SC", serif; font-size: 22pt; margin: 0 0 2.5mm 0; letter-spacing: 0.25em; }}
.toc-sub {{ font-size: 9.5pt; color: #9e2a2b; letter-spacing: 0.2em; font-weight: 500; }}
.toc-list {{ list-style: none; padding: 0; margin: 0; column-count: 2; column-gap: 10mm; }}
.toc-list li {{ margin: 2.8mm 0; break-inside: avoid; border-bottom: 1px dashed #dfd8cc; padding-bottom: 1.8mm; }}
.toc-list a {{ text-decoration: none; color: #24272b; display: flex; align-items: center; font-size: 11.8pt; }}
.toc-num {{ font-size: 9.5pt; background: #f4ece1; color: #9e2a2b; font-weight: 700; padding: 1px 6px; border-radius: 4px; margin-right: 3.2mm; min-width: 7.5mm; text-align: center; }}
.chapter {{ page-break-before: always; }}
.chap-header {{ text-align: center; margin: 2mm 0 8mm 0; page-break-after: avoid; }}
.chap-idx {{ font-size: 8.5pt; color: #9e2a2b; letter-spacing: 0.22em; font-weight: 700; margin-bottom: 1.5mm; }}
h1 {{ font-size: 19.5pt; font-weight: 700; color: #18181b; margin: 0 0 3.5mm 0; }}
h2.sub-head, h3.sub-head {{ text-indent: 0; margin: 1.1em 0 0.5em 0; font-size: 14.5pt; color: #7c2d12; border-left: 3.5px solid #9e2a2b; padding-left: 3mm; page-break-after: avoid; }}
.chap-rule {{ width: 26mm; height: 2.5px; background: #9e2a2b; margin: 0 auto; border-radius: 2px; }}
p {{ text-indent: 2em; margin: 0.68em 0; text-align: justify; orphans: 2; widows: 2; }}
p strong {{ font-weight: 700; color: #111315; }}
p.sub-head {{ text-indent: 0; margin: 1.1em 0 0.5em 0; font-size: 14pt; color: #7c2d12; border-left: 3.5px solid #9e2a2b; padding-left: 3mm; page-break-after: avoid; }}
p.yw {{
    font-family: "STKaiti", "KaiTi", "Kaiti SC", "楷体", serif;
    font-size: 14.2pt; line-height: 1.88; letter-spacing: 0.02em; text-indent: 0;
    margin: 1.1em 0 0.85em 0; padding: 0.7em 0.95em; border: 1px solid #e6dec8;
    border-left: 4.5px solid #9e2a2b; background-color: #f8f4ea; color: #181512;
    border-radius: 0 6px 6px 0; page-break-inside: avoid;
}}
.yw-badge {{
    display: inline-block; font-family: sans-serif; font-size: 10pt;
    font-weight: 700; color: #fff; background-color: #9e2a2b; padding: 1px 7px;
    border-radius: 4px; margin-right: 7px; vertical-align: 1.5px; line-height: 1.5;
}}
.highlight-card {{
    font-family: "STKaiti", "KaiTi", "Kaiti SC", "楷体", serif;
    font-size: 13.5pt; line-height: 1.82; text-indent: 0; margin: 0.65em 0 1em 0;
    padding: 0.6em 0.9em; background-color: #fcfaf5; border: 1px dashed #d5c5ab;
    border-left: 3.5px solid #b46535; border-radius: 0 5px 5px 0; color: #2c221b;
    font-weight: bold; page-break-inside: avoid;
}}
.img-wrap {{ text-align: center; margin: 7mm 0; page-break-inside: avoid; }}
.img-wrap img {{ max-width: 86%; max-height: 175mm; height: auto; border-radius: 4px; border: 1px solid #e5dec9; padding: 2mm; background: #fff; }}
</style>
</head>
<body>
<div class="cover-page">
    {cover_img_tag}
    <div class="cover-title">{title}</div>
    {f'<div class="cover-author">{author} 著</div>' if author else ''}
    {f'<div class="cover-intro">{intro_html}</div>' if intro_html else ''}
</div>
<div class="toc-page">
    <div class="toc-header"><h2>目 录</h2><div class="toc-sub">CONTENTS</div></div>
    <ul class="toc-list">{''.join(toc_items)}</ul>
</div>
{''.join(chap_htmls)}
</body>
</html>"""

    safe = sanitize_filename(title) or book_id
    html_path = os.path.join(BASE, "output", f"_temp_{book_id}.html")
    pdf_path = os.path.join(BASE, "output", f"{safe}（双字体精排）.pdf")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    header_author = f"{author} 著" if author else ""
    header_tmpl = f"""<div style="width:100%; font-size:9pt; font-family:sans-serif; color:#8a8175; padding:0 16.5mm; display:flex; justify-content:space-between; border-bottom:0.5px solid #e4ddd0; padding-bottom:3px;"><span>{title}</span><span>{header_author}</span></div>"""
    footer_tmpl = """<div style="width:100%; font-size:9.5pt; font-family:sans-serif; color:#787066; padding:0 16.5mm; text-align:center;">— <span class="pageNumber"></span> / <span class="totalPages"></span> —</div>"""

    async with async_playwright() as p:
        browser = await launch_browser_headless(p)
        page = await browser.new_page()
        file_url = "file:///" + os.path.abspath(html_path).replace("\\", "/")
        await page.goto(file_url, wait_until="networkidle", timeout=60000)
        await page.evaluate("() => document.fonts.ready")
        await page.pdf(
            path=pdf_path,
            format="A4",
            print_background=True,
            display_header_footer=True,
            header_template=header_tmpl,
            footer_template=footer_tmpl,
            margin={"top": "20mm", "bottom": "19mm", "left": "16.5mm", "right": "16.5mm"},
            tagged=True,
            outline=True,
        )
        await browser.close()

    if os.path.isfile(html_path):
        os.remove(html_path)
    print(f"📕 精排 PDF 生成完成: {pdf_path} ({os.path.getsize(pdf_path) / 1048576:.2f} MB)", flush=True)
    return pdf_path


async def main(book_id_or_url, epub=False, pdf=False):
    book_id = extract_book_id(book_id_or_url)
    if not book_id:
        print("❌ 错误: 未提供有效的书籍 ID 或链接")
        return False

    root, texts = read_book(book_id)
    if not texts:
        print(f"❌ 错误: 未找到该书籍已抓取的章节文件，请先运行抓取！(路径: output/{book_id}_v2/chapters/)")
        return False

    meta_path = os.path.join(root, "meta.json")
    title = f"Book_{book_id}"
    author, intro = "", ""
    if os.path.isfile(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            m = json.load(f)
            title = m.get("title", title)
            author = m.get("author", "")
            intro = m.get("intro", "")

    print(f"\n📦 开始导出: 《{title}》")
    out_dir = build_obsidian_markdown(book_id, root, title, author, texts)
    if epub:
        build_epub(book_id, root, title, author, texts)
    if pdf:
        await build_pdf(book_id, root, title, author, intro, texts)
    print(f"\n🎉 导出完成！Obsidian 章节文件夹: {out_dir}")
    print("   💡 将该文件夹整体复制到 Obsidian 仓库任意位置即可开始阅读与记笔记。\n")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python export_book.py <微信读书链接或ID> [--epub] [--pdf]")
        sys.exit(1)
    epub_flag = "--epub" in sys.argv
    pdf_flag = "--pdf" in sys.argv
    bid = [a for a in sys.argv[1:] if not a.startswith("-")][0]
    asyncio.run(main(bid, epub=epub_flag, pdf=pdf_flag))
