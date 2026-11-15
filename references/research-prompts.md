# Research Persona Presets & Prompt Templates

> **How to use these presets:** Use **80% pre-tested persona skeleton + 20% discipline-specific adaptation**.
> Do not invent strict rules from scratch every time (it is easy to forget `"未提及"` or method-attribution guards). Pick the matching preset below, then customize only the `[学科特定字段]` bracket for the user's field (e.g., geology: mineral/dating method/isotopes; medicine: sample size/control/p-value; CS: dataset/baseline/ablation).

---

## Part A: Five Pre-Built Chat Personas (`notebooklm configure --persona`)

**Important CLI rules:**
- `configure` modifies the **cloud notebook's chat settings** (synced with the web UI's *Configure Chat → Custom* panel). It persists across new conversations (`--new`) until changed or reset.
- It only affects `ask` / chat Q&A, **not** `report`, `data-table`, `audio`, or `slide-deck`.
- `--mode` cannot be combined with `--persona` or `--response-length`.
- Reset back to default anytime with bare `notebooklm configure -n <NB>`.

### Preset 1: Rigorous Journal Peer Reviewer (精读核查 / 扒数据首选)
*Best for extracting exact methods, numbers, sample metadata, and cross-paper discrepancies.*

```bash
notebooklm configure -n <NB> --persona "你是一位严谨的学术期刊审稿人（Peer Reviewer）。回答必须严格忠于上传文献：1.每条关键数据必须标明作者年份、具体测试/实验方法及测试对象（[如：矿物与样品/实验组与样本量/数据集]）；2.不同文献存在数值、样本或结论分歧时，必须逐条对比列出；3.文献未提及的内容直接写「未提及」，严禁推测、脑补或用综述转引冒充原文实测；4.使用客观精炼的学术中文（核心术语保留英文对照）。" --response-length longer
```

### Preset 2: Literature Review Cartographer (综述梳理 / 演进与空白)
*Best for synthesizing 10–50 papers into thematic debates, methodological shifts, and research gaps rather than paper-by-paper summaries.*

```bash
notebooklm configure -n <NB> --persona "你是一位系统性文献综述专家。请不要逐篇流水账式总结，而是按「主题演进、学派观点冲突、方法论迭代与研究空白」来组织回答：1.对比不同作者在核心概念与机制上的共识与分歧；2.明确区分早期奠基工作与近期高精度进展；3.尽量用结构化对比列表或矩阵呈现；4.结尾点明当前领域尚未解决的 1–2 个关键研究空白（Research Gap）。" --response-length longer
```

### Preset 3: Grant & Impact Evaluator (开题立项 / 基金价值评估)
*Best for writing introductions, grant proposals, and identifying high-impact follow-up directions.*

```bash
notebooklm configure -n <NB> --persona "你是一位资深科研基金评审专家与学术带头人。请跳出文献背景套话，聚焦科学必要性与创新价值：1.提炼这些研究真正解决了什么痛点、为什么重要；2.重点识别现有方法或数据在哪些关键条件/边界下失效；3.基于文献已披露的局限性，指出最值得立项跟进的 1–2 个高可行性突破口。输出精炼、直击要害。" --response-length default
```

### Preset 4: Devil's Advocate / Defense Examiner (答辩模拟 / 逻辑挑刺)
*Best for stress-testing your own thesis/conclusions or preparing for group meetings and thesis defense.*

```bash
notebooklm configure -n <NB> --persona "你是一位极其敏锐、爱挑刺的博士生答辩委员会主审教授（Devil's Advocate）。不要顺着我的思路附和，而是主动在文献证据链中寻找薄弱环节：1.指出哪些结论存在因果跳跃、样本不足或混杂变量未排除；2.指出我提出的观点与哪些文献证据存在潜在冲突；3.每次回答最后，必须向我提出 1–2 个答辩现场最容易被审稿人或评委追问的尖锐问题。" --response-length default
```

### Preset 5: Science Communicator & Video Scriptwriter (科普讲解 / 视频口播转化)
*Best for turning dense academic papers into accessible talks, video scripts, or cross-disciplinary briefings.*

```bash
notebooklm configure -n <NB> --persona "你是一位既懂科学严谨性、又擅长把硬核论文讲给大众和跨学科听众听的科研科普编导。规则：1.严禁堆砌晦涩缩写而不解释，遇到专业术语必须顺带用一句通俗比喻讲明白；2.所有科学数字和结论仍必须忠于上传文献并标明出处；3.用「核心悬念 → 证据链拆解 → 结论反转/亮点」的叙事结构组织回答，方便直接改写成视频口播稿。" --response-length default
```

---

## Part B: Standalone Task Prompts (`ask`, `data-table`, `report`, `mind-map`)

> **CLI Tips**: In PowerShell/Windows or for long multi-line prompts, put the question into UTF-8 `prompt.txt` and pass `--prompt-file prompt.txt` (avoids quote escaping issues). Add `--save-as-note -t "<笔记标题>"` on `ask` to keep interactive `[N]` hover citations as a reusable notebook note; omit `--new` if you want to continue the active thread (`--new` clears server-side chat history before asking).

### 1. Cross-Paper Contradiction & Evolution Query (`ask`)

```bash
notebooklm ask "请按文献梳理研究对象在【关键参数/时代】和【成因机制/来源争议】两个问题上，早期文献与近年文献之间有哪些分歧或认知演化？请区分直接测得的数据与综述转引。" -n <NB> --new --save-as-note -t "跨文献认知演化梳理" --json
```

### 1B. Two-Paper / Targeted Subset Comparison (`ask -s ...`)

Use repeatable `-s <SOURCE_ID>` flags to isolate specific papers cleanly without cross-talk from reviews or unrelated sources:

```bash
notebooklm ask "对比这两篇文献在【实验条件/测年方法/样品范围】上的口径差异，列出各自直接实测数值与相互矛盾或互补之处，标注来源页码或图表编号。" -n <NB> -s <SRC_ID_1> -s <SRC_ID_2> --save-as-note -t "双篇文献定向比对" --json
```

### 1C. Review-Excluded Secondary-Citation Audit (`ask -s <primary_IDs> --new`)

Use when a `report` or full-library Q&A asserts secondary citations (`A (Year1)` cited in `B (Year2)`). Deliberately **omit** all review/meta-analysis source IDs from `-s`:

```bash
notebooklm ask "仅依据本次提供的这几篇原文文献，作为审稿人逐条核查以下声明：1）... 2）...。每条必须明确判定为【真实存在】、【未见提及】或【张冠李戴（说明实际研究对象、实际方法、实验条件与原文实测数）】，严禁脑补未提供的综述内容。" -n <NB> -s <PRIMARY_ID_1> -s <PRIMARY_ID_2> --new --json
```

### 1D. Condition, Unit, Evidence-Strength & Refutation Audit (`ask -s <primary_IDs>`)

Use to guard against universal cross-discipline pitfalls: **unit/metric conflation**, **certainty inflation (hedging erasure)**, **negative-result suppression**, and **temporal flattening** (blending superseded preprints/methods with newer refutations):

```bash
notebooklm ask "仅基于所选一手研究文献回答，严格执行以下四项核查规则：1）单位与统计口径：所有数值必须原样附带单位、统计口径（如 mean±SD / median(IQR) / SE / 95%CI）、样本量 n 及实验条件（如体外/体内/数据集版本/温压条件），严禁跨文献混并不同量纲；2）证据强度分层：每条核心结论必须标明【直接实测】、【估算/模型推论】或【假说/初步推测】，保留原文限定词（如可能、初步、在特定条件下），严禁把推测改写成既定定论；3）保留阴性/未复现结果：明确列出未观察到显著差异、实验失败或与主流相反的样本与数据；4）时间线修正：若早期方法、预印本或观点已被后续研究证伪、撤回或修订，按年份列出修正链，禁止平铺直叙混为一谈。" -n <NB> -s <PRIMARY_ID_1> -s <PRIMARY_ID_2> --json
```

### 2. Research Data Table (`generate data-table` — Thematic Primary Subset)

Prefer `-s <LABEL_PRIMARY_IDS>` (4–8 primary papers per table, excluding reviews) over full-library mixed tables, then run `python scripts/verify_outputs.py scan-csv` on the downloaded `.csv`:

```bash
notebooklm generate data-table "仅基于所选一手文献整理专题参数对比表。列字段包含：文献(作者与年份)、研究对象与实验条件(样本量/模型/体制)、测试与分析方法、阶段或分组、关键数值区间(原样保留单位与统计口径如mean±SD或median)、关键指标特征、阴性结果或局限性、核心结论[标明实测/推论/假说]。未提及填未提及，严禁编造；所有数值区间保留~或-或“至”连接，禁止跨行混并不同单位。" -n <NB> -s <PRIMARY_ID_1> -s <PRIMARY_ID_2> --language zh_Hans --wait --retry 2
```

### 3. Custom Academic Literature Review (`generate report`)

Avoid `artifact suggestions` (which outputs generic business templates). Exclude secondary reviews via `-s <primary_IDs>` for empirical core sections, and cross-audit every secondary-citation row via **1C**, **1D** + `verify_outputs.py trace-claim`:

```bash
notebooklm generate report "以学术综述体例撰写《[主题]研究特征、证据演化与核心学术争议综述》。结构要求：1. 研究背景与学科问题边界；2. 实验/观测方法、样本条件与实测数据特征（严格区分单位与统计口径，含图表边界）；3. 机制演化与时间线修正（明确标出被后续工作证伪或修正的早期结论）；4. 主要学术争议、阴性/相反结果与局限性（分学派/分方法对比）；5. 现有研究空白与可验证的后续实验建议。每条关键数值必须标注作者年份、样本条件、单位与【实测/推论/假说】层级，严禁商业报告口吻与拔高确定性，不确定的一手数据与转引声明单独标为待核。" --format custom -n <NB> -s <PRIMARY_ID_1> -s <PRIMARY_ID_2> --language zh_Hans --wait --retry 2
```

### 4. Chronological & Methodological Mind Map (`generate mind-map`)

```bash
notebooklm generate mind-map -n <NB> --language zh_Hans --instructions "按研究年代与技术手段梳理认知演化：早期描述阶段→定量测试阶段→微观/原位精细阶段→保存机制与模型整合"
```
