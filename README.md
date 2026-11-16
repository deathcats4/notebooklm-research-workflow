# NotebookLM Research Workflow Skill

面向科研文献精读与防幻觉质量控制的通用 **Agent Skill**（学科无关，适用于生物医学、计算机/AI、化学材料、地学、社科等场景），与基础 `notebooklm` Skill 互补。

核心目标是在科研文献进入综述或论文写作前，建立三层防线：
1. **双引擎分工**：NotebookLM 负责 15–50+ 篇长文献广度探索、脉络梳理与多模态初稿；1–8 篇核心精读及复杂跨页表格精确提数交由本地 Agent 直读原 PDF。
2. **输入质检与物理隔离**：拦截“假就绪”PDF（缺字/丢数字/矢量图切碎），并将一手实证（`primary`）、二手综述（`review`）与背景参考（`background`）物理隔离。
3. **输出机审与诚实分层**：通过自动化脚本拦截区间吞号、单位混用、静默漏引、确定性拔高与二手伪引，所有结论按核验等级诚实分层披露。

---

## 目录结构

```text
notebooklm-research-workflow/
├── SKILL.md                    # Agent 触发指令与 5 步标准工作流
├── README.md                   # 面向人类用户的使用与安装说明
├── LICENSE                     # MIT 开源协议
├── requirements.txt            # Python 第三方依赖清单
├── .gitignore                  # Git 忽略规则（防止缓存与凭证入库）
├── references/
│   ├── research-prompts.md     # 5 套跨学科科研人设与隔离核查提示词模板
│   └── budget-tiers.md         # 按严谨度分级的工具路由与额度参考
└── scripts/
    ├── pdf_prepare.py          # 本地 PDF 预检、切页、框选裁图、原位补图与图件打包 PDF
    ├── check_fulltext.py       # NotebookLM 入库全文脚本/噪声/数字密度审计
    ├── export_md_figures.py    # 导出 NotebookLM Markdown 与实际识别到的图件切片
    ├── md2docx.py              # 将 OCR Markdown + 插图重建为干净的 DOCX
    ├── figure_probe.py         # 读图防漏字探针（验证图内信息可见且无正文泄漏）
    ├── verify_outputs.py       # CSV 数值与量纲机审 / ask 覆盖率审计 / 原文回溯
    └── zotero_triage.py        # （可选）Zotero 馆藏入库前分诊与核验门禁
```

---

## 环境要求与安装

- **Python**：`>= 3.10`
- **基础前置**：已安装并登录 [`notebooklm-py`](https://github.com/teng-lin/notebooklm-py)（建议同时安装基础 `notebooklm` Skill）
- **可选扩展**：如需使用 Zotero 分诊与核验卡写回，需额外配置 Zotero MCP（如 `zotero_54yyyu`）

### 1. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

> 若使用 `uv` 或虚拟环境：
> ```bash
> uv pip install -r requirements.txt
> ```

### 2. 初始化 NotebookLM 登录态

```bash
notebooklm login
notebooklm auth check --test --passive --json
```

---

## 作为 Agent Skill 安装使用

将本目录放入你所用 Agent 工具的 Skill 发现目录即可：

- **项目级（推荐，可随项目版本控制共享）**：
  `<your-project>/.agents/skills/notebooklm-research-workflow/`
- **用户全局级（所有项目可用）**：
  - Antigravity / 通用 `.agents` 规范：`~/.agents/skills/notebooklm-research-workflow/`
  - Claude Code：`~/.claude/skills/notebooklm-research-workflow/`

安装后，直接向 Agent 发出科研文献入库、质检、修复、跨篇对比或输出核验任务即可触发该 Skill；完整操作规则由 Agent 按需读取 [SKILL.md](./SKILL.md) 执行。

---

## 配套脚本快速检查

```bash
# 查看各脚本命令行帮助
python scripts/pdf_prepare.py --help
python scripts/check_fulltext.py --help
python scripts/export_md_figures.py --help
python scripts/md2docx.py          # 打印位置参数用法（exit 2）
python scripts/figure_probe.py --help
python scripts/verify_outputs.py --help
python scripts/zotero_triage.py --help
```

---

## 开源协议

本 Skill 本身的文档与脚本采用 **[MIT License](./LICENSE)** 开源。

**第三方依赖提示**：运行辅助脚本需由用户自行安装 `requirements.txt` 中的第三方 Python 库；其中 `pymupdf` 与 `pymupdf4llm` 由 Artifex 提供并采用双许可（AGPL-3.0 或商业许可），`notebooklm-py`、`python-docx` 遵循其各自上游开源协议。
