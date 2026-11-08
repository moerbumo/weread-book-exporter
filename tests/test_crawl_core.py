"""crawl_weread.py 纯函数单元测试（不依赖浏览器）"""
import json

import crawl_weread as cw


# ---------------------------------------------------------------- extract_book_id
def test_extract_book_id_from_url():
    assert cw.extract_book_id("https://weread.qq.com/web/reader/3b132980813ab94eeg013129") == "3b132980813ab94eeg013129"
    assert cw.extract_book_id("https://weread.qq.com/web/bookDetail/abc123def456") == "abc123def456"


def test_extract_book_id_plain():
    assert cw.extract_book_id("3b132980813ab94eeg013129") == "3b132980813ab94eeg013129"
    assert cw.extract_book_id("  3b132980813ab94eeg013129  ") == "3b132980813ab94eeg013129"


def test_extract_book_id_empty():
    assert cw.extract_book_id("") == ""
    assert cw.extract_book_id(None) == ""


# ---------------------------------------------------------------- 目录位置匹配
def test_match_title_text_exact():
    assert cw._match_title_text("第一章 量与价的基本概念", "第一章 量与价的基本概念") == ""


def test_match_title_text_stuck():
    rem = cw._match_title_text("第2章 般若品第二次日，韦使君请益", "第2章 般若品")
    assert rem is not None
    assert "韦使君" in rem


def test_match_title_text_no_match():
    assert cw._match_title_text("今天天气不错", "第一章 起点") is None


def test_match_catalog_pos_advance():
    catalog = ["第一章", "第二章", "第三章"]
    title, rem, pos = cw.match_catalog_pos("第二章 新的开始", catalog, 1)
    assert title == "第二章"
    assert pos == 2


def test_match_catalog_pos_skip_empty():
    catalog = ["第一章", "第二章", "第三章"]
    title, rem, pos = cw.match_catalog_pos("第三章内容", catalog, 1)
    assert title == "第三章"
    assert pos == 3


def test_match_catalog_pos_end():
    catalog = ["第一章", "第二章"]
    assert cw.match_catalog_pos("正文", catalog, 2) == (None, "", 2)
    assert cw.match_catalog_pos("正文", [], 0) == (None, "", 0)


def test_match_catalog_line_forward_scan():
    # 扉页/版权信息/自序不渲染标题时,位置卡在 0,大字号标题行前向扫描兜底命中第 4 项
    catalog = ["扉页", "版权信息", "自序 动态推演，做好应对", "第一章 量与价的基本概念", "第二章"]
    title, rem, pos = cw.match_catalog_line("第一章量与价的基本概念", "title", catalog, 0)
    assert title == "第一章 量与价的基本概念"
    assert pos == 4


def test_match_catalog_line_sub_heading_not_catalog():
    catalog = ["扉页", "第一章"]
    # 非目录大字号标题不触发前向扫描,保持 None → 降级为 ## 子标题
    assert cw.match_catalog_line("一K线图", "title", catalog, 1) == (None, "", 1)


def test_match_catalog_line_non_title_no_forward():
    catalog = ["扉页", "第一章"]
    # 普通正文行不做前向扫描(不以目录标题开头,位置窗口内也无匹配)
    assert cw.match_catalog_line("今天的内容很精彩", "js", catalog, 0) == (None, "", 0)


# ---------------------------------------------------------------- 页面切分与行聚合
def test_split_pages_basic():
    chars = [
        {"t": "你好", "x": 10, "y": 10, "f": "20px serif", "side": 0, "seq": 1},
        {"t": "世界", "x": 60, "y": 10, "f": "20px serif", "side": 0, "seq": 1},
        {"t": "再见", "x": 10, "y": 20, "f": "20px serif", "side": 0, "seq": 2},
    ]
    pages = cw.split_pages(chars)
    assert len(pages) == 2
    assert pages[0][0] == 0


def test_split_pages_filters_junk():
    chars = [
        {"t": "abcdefg", "x": 10, "y": 10, "f": "20px", "side": 0, "seq": 1},
        {"t": "a;b;c;", "x": 10, "y": 10, "f": "20px", "side": 0, "seq": 1},
    ]
    assert cw.split_pages(chars) == []


def test_page_lines_title_merge():
    chars = [
        {"t": "第一章", "x": 10, "y": 100, "f": "25px bold", "side": 0, "seq": 1},
        {"t": "量与价的", "x": 10, "y": 130, "f": "25px bold", "side": 0, "seq": 1},
    ]
    lines = cw.page_lines(chars)
    assert lines[0]["text"] == "第一章量与价的"
    assert lines[0]["cls"] == "title"


# ---------------------------------------------------------------- render_md
def test_render_md_sub_heading():
    blocks = [
        {"type": "line", "cls": "js", "text": "正文段落。"},
        {"type": "line", "cls": "title", "text": "一K线图"},
        {"type": "line", "cls": "js", "text": "小节正文。"},
    ]
    body, imgs = cw.render_md("第一节 量价配合", blocks, 1)
    assert body.startswith("# 第一节 量价配合\n")
    assert "## 一K线图" in body
    assert imgs == []


def test_render_md_skips_own_title():
    blocks = [
        {"type": "line", "cls": "title", "text": "第一节 量价配合"},
        {"type": "line", "cls": "js", "text": "正文。"},
    ]
    body, _ = cw.render_md("第一节 量价配合", blocks, 1)
    assert "## 第一节" not in body


def test_render_md_yw_and_fang():
    blocks = [
        {"type": "line", "cls": "yw", "text": "引文内容"},
        {"type": "line", "cls": "fang", "text": "加粗小标题"},
    ]
    body, _ = cw.render_md("章", blocks, 1)
    assert '<p class="yw">引文内容</p>' in body
    assert '<p class="fang">加粗小标题</p>' in body


def test_render_md_image():
    blocks = [
        {"type": "line", "cls": "js", "text": "文字。"},
        {"type": "img", "src": "https://res.weread.qq.com/wrepub/epub/123.jpg"},
    ]
    body, imgs = cw.render_md("章", blocks, 3)
    assert "![图](images/ch0003_img01.jpg)" in body
    assert imgs and imgs[0]["name"] == "ch0003_img01.jpg"


# ---------------------------------------------------------------- 速度档位
def test_resolve_profile():
    assert cw.resolve_profile("fast") == "fast"
    assert cw.resolve_profile("normal") == "normal"
    assert cw.resolve_profile("gentle") == "gentle"


# ---------------------------------------------------------------- 重建与去重
def test_rebuild_chapters_dedupe(tmp_path, monkeypatch):
    monkeypatch.setattr(cw, "BASE", str(tmp_path))
    book_dir = tmp_path / "output" / "testbook_v2"
    raw_dir = book_dir / "raw"
    raw_dir.mkdir(parents=True)
    (book_dir / "chapters").mkdir(parents=True)

    page1 = {"step": 0, "chapter": "第一章", "words": 10,
             "blocks": [
                 {"type": "line", "cls": "js", "text": "第一段的内容比较多。"},
                 {"type": "line", "cls": "js", "text": "第二段的内容也不少。"},
             ], "lines": ["第一段的内容比较多。", "第二段的内容也不少。"]}
    page2 = dict(page1, step=1)  # 与 page1 完全重复（断点重叠重抓）
    page3 = {"step": 2, "chapter": "第二章",
             "blocks": [
                 {"type": "line", "cls": "title", "text": "小标题"},
                 {"type": "line", "cls": "js", "text": "新内容。"},
             ], "lines": ["小标题", "新内容。"]}

    for i, p in enumerate([page1, page2, page3]):
        (raw_dir / f"page_{i:04d}.json").write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")

    n = cw.rebuild_chapters_from_raw("testbook")
    assert n == 2

    ch1 = (book_dir / "chapters" / "0001.md").read_text(encoding="utf-8")
    assert ch1.count("第一段的内容") == 1  # 重复页被去重

    ch2 = (book_dir / "chapters" / "0002.md").read_text(encoding="utf-8")
    assert "## 小标题" in ch2


def test_max_page_step_and_drop_partial(tmp_path, monkeypatch):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    for n in (0, 1, 3, 7):
        (raw_dir / f"page_{n:04d}.json").write_text(
            json.dumps({"step": n, "chapter": f"第{n}章", "blocks": [], "lines": []}, ensure_ascii=False),
            encoding="utf-8")
    assert cw.max_page_step(str(raw_dir)) == 8  # 最大编号 7 + 1

    cw.drop_partial_chapter_pages(str(raw_dir), "第3章")
    remaining = sorted(f.name for f in raw_dir.iterdir())
    assert "page_0003.json" not in remaining
    assert "page_0007.json" in remaining


def test_resplit_blocks_by_catalog():
    # 模拟"整本书没切章"的坏数据:所有页面 chapter 都是扉页,但块流里有标题行
    catalog = ["扉页", "第一章 量与价的基本概念", "第二章 量与价的关系"]
    stream = [
        {"type": "line", "cls": "js", "text": "这是扉页文字。"},
        {"type": "line", "cls": "title", "text": "第一章量与价的基本概念"},
        {"type": "line", "cls": "js", "text": "第一章正文。"},
        {"type": "line", "cls": "title", "text": "一K线图"},
        {"type": "line", "cls": "js", "text": "小节正文。"},
        {"type": "line", "cls": "title", "text": "第二章量与价的关系"},
        {"type": "line", "cls": "js", "text": "第二章正文。"},
    ]
    sections = cw.resplit_blocks(catalog, stream)
    assert [t for t, _ in sections] == ["扉页", "第一章 量与价的基本概念", "第二章 量与价的关系"]
    ch1 = [b["text"] for b in sections[1][1]]
    assert "第一章正文。" in ch1
    assert "一K线图" in ch1  # 非目录标题降级为章内块(cls=title)
    assert "第二章正文。" in [b["text"] for b in sections[2][1]]


def test_rebuild_with_catalog_resplit(tmp_path, monkeypatch):
    # 坏数据:pages 全部 chapter=扉页 + 目录文件存在 → 重建时按目录重切分
    monkeypatch.setattr(cw, "BASE", str(tmp_path))
    book_dir = tmp_path / "output" / "testbook_v2"
    raw_dir = book_dir / "raw"
    raw_dir.mkdir(parents=True)
    (book_dir / "chapters").mkdir(parents=True)
    (book_dir / "_catalog.json").write_text(
        json.dumps(["扉页", "第一章 起点", "第二章 终点"], ensure_ascii=False), encoding="utf-8")

    pages = [
        {"step": 0, "chapter": "扉页", "blocks": [
            {"type": "line", "cls": "js", "text": "这是扉页文字。"},
            {"type": "line", "cls": "title", "text": "第一章起点"},
            {"type": "line", "cls": "js", "text": "第一章正文。"},
        ], "lines": []},
        {"step": 1, "chapter": "扉页", "blocks": [
            {"type": "line", "cls": "title", "text": "第二章终点"},
            {"type": "line", "cls": "js", "text": "第二章正文。"},
        ], "lines": []},
    ]
    for i, p in enumerate(pages):
        (raw_dir / f"page_{i:04d}.json").write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")

    n = cw.rebuild_chapters_from_raw("testbook")
    assert n == 3
    ch1 = (book_dir / "chapters" / "0001.md").read_text(encoding="utf-8")
    ch2 = (book_dir / "chapters" / "0002.md").read_text(encoding="utf-8")
    ch3 = (book_dir / "chapters" / "0003.md").read_text(encoding="utf-8")
    assert ch1.startswith("# 扉页")
    assert ch2.startswith("# 第一章 起点")
    assert ch3.startswith("# 第二章 终点")
