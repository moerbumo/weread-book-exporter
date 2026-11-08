"""export_book.py 纯函数与 Obsidian 导出测试（不依赖浏览器）"""
import os

import export_book as eb


# ---------------------------------------------------------------- refine_chapter_text
def test_refine_preserves_sub_headings():
    text = "# 第一章\n\n正文。\n\n## 小标题\n\n小节正文。"
    out = eb.refine_chapter_text(text)
    assert "## 小标题" in out


def test_refine_merges_fang_into_previous():
    # 重点段落在上一段未结束时并入其末尾（加粗）
    text = "# 章\n\n前文未说完\n\n<p class=\"fang\">重点内容</p>"
    out = eb.refine_chapter_text(text)
    assert "前文未说完**重点内容**" in out


def test_refine_keeps_fang_after_sentence_end():
    # 上一段已完整结束时，重点段落独立成加粗块
    text = "# 章\n\n前文已说完。\n\n<p class=\"fang\">重点内容</p>"
    out = eb.refine_chapter_text(text)
    assert "前文已说完。**重点内容**" not in out
    assert "**重点内容**" in out


def test_refine_image_between_paragraphs():
    text = "# 章\n\n段落未结束\n\n![图](images/a.jpg)\n\n继续说完。"
    out = eb.refine_chapter_text(text)
    assert "段落未结束继续说完" in out
    assert "![图](images/a.jpg)" in out


# ---------------------------------------------------------------- sanitize_filename
def test_sanitize_filename():
    assert eb.sanitize_filename("量价关系——透视股票涨跌脉络") == "量价关系——透视股票涨跌脉络"
    assert eb.sanitize_filename('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"
    assert eb.sanitize_filename("") == "untitled"
    assert eb.sanitize_filename("  ").strip() == "untitled"


# ---------------------------------------------------------------- Obsidian 导出
def test_build_obsidian_structure(tmp_path, monkeypatch):
    monkeypatch.setattr(eb, "BASE", str(tmp_path))
    root = tmp_path / "output" / "bookid_v2"
    (root / "images").mkdir(parents=True)
    (root / "images" / "pic.jpg").write_bytes(b"x" * 200)

    texts = [
        "# 第一章 起点\n\n正文。\n\n## 小节\n\n<p class=\"yw\">引文内容</p>\n\n<p class=\"fang\">重点句</p>\n",
    ]
    out_dir = eb.build_obsidian_markdown("bookid", str(root), "测试书", "作者甲", texts)

    files = sorted(os.listdir(out_dir))
    ch_files = [f for f in files if f.endswith(".md") and not f.startswith("_")]
    assert len(ch_files) == 1
    assert ch_files[0].startswith("001 ")

    content = open(os.path.join(out_dir, ch_files[0]), encoding="utf-8").read()
    assert content.startswith("---")
    assert 'book: "测试书"' in content
    assert "# 第一章 起点" in content
    assert "> **引文内容**" in content  # HTML 标签已转纯 Markdown 引用块
    assert "> *重点句*" in content
    assert "<p class=" not in content

    idx = [f for f in files if f.startswith("_目录")][0]
    idx_content = open(os.path.join(out_dir, idx), encoding="utf-8").read()
    assert "[[" in idx_content  # wikilink 双链

    assert os.path.isfile(os.path.join(out_dir, "images", "pic.jpg"))
