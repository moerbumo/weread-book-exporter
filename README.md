# 📚 WeRead Book Exporter (`weread-book-exporter`)

[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/)
[![Playwright](https://img.shields.io/badge/Engine-Playwright-2EAD33.svg)](https://playwright.dev/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

将微信读书 Web 端已购或会员书籍**无损抓取全书全文**，并导出为 **Obsidian 知识库友好的 Markdown 章节文件**（每章一个 .md + 目录索引 + 高清插图）。可选生成精排 PDF 与标准 EPUB。

自动保留原书的**章节结构、章内小标题层级、引文/重点段落样式与高清插图**，方便个人深度学习、全文检索与知识库归档。

---

## ✨ 核心特性

### 1. 100% 全书完整抓取（可靠完结判定，绝不误判假完成）
- **目录覆盖校验**：只有「官方目录全部章节均捕获 + 末章后连续空页」才判定全书完成；
- **空页救援机制**：连续数页无新内容时（风控/网络波动），自动触发多轮救援重试并逐步降速；救援失败只保存断点、**绝不误标完成**；
- **真·断点续传**：中断后重新运行同一条命令，自动从断点章节续抓；曾因旧版假完成误判的书，会在启动时自动核对目录并补齐缺失章节；
- **自动双栏翻页模式**：自动切换水平双栏模式，逐页完整遍历，杜绝滚动模式翻页漏字；
- **精准页面重组**：底层捕获 Canvas 绘制指令与分栏坐标，还原每页文本流与段落顺序，杜绝跨页丢字、重复或错乱。

### 2. 原生级层次还原与断句修复
- **章节按官方目录精确切分**：目录标题按阅读顺序位置匹配，修复章节重复、顺序错乱问题；
- **章内小标题自动降级为 `##`**：问答收录、小节目录等大字号标题不再被误拆成伪章节；
- **智能断句拼接**：自动修复因页面折行、翻页或插图穿插导致的句子中断；
- **插图自动下载**：按阅读顺序内嵌至对应位置。

### 3. Obsidian 知识库友好输出（默认）
```
output/
└── 书名/
    ├── _目录 书名.md        ← 全书索引（wikilink 双链跳转）
    ├── 001 第一章 ....md    ← 每章一个文件（YAML frontmatter）
    ├── 002 第二章 ....md
    └── images/              ← 原书高清插图
```
将书籍文件夹整体复制进 Obsidian 仓库即可使用；可选 `--epub` / `--pdf` 额外生成 EPUB 与精排 PDF。

### 4. 速度三档可调 + 温和防风控
- `--profile fast|normal|gentle` 随时切换（默认 **normal 均衡档**：每页 5~8 秒，每 25~35 页长停 45~75 秒）；
- **空页自动退避降速**：连续无内容时翻页间隔自动成倍拉长，主动给服务端缓冲；
- 随机波动翻页间隔，告别机械固定的秒级请求。

---

## 🚀 快速开始

### 1. 安装依赖（国内极速安装，仅需几秒）

```bash
# 克隆仓库
git clone https://github.com/WANGXIN-567/weread-book-exporter.git
cd weread-book-exporter

# 使用国内高速镜像安装依赖（推荐腾讯云或阿里云，稳定不报 403）
pip install -r requirements.txt -i https://mirrors.cloud.tencent.com/pypi/simple/
# 备用阿里云镜像：
# pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

> ⚡ **免下载 200MB 浏览器**：代码内置了智能浏览器检测，会自动直接调用电脑自带的 **Microsoft Edge** 或 **Google Chrome**；如果系统未安装上述浏览器，可执行 `playwright install chromium` 补充下载。

### 2. 一键抓取并导出（核心推荐）

直接复制微信读书网页版的完整链接（支持任意已购/会员书籍）：

```bash
# 直接粘贴整条网页链接（或书籍 ID 均可）：
python main.py https://weread.qq.com/web/reader/3b132980813ab94eeg013129
```

> **提示**：
> - 首次运行时会自动弹出浏览器窗口，使用微信扫码登录一次即可。
> - 脚本会自动完成「全书章节遍历 ➔ 原版高清封面下载 ➔ 插图下载 ➔ Obsidian Markdown 章节文件生成」全流程！
> - 所有文件最终保存在 `output/` 目录下，按书名分文件夹。
> - **断点续传**：抓取中断（风控/断网/手动 Ctrl+C）后，重新运行同一条命令即可从断点继续，无需重头开始。

### 3. 常用参数

```bash
# 速度档位：fast 快速 / normal 均衡(默认) / gentle 温和
python main.py <链接或ID> --profile fast

# 强制重新完整抓取
python main.py <链接或ID> --force

# 额外生成 EPUB / PDF
python main.py <链接或ID> --epub --pdf
```

*(高级用法：也可单独执行 `python crawl_weread.py <链接或ID> [--force] [--profile ...] [--rebuild]` 与 `python export_book.py <链接或ID> [--epub] [--pdf]`)*

---

## 🛠️ 工作流程

```mermaid
flowchart LR
    A["微信读书网页版\n(自动切换双栏翻页)"] --> B["底层文本与插图捕获\n逐页提取字号/字重/坐标"]
    B --> C["目录位置匹配切章\n段落重组 & 去重"]
    C --> D["完结判定\n目录覆盖 + 末章空页"]
    D --> E["Obsidian Markdown\n(EPUB / PDF 可选)"]
```

---

## 🧪 开发与测试

```bash
pip install -r requirements-dev.txt -i https://mirrors.cloud.tencent.com/pypi/simple/
python -m pytest tests/ -q
```

---

## ⚠️ 免责声明 (Disclaimer)

1. 本工具仅用于个人学习、读书笔记整理与前端渲染技术研究。
2. 请支持正版阅读，仅对您个人合法拥有阅读权限的书籍使用本工具，导出的文件请勿用于任何商业用途或公开传播。
3. 本仓库仅包含通用转换脚本，不包含任何受版权保护的书籍内容或个人登录信息。

---

如果这个工具对你的阅读与笔记整理有帮助，欢迎点亮一颗 **Star ⭐** 支持！
