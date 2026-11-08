#!/usr/bin/env python3
"""WeRead One-Click Book Exporter (main.py)

一键无损抓取微信读书全书，并导出为 Obsidian 知识库友好的 Markdown 章节文件。

用法:
    python main.py <微信读书网页链接或书籍ID> [选项]

选项:
    --profile {fast,normal,gentle}   抓取速度档位（默认 normal 均衡档）
    --force / -f                     强制重新完整抓取
    --epub                           额外生成 EPUB 电子书
    --pdf                            额外生成精排 PDF

示例:
    python main.py https://weread.qq.com/web/reader/3b132980813ab94eeg013129
    python main.py 3b132980813ab94eeg013129 --profile fast
"""
import argparse
import asyncio
import os
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import crawl_weread
import export_book


def print_banner():
    print("""
===========================================================
  📚 WeRead Book Exporter - 微信读书全书无损一键导出工具
===========================================================
    """)


async def run(raw_input, force=False, profile="normal", epub=False, pdf=False):
    book_id = crawl_weread.extract_book_id(raw_input)
    if not book_id:
        print("❌ 错误: 请输入有效的微信读书网页链接或书籍 ID！")
        print("👉 示例: python main.py https://weread.qq.com/web/reader/3b132980813ab94eeg013129")
        sys.exit(1)

    print(f"🚀 开始执行一键全流程导出，书籍 ID: {book_id}，速度档位: {profile}\n")

    # 第一步：完整抓取全书（可靠完结判定 + 真·断点续传）
    print("【步骤 1/2】抓取全书章节、插图与原版封面...")
    crawl_ok = await crawl_weread.main(book_id, force=force, profile=profile)
    if not crawl_ok:
        print("\n❌ 抓取未完成（疑似风控或网络中断，也可能是手动中断）。")
        print("   ✅ 已保存断点进度（未标记完成）。建议等待 10~30 分钟风控冷静期后，")
        print("      重新运行同一条命令即可自动断点续传，无需重头开始。")
        sys.exit(1)

    # 第二步：生成 Obsidian Markdown 章节文件（EPUB/PDF 可选）
    print("\n【步骤 2/2】正在生成 Obsidian Markdown 章节文件...")
    export_ok = await export_book.main(book_id, epub=epub, pdf=pdf)
    if not export_ok:
        print("\n❌ 导出过程遇到错误，请检查日志。")
        sys.exit(1)

    out_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "output"))
    print("=" * 60)
    print(f"🎉 全部完成！导出的书籍文件夹已保存在:\n👉 {out_root}")
    print("   💡 将书籍文件夹整体复制到 Obsidian 仓库即可开始阅读与记笔记。")
    print("=" * 60)


def main():
    print_banner()
    parser = argparse.ArgumentParser(description="微信读书全书无损抓取 + Obsidian Markdown 导出")
    parser.add_argument("input", nargs="?", help="微信读书网页链接或书籍 ID")
    parser.add_argument("-f", "--force", action="store_true", help="强制重新完整抓取")
    parser.add_argument("--profile", choices=["fast", "normal", "gentle"], default="normal",
                        help="抓取速度档位: fast 快速 / normal 均衡(默认) / gentle 温和")
    parser.add_argument("--epub", action="store_true", help="额外生成 EPUB 电子书")
    parser.add_argument("--pdf", action="store_true", help="额外生成精排 PDF")
    args = parser.parse_args()

    raw_input = args.input
    if not raw_input:
        try:
            raw_input = input("请输入微信读书网页链接或书籍 ID: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n已取消。")
            sys.exit(0)
        if not raw_input:
            print("未输入有效链接或 ID，程序退出。")
            sys.exit(0)

    asyncio.run(run(raw_input, force=args.force, profile=args.profile, epub=args.epub, pdf=args.pdf))


if __name__ == "__main__":
    main()
