故障报告好像包含了think-body内容，按预期故障报告是基于前面所有分析形成的最终报告，请分析原因并解决。
# Diagnostics — AI 驱动的系统故障诊断 Agent

一个结合 LLM 与领域 Tool、自动执行 Linux / Kubernetes / GPU 故障排查的智能诊断平台。采用 **Coordinator 初筛 + 7 个 Domain Expert (Skills-First) 深度分析** 架构，假设驱动、证据收束。报告路径由程序预生成（UUID 命名）并通过 Prompt 变量注入，按实体（主机 / K8s 集群）层级化持久归档到文件系统，诊断前自动关联同一运维对象的历史故障。Frontend 3:7 双栏布局：左侧按诊断 Round 展示可折叠的推理过程与 Markdown 报告，右侧实时渲染诊断执行树。

![界面截图 1](static/1.png)
![界面截图 2](static/2.png)
![界面截图 3](static/3.png)
![界面截图 4](static/4.png)
![界面截图 5](static/5.png)

## 目录

- [架构概览](#架构概览)
- [快速开始](#快速开始)
- [持久化报告与历史关联](#持久化报告与历史关联)
  - [设计动机](#设计动机)
  - [文件系统层级组织](#文件系统层级组织)
  - [报告命名规范](#报告命名规范)
  - [历史关联流程](#历史关联流程)
  - [关键实现](#关键实现)
  - [历史诊断流程图持久化](#历史诊断流程图持久化)
  - [设计动机](#设计动机-1)
  - [数据结构](#数据结构)
  - [关键实现](#关键实现-1)
  - [API 端点](#api-端点)
  - [前端数据流转](#前端数据流转)
  - [历史报告页面 UI](#历史报告页面-ui)
  - [Markdown 渲染](#markdown-渲染)
- [动态诊断图：原理与实现](#动态诊断图原理与实现)
  - [设计哲学](#设计哲学)
  - [核心数据结构](#核心数据结构)
  - [生命周期与状态机](#生命周期与状态机)
  - [Phase（轮次）创建策略](#phase轮次创建策略)
  - [节点描述：完全由 LLM 驱动](#节点描述完全由-llm-驱动)
- [数据流路径](#数据流路径)
  - [端到端时序](#端到端时序)
  - [事件类型一览](#事件类型一览)
- [后端关键实现](#后端关键实现)
  - [事件流处理（streaming.py）](#事件流处理)
  - [诊断树构建器（step_tracker.py）](#诊断树构建器)
  - [SSE 端点（app.py）](#sse-端点)
  - [Agent 工厂（factory.py）](#agent-工厂)
- [前端关键实现](#前端关键实现)
  - [诊断树可视化](#诊断树可视化)
  - [思考过程折叠面板](#思考过程折叠面板)
  - [工具详情弹窗](#工具详情弹窗)
- [技能选择器](#技能选择器)
- [项目结构](#项目结构)
- [配置](#配置)
- [License](#license)

## 架构概览

```mermaid
flowchart LR
    User["User"] -->|"POST /api/chat/stream"| FastAPI["FastAPI SSE Endpoint"]
    FastAPI -->|"Initialize Tree"| Tree["TreeBuilder"]
    FastAPI -->|"Start Agent Stream"| Stream["stream_agent_events()"]
    Stream -->|"LLM Request"| LM["LM Studio / OpenAI API"]
    Stream -->|"text_delta Event"| SSE["SSE Formatter"]
    Stream -->|"tool_start / tool_end"| SSE
    Stream -->|"round_number"| SSE
    Tree -->|"tree_snapshot"| SSE
    SSE -->|"SSE Event Stream"| Frontend["Frontend (app.js)"]
    Frontend -->|"phase=thinking → Collapsible Panel"| Chat["Chat Panel (Left)"]
    Frontend -->|"Tree Nodes + SVG Edges"| Graph["Diagnosis Graph (Right)"]
    Stream -->|"Text Buffer"| Finalize["_finalize()"]
    Finalize -->|"phase=answering → Report"| Chat
```

**数据流分两路**：推理过程中的 `text_delta(phase=thinking)` 流入左侧按 Round 创建的折叠块（含 LLM 推理、🔧 Tool Call、📊 Tool Result）；Stream 结束时 `_finalize()` 将 Coordinator 最后一轮的 Buffer 文本以 `phase=answering` 发送到回答区，形成最终诊断报告。

技术栈：

| 层 | 技术 |
|---|---|
| Agent 框架 | `deepagents` — 支持 Subagent 委派、文件系统 Backend、Skill 加载 |
| LLM 接入 | `langchain-openai` — 兼容 OpenAI API 的本地/远程模型 |
| Web 框架 | `FastAPI` — SSE 流式响应 |
| Frontend | 原生 HTML + CSS + JavaScript，零构建工具 |
| 可视化 | SVG 边线 + 自定义 BFS 层级布局 |
| 运行环境 | `uvicorn` ASGI 服务器 |

## Hypothesis-Driven Diagnosis Ledger

诊断 Agent 采用**假设驱动诊断机制**：维护结构化的诊断台账（Diagnosis Ledger），通过假设形成 → 验证 → 评估 → 报告的核心循环逐步逼近根因，而非固定步骤数的线性流程。

### 核心概念

| 概念 | 说明 |
|------|------|
| **DiagnosisLedger** | 诊断台账 — 持久化在 LangGraph State 中的结构化诊断记忆 |
| **Hypothesis Tree** | 假设树 — 最多 3 个/层，支持多级子假设深化 |
| **Active Path** | 活动路径 — 当前探索路径栈，支持回溯 |
| **Phase** | 诊断阶段 — `understand` → `hypothesize` → `verify` → `evaluate` → `report` → `backtrack` |
| **Exit Conditions** | 退出条件 — 根因确认(p≥80%) / 假设穷尽 / 证据饱和(3×inconclusive) |

### 数据流

```
awrap_model_call (每次LLM调用前)
  ├── 获取当前 ledger (self._current_ledger > request.state > new_ledger)
  ├── render_ledger_context() → 注入 system message
  └── 等待 LLM 响应 → ExtendedModelResponse → 同步回 state

awrap_tool_call (每次工具调用后)
  ├── 诊断工具 → 自动记录证据到活动假设
  ├── record_round() 记录步骤
  └── 台账工具 → 持久化 + stream_writer 推送 ledger_snapshot
```

### 三个台账管理工具

| 工具 | 调用阶段 | 作用 |
|------|---------|------|
| `commit_hypotheses` | HYPOTHESIZE | 提交 ≤3 个假设（按概率降序），自动选择最高概率进入 verifying |
| `select_path` | EVALUATE | 选择 1 条路径深入，未选中自动 deprioritized（可回溯） |
| `record_finding` | VERIFY 后 | 记录 confirmed/refuted/inconclusive，自动检查退出条件 |

### 前端假设树

右侧面板提供"执行树"和"假设树"两个标签页。假设树实时渲染假设节点的层级结构：状态、概率条、支持/反驳证据、验证工具，以及"当前聚焦"标记。

### 退出条件（进入 REPORT 的判定）

满足以下任一即进入报告阶段，不依赖固定步骤数：
1. **根因确认**：某假设 confirmed 且 probability≥80%，假设已足够具体
2. **假设穷尽**：所有可探索路径（含回溯）都 refuted/dead_end
3. **证据饱和**：连续 3 次 record_finding 返回 inconclusive
4. **无合理假设**：EVALUATE 阶段无法提出合理的新假设

## 快速开始

```bash
# 安装依赖
pip install -e .

# 配置环境变量
cp .env.example .env
# 编辑 .env，设置 DIAGNOSTICS_BASE_URL 指向你的 LLM 服务（如 LM Studio）

# 启动服务
python main.py
# 访问 http://127.0.0.1:8000
```

## 持久化报告与历史关联

### 设计动机

运维诊断不是一次性事件——同一个主机或 K8s 集群可能反复出现相同或关联的故障。本系统将每次诊断报告**按运维实体层级化持久存储**，并在诊断前自动读取该实体的历史故障记录，使 Coordinator 能够：
- 识别是否属于历史故障复发
- 对比当前异常指标与历史特征信号
- 优先排查已被证实的历史根因
- 在全局经验库（LEARNINGS.md）中积累跨实体的通用诊断模式

### 文件系统层级组织

报告按**实体类别** → **实体名称** → **日期+症状** 三级结构归档。GPU 以 K8s 形态提供，归属到所在集群目录：

```
agent_data/reports/
├── hosts/                                   ← 传统物理/虚拟机
│   ├── server01/
│   │   ├── HISTORY.md                       ← 该主机故障历史摘要
│   │   ├── 2026-06-08_server01_oom-killed.md
│   │   └── 2026-06-09_server01_cpu-load-high.md
│   └── db-node-03/
│       ├── HISTORY.md
│       └── 2026-06-09_db-node-03_disk-io-saturation.md
│
└── kubernetes/                              ← 所有 K8s 集群（含 GPU）
    ├── prod-cluster/
    │   ├── HISTORY.md                       ← 该集群故障历史摘要
    │   ├── 2026-06-09_prod-cluster_pod-java-backend-crashloop.md
    │   ├── 2026-06-09_prod-cluster_node-worker3-notready.md
    │   └── 2026-06-09_prod-cluster_gpu-gpu01-memory-oom.md
    └── staging-cluster/
        ├── HISTORY.md
        └── ...
```

### 报告命名规范

```
{YYYY-MM-DD}_{entity_name}_{brief-symptom}.md
```

| 组成部分 | 说明 | 示例 |
|---|---|---|
| `YYYY-MM-DD` | 诊断日期，便于按时间搜索 | `2026-06-09` |
| `entity_name` | 主机名或集群名，便于按实体过滤 | `server01`, `prod-cluster` |
| `brief-symptom` | 英文简短症状（小写，连字符分隔） | `cpu-load-high`, `gpu-memory-oom` |

搜索示例：
- 按时间过滤：`ls reports/hosts/server01/2026-06*`
- 按实体列出所有报告：`ls reports/kubernetes/prod-cluster/`
- 跨实体搜索根因：`grep -r "OOMKilled" reports/`

### 历史关联流程

以下一次完整的"带历史追溯"诊断请求为例，展示 Coordinator 如何关联历史故障：

```mermaid
flowchart TD
    Input["用户输入: 'prod-cluster 集群 Pod java-backend 频繁重启'"]
    Input --> Step1["Step 1: 实体识别<br/>category=kubernetes<br/>entity=prod-cluster"]
    Step1 --> Step2["Step 2: 读取历史<br/>read_file reports/kubernetes/prod-cluster/HISTORY.md"]
    Step2 --> Step3{"历史命中?"}
    Step3 -->|"是: 3天前 worker2 有过 MemoryPressure"| Step4["Step 3: 将历史模式注入诊断上下文<br/>委派专家时提示关联历史案例"]
    Step3 -->|"否: 无历史记录"| Step5["Step 3: 首次诊断该实体<br/>执行完整5轮诊断流程"]
    Step4 --> Step5
    Step5 --> Step6["Step 4: 第5轮收束<br/>write_file 层级归档路径"]
    Step6 --> Step7["Step 5: 更新 HISTORY.md<br/>追加本次诊断摘要"]
    Step7 --> Step8["Step 6: 全局自我进化<br/>更新 LEARNINGS.md"]
```

**设计了两个层次的记忆**：
- **实体级记忆（HISTORY.md）**：同一运维对象的故障时间线，精确到主机/集群
- **全局级记忆（LEARNINGS.md）**：跨实体的通用诊断模式和方法论积累

### 关键实现

**1. FilesystemBackend 天然支持**

现有的 `CompositeBackend` 已将 `/agent_data/` 路由到 `FilesystemBackend`，Agent 通过 `write_file("/agent_data/reports/hosts/server01/...")` 写入的报告直接落盘持久化，无需任何后端改动。

**2. Prompt 驱动，零代码新增**

实体识别、路径规划、历史读取、HISTORY.md 更新等全部逻辑通过 `prompt.py` 中的任务指令驱动，无需新增 Python 模块。Coordinator 在诊断开始时被指令：
- 提取运维对象 → 确定归档路径
- 读取 HISTORY.md → 注入诊断上下文
- 报告写入选定路径 → 更新 HISTORY.md → 更新 LEARNINGS.md

**3. 目录自动初始化**

`factory.py` 在 Agent 构建时自动创建 `reports/hosts/` 和 `reports/kubernetes/` 目录，确保 Agent 首次写入时路径已就绪。

**4. 报告路径程序生成与变量注入**

报告路径由程序预生成，不依赖 LLM。流程如下：

```
Frontend 选择实体类型 + 输入实体名
  → POST /api/chat/stream (entity_type + entity_name)
    → app.py: _make_report_path() 生成 UUID 路径
      格式: /agent_data/reports/{hosts|kubernetes}/{entity}/{YYYY-MM-DD-HHmmss}-{uuid8}.md
    → make_system_prompt(report_path) 将路径渲染进 SYSTEM_PROMPT
    → build_agent(system_prompt=...) 创建携带精确路径的 Agent
    → Agent 直接使用 prompt 中的 {report_path} 写入报告
      （无需 LLM 自行总结症状或拼接文件名）
```

关键技术点：
- `prompt.py` 中报告路径用 `{report_path}` 占位，`make_system_prompt(report_path)` 在每次请求时格式化模板
- `factory.py` 的 `build_agent()` 接受可选 `system_prompt` 参数，按请求动态构建 Agent
- 文件名使用 `日期-时间-UUID` 格式，避免依赖 LLM 的症状总结，保证唯一且可追溯

### 历史诊断流程图持久化

除报告文本外，每次诊断的**完整执行树（流程图数据）**也随报告一同保存，支持事后回放诊断过程。

#### 设计动机

实时诊断图在 Page 刷新后即丢失——运维人员需要在事后复盘诊断路径、对比不同时间的诊断策略，或在报告页面中直观查看 Agent 的执行链路。因此诊断树数据结构在诊断结束时与报告一同序列化归档。

#### 数据结构

报告目录中同时存在两种文件：

```
agent_data/reports/kubernetes/prod-us-east/
├── 2026-06-17-201851-6bc163c0.md              # Markdown 诊断报告
└── 2026-06-17-201851-6bc163c0.result.json     # 诊断流程图 + 元数据
```

`result.json` 格式：

```json
{
  "entity_type": "kubernetes",
  "entity_name": "prod-us-east",
  "duration_secs": 126.5,
  "assistant_text_chars": 4793,
  "event_counts": { "text_delta": 810, "tool_start": 44, "tool_end": 44 },
  "report_file": "kubernetes/prod-us-east/2026-06-17-201851-6bc163c0.md",
  "tree": {
    "steps": [
      {
        "id": "n1",
        "title": "第1轮智能分析",
        "parent_id": null,
        "parent_ids": [],
        "status": "completed",
        "node_type": "phase",
        "description": "",
        "tool_name": "",
        "tool_args": ""
      }
    ]
  }
}
```

#### 关键实现

**1. 序列化（`app.py` → `_save_result()`）**

诊断流结束时，`TreeBuilder` 的完整节点数据通过 `_serialize_tree()` 提取为可序列化的字典列表，字段 `status` 和 `node_type` 取其 `.value`（字符串），与 Markdown 报告一起写入文件系统：

```python
def _save_result(report_path, tree, entity_type, entity_name, duration, ...):
    data = {
        "entity_type": entity_type,
        "entity_name": entity_name,
        "duration_secs": round(duration, 1),
        "event_counts": dict(event_counters),
        "tree": {"steps": _serialize_tree(tree)},
    }
    result_path = stem.with_suffix(".result.json")
    result_path.write_text(json.dumps(data, ensure_ascii=False))
```

**2. API 端点**

| 端点 | 用途 | 返回 |
|---|---|---|
| `GET /api/history` | 扫描 `reports/` 目录，列出所有历史会话 | 列表（含 `entity_type`、`entity_name`、`duration_secs`、`tree` 等） |
| `GET /api/history/{result_file}` | 获取单个会话的完整诊断树 | `result.json` 全部内容 |
| `GET /api/report/{report_file:path}` | 获取 Markdown 报告原文 | `{ "content": "..." }` |

历史列表同时涵盖有 `result.json` 的完整记录和仅有 `.md` 的旧格式报告（后者无诊断图），按时间倒序排列。

**3. 前端数据流转**

历史页面的渲染分为两个独立过程：

```
用户点击历史条目
  → openHistory(item)
    ├── GET /api/report/{report_file} → renderMarkdown() → 左侧报告面板
    └── item.tree.steps → renderHistoryGraph() → 右侧诊断图
```

诊断树的反序列化需要注意字段命名转换——后端 Python 使用 `snake_case`（`parent_id`、`node_type`、`tool_name`），前端 JS 使用 `camelCase`（`parentId`、`nodeType`、`toolName`）。`renderHistoryGraph()` 在构建历史图节点时显式映射：

```javascript
hg.nodes.set(s.id, {
  id: s.id,
  title: s.title,
  parentId: s.parent_id || null,    // ← 蛇形→驼峰
  parentIds: parentIds,
  nodeType: s.node_type || "tool",  // ← 蛇形→驼峰
  toolName: s.tool_name || "",      // ← 蛇形→驼峰
  toolArgs: s.tool_args || "",
  status: "completed",              // ← 历史节点统一标记为已完成
  ...
});
```

**4. 历史报告页面 UI**

进入历史页面后，右侧原"执行过程"面板切换为历史查看器：

- **顶部工具栏**：返回按钮 + 诊断标题（实体类型/名称 · 耗时）
- **左半区**：Markdown 报告，支持标题、粗斜体、代码块、表格、有序/无序列表、引用块等完整 GFM 语法渲染，行内单个换行自动转为 `<br>`
- **右半区**：诊断流程图，使用与实时诊断共用的 BFS 层级布局、`createNodeDOM()` 节点渲染和 SVG 贝塞尔曲线边线绘制。所有节点绿色标记为"已完成"状态
- **拖拽分割线**：支持拖动中间分割条调整左右面板比例（20%–80%）

**5. Markdown 渲染**

`renderMarkdown()` 是共享函数，同时服务于实时诊断和历史报告。它按四阶段处理：

| 阶段 | 处理内容 | 示例 |
|---|---|---|
| Phase 1 | 代码块、行内代码、标题（h1-h3）、粗体、斜体、链接、引用块 | `` `code` `` → `<code>`, `# Title` → `<h1>` |
| Phase 2 | 表格检测 | `\| A \| B \|` 连续行 → `<table><thead><tbody>` |
| Phase 3 | 有序/无序列表 | `- item` → `<ul><li>`, `1. item` → `<ol><li>` |
| Phase 4 | 段落包裹、换行处理、标签清理 | `\n\n` → `</p><p>`, `\n` → `<br>` |

输入文本在 Phase 1 之前先经 `escNoBr()` 转义 `<` `>` `&` `"` 防止 XSS 注入。

---

## 动态诊断图：原理与实现

本项目最核心的特性是**右侧面板中随诊断过程实时生长的执行树**。它不是静态图示，而是一个与 Agent ↔ LLM 交互周期同步更新的动态数据结构。

### 设计哲学

诊断树完全由 **LLM 的实时输出驱动**，而非 Backend 硬编码的步骤模板。核心理念：

- **不预设诊断路径**：Agent 根据 Tool 返回的数据，自主决策下一步检查什么
- **LLM 提供语义描述**：每个节点的标题和描述来自 LLM 的思考文本，而非固定字符串
- **树结构反映实际推理**：父子关系代表了 Agent 的真实决策链

### 核心数据结构

诊断树由三种节点类型构成：

```python
class NodeType(Enum):
    ROOT  = "root"    # 保留定义，实际未使用——Phase 直接作为根节点
    PHASE = "phase"   # Round 节点："第N轮智能分析"
    TOOL  = "tool"    # Tool 节点："分析CPU指标"、"排查Pod状态"等
    TASK  = "task"    # Task 委派节点（预留扩展）
```

树的结构层次（以两轮诊断为例）：

```mermaid
graph TD
    P1["Round 1 Analysis<br/>（Driven by LLM thinking）"]
    P1 --> T1["get_system_overview<br/>（pending → running → completed）"]
    P1 --> T2["check_cpu"]
    P1 --> T3["check_kubernetes_pods"]

    P2["Round 2 Analysis<br/>（parent_ids from Round 1 Tool nodes）"]
    P2 --> T4["Delegate cpu-expert<br/>（task Subagent）"]
    P2 --> T5["Delegate k8s-workload-expert"]

    T1 -.-> P2
    T2 -.-> P2
    T3 -.-> P2
```

**Phase 节点直接作为诊断树的根**：树启动时 `tree.start()` 直接创建第 1 Round Phase（`parent_ids=[]`），后续每 Round Phase 的 `parent_ids` 指向上一 Round 所有 Tool 子节点，形成链式因果连接。不再有独立的"开始诊断"根节点。

每个节点携带的字段：

| 字段 | 说明 |
|---|---|
| `id` | 唯一标识（如 `n1`, `n2`） |
| `title` | 中文显示名称（Tool 名→中文映射表） |
| `parent_id` / `parent_ids` | 父节点引用（PHASE 节点支持多父） |
| `status` | `pending` / `running` / `completed` / `error` |
| `description` | 由 LLM 输出驱动，描述此步骤的目的 |
| `tool_name` / `tool_args` | Tool 函数名和参数，Frontend 展示为 `check_cpu(profile="default")` |

### 生命周期与 State Machine

`TreeBuilder` 内部维护一个有限 State Machine，每个转换由外部 Event 触发：

```mermaid
stateDiagram-v2
    [*] --> init : TreeBuilder created
    init --> thinking : start() / handle_round_start() — Create Phase
    thinking --> thinking : handle_token() — Accumulate LLM text
    thinking --> executing : handle_tool_call() — LLM issues Tool Call
    executing --> executing : handle_update() — Tools completing
    executing --> thinking : handle_round_start() — New Round starts
    executing --> thinking : handle_update() — All tools done, Phase completed
    thinking --> done : finalize() — Stream ends, create "诊断完成"
    executing --> done : finalize()
```

**State 转换要点**：
- `thinking`：LLM 正在流式输出文本，Phase 为 RUNNING
- `executing`：Tool 正在执行，`handle_tool_call()` 在当前 Phase 下挂载 Tool 子节点
- 新 Phase 由 `handle_round_start()` 创建——当 LangGraph 进入下一轮 model 节点时触发
- 诊断结束由 `finalize()` 显式创建 **"诊断完成"** 终态节点

### Phase（Round）创建策略

整个诊断由多个 **Round** 组成，每 Round 对应一次 LangGraph **model 节点进入**。Phase 创建采用**事件驱动**模式，不再依赖 token 流检测：

```mermaid
flowchart TD
    START["Agent Start"] -->|tree.start()| P1["Phase 1 RUNNING"]
    START -->|"LangGraph stream"| M1["model node (#1)"]
    M1 -->|"LLM text + tool_calls"| T1["tools node → tool_end"]
    T1 -->|"Tools return, next model entry"| M2["model node (#2)"]
    M2 -->|"round_start event"| P2["Phase 1 COMPLETED<br/>Phase 2 RUNNING"]
    M2 --> T2["tools node"]
    T2 -->|"..."| MN["model node (#N)"]
    MN -->|"round_start event"| PN["Phase N RUNNING"]
    MN -->|"Stream End"| END["finalize() → '诊断完成'"]
```

**关键流程**：
1. `streaming.py` 检测到 LangGraph `tools → model` 节点转换 → emit `round_start` 事件
2. `app.py` 收到 `round_start` → `tree.handle_round_start(N)` → Phase N-1 COMPLETED + Phase N RUNNING
3. Phase 创建与 LLM 调用 1:1 对应，语义清晰

**parent_ids 继承**：新 Phase 的 `parent_ids` 指向上一 Round 所有 Tool 子节点，形成 BFS 层级布局中的因果链连线。

### 报告内容分发

报告通过 **`write_file` 捕获**机制分发，而非文本解析：

```
LLM → write_file(report_path, content="# 故障诊断报告...")
  → middleware 检测 write_file 命中 report_path
    → stream_writer → custom event "report_content"
      → streaming.py → text_delta(phase="answering")
        → 前端 answer-body
        → app.py report_text → .md 文件（仅报告）
```

`assistant_text`（全量文本）用于 Agent 跨会话记忆；`report_text`（仅 answering）用于持久化 `.md` 文件。

## 数据流路径

### 端到端时序

以下是一次完整诊断请求中各组件的交互时序：

```
┌──────────┐    ┌───────────┐    ┌──────────────────┐    ┌──────────────┐    ┌─────┐
│  Browser │    │  FastAPI  │    │  stream_agent_   │    │  TreeBuilder │    │ LLM │
│ (app.js) │    │  (app.py) │    │  events()        │    │              │    │     │
└────┬─────┘    └─────┬─────┘    └────────┬─────────┘    └──────┬───────┘    └──┬──┘
     │                │                   │                     │               │
     │ POST /api/chat │                   │                     │               │
     │───────────────>│                   │                     │               │
     │                │ tree.start()      │                     │               │
     │                │──────────────────>│                     │               │
     │  SSE session   │                   │ tree_snapshot(phase)│               │
     │<───────────────│                   │<────────────────────│               │
     │                │                   │                     │               │
     │                │ agent.astream()   │                     │               │
     │                │──────────────────>│                     │               │
     │                │                   │ ── LLM chunk ──>    │               │
     │                │                   │                     │  "CPU usage   │
     │                │                   │                     │   high..."    │
     │                │                   │ <── text delta ──   │               │
     │                │                   │                     │               │
     │                │                   │ handle_token(text)  │               │
     │                │                   │────────────────────>│               │
     │ SSE text_delta │                   │                     │               │
     │<───────────────│                   │                     │               │
     │                │                   │                     │               │
     │                │                   │ ── AIMessage ──>    │               │
     │                │                   │   tool_calls=[...]  │               │
     │                │                   │ <── tool_calls ──   │               │
     │                │                   │                     │               │
     │                │                   │ parse <step> tags   │               │
     │                │                   │ build descriptions  │               │
     │                │                   │                     │               │
     │                │                   │ AgentEvent(         │               │
     │                │                   │   "tool_start",     │               │
     │                │                   │   description=...)  │               │
     │                │                   │                     │               │
     │                │                   │ handle_tool_call()  │               │
     │                │                   │────────────────────>│               │
     │                │                   │                     │ create Phase  │
     │                │                   │                     │ + Tool nodes  │
     │                │                   │ tree_snapshot       │               │
     │                │                   │<────────────────────│               │
     │                │                   │                     │               │
     │ SSE tree_      │                   │                     │               │
     │ snapshot +     │                   │                     │               │
     │ tool_start     │                   │                     │               │
     │<───────────────│                   │                     │               │
     │                │                   │                     │               │
     │ ── Frontend renders tree ──>       │                     │               │
     │                │                   │                     │               │
     │                │                   │ ── Tool executes ─> │               │
     │                │                   │ <── ToolMessage ──  │               │
     │                │                   │                     │               │
     │                │                   │ AgentEvent(         │               │
     │                │                   │   "tool_end", ...)  │               │
     │                │                   │                     │               │
     │                │                   │ handle_update()     │               │
     │                │                   │────────────────────>│               │
     │                │                   │                     │ all done →    │
     │                │                   │                     │ next Phase    │
     │                │                   │ tree_snapshot       │               │
     │                │                   │<────────────────────│               │
     │                │                   │                     │               │
     │ SSE tool_end + │                   │                     │               │
     │ tree_snapshot  │                   │                     │               │
     │<───────────────│                   │                     │               │
     │                │                   │                     │               │
     │  ... Multi-Round loop repeats ...  │                     │               │
     │                │                   │                     │               │
     │                │                   │ _finalize()         │               │
     │                │                   │ tree.finalize()     │               │
     │                │                   │────────────────────>│               │
     │                │                   │                     │ mark done     │
     │                │                   │ tree_snapshot       │               │
     │                │                   │<────────────────────│               │
     │                │                   │                     │               │
     │ SSE done +     │                   │                     │               │
     │ tree_snapshot  │                   │                     │               │
     │<───────────────│                   │                     │               │
     │                │                   │                     │               │
     │ ── finalizeGraph() ──>             │                     │               │
```

### Event 类型一览

| SSE Event | 触发时机 | Frontend 行为 |
|---|---|---|
| `session` | 连接建立 | 保存 session_id |
| `text_delta` | LLM 输出文本 / Tool Result / write_file 报告 | phase=thinking → 追加到 "第N轮智能分析" 折叠块；phase=answering → 追加到诊断报告区 |
| `round_start` | LangGraph 进入新 model 节点 | → `handle_round_start()` 创建新 Phase |
| `ledger_snapshot` | 台账工具调用后 | 更新假设树面板 |
| `tree_snapshot` | 树结构发生变化 | **完全重建诊断树**：重新计算 BFS 层级、渲染节点、绘制 SVG 边线 |
| `tool_start` | LLM 发出 Tool Call | 信息性 Event（树由 tree_snapshot 驱动） |
| `tool_end` | Tool 执行完毕 | 信息性 Event（树由 tree_snapshot 驱动） |
| `tool_args_available` | 流式参数到达（updates 模式补全） | 更新 Tool 节点的参数显示 |
| `agent_start` / `agent_end` | Subagent 启动/结束 | 信息性 Event |
| `done` / `cancelled` / `error` | Stream 终止 | 折叠未完成面板、标记完成状态 |

## 后端关键实现

### 事件流处理（`diagnostics/agent/streaming.py`）

核心函数 `stream_agent_events()` 负责将 `deepagents` 的原始流式输出转换为结构化的 `AgentEvent` 序列。

**并发模型**：

```python
async def stream_agent_events(agent, messages, cancel_event, session_id):
    chunk_queue = asyncio.Queue()  # 解耦 Producer 和 Consumer

    # Producer: 将 agent.astream() 的原始数据放入队列
    async def _producer():
        async for raw in agent.astream(...):
            await chunk_queue.put(raw)

    # Consumer: 从队列取数据，用 asyncio.wait 同时监听取消信号
    while True:
        get_task = asyncio.ensure_future(chunk_queue.get())
        cancel_task = asyncio.ensure_future(cancel_event.wait())
        done, pending = await asyncio.wait(
            {get_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done:
            yield AgentEvent("cancelled", ...)
            return
        raw = await get_task
        for evt in _process_chunk(raw, state, session_id):
            yield evt
```

**LangGraph 节点检测与 Round 管理**：

`_process_chunk()` 从 `stream_mode="messages"` 的 metadata 中提取 `langgraph_node` 字段，检测 Coordinator 是否进入 `"model"` 节点。每次 model 节点转换代表一次新的 LLM 调用，`round_number` 在文本处理之前递增，确保所有 `text_delta` Event 携带正确的 Round 号。

```python
node_type = metadata.get("langgraph_node", "")
if is_coordinator and node_type == "model":
    if state._last_node_type != "model":
        state.round_number += 1  # 新 LLM 调用 → 新 Round
```

**Tag 解析与文本路由**：

1. **`_parse_step_tags()`** — 最先执行，提取 `<step>` Tag 声明。当 LLM 纯输出 `<step>` Tag（无其他文本）时，将步骤文本也发送到 Frontend，用 `new_steps_added` 标志避免流式传输中每个中间 chunk 重复发送。

2. **`_parse_think_tags()`** — 解析 `<think>...</think>` 块，区分深度思考内容和最终回答。

3. **文本路由** — Coordinator 的中间推理文本仅发送到 `phase="thinking"`（创建折叠块），并累积到 `_coordinator_text` Buffer。Stream 结束时 `_finalize()` 将 Buffer 内容发送到 `phase="answering"`（回答区正文）。Tool Call 痕迹（🔧）和 Tool Result（📊）同样 Route 到 `phase="thinking"`，归入当前 Round 折叠块。

**Subagent 的 Phase 路由规则**：

```python
def _subagent_phase(path, state) -> str:
    # 所有 Subagent 的输出 → thinking（显示为可折叠的推理过程）
    # Coordinator 的最终报告由 _finalize 处理 → answering
    if len(path) <= 1 or path[0] == "coordinator":
        return "answering"  # Coordinator → answer 区域
    return "thinking"       # Subagent → 折叠块
```

**双通道参数提取**：部分 LLM Backend（如 LM Studio/Qwen）在流式模式下，`messages` 模式的 tool_call 中参数为空，完整参数出现在 `updates` 模式。`_process_chunk()` 同时处理两种模式：

```python
if mode == "messages":
    # 主通道：可能收到空的 tool_call args
    ...
elif mode == "updates":
    # 补全通道：从 LangGraph state 中提取完整参数
    for msg in value["messages"]:
        tcs = getattr(msg, "tool_calls", None)
        if tcs:
            for tc in tcs:
                # 如果 active_tools 中已有记录但 args 为空，补全
                if info and not info.get("args"):
                    info["args"] = tc_args
                    events.append(AgentEvent("tool_args_available", ...))
```

### TreeBuilder（`diagnostics/server/step_tracker.py`）

`TreeBuilder` 是一个**被动响应式**构建器，它在接收到 Event 时同步更新内部树 State。

```python
@dataclass
class TreeBuilder:
    nodes: dict[str, TreeNode]           # id → TreeNode
    node_order: list[str]                # 保持插入顺序
    state: str = "init"                  # State Machine
    _current_phase_id: str               # 当前活跃 Phase
    _last_tool_child_ids: list[str]      # 上一 Round 创建的 Tool 节点 ID
    think_buffer / think_segments        # LLM 思考文本累积
    answer_buffer                        # LLM 回答文本累积
```

**Snapshot 生成**：每次树 State 变更时，调用 `_snapshot_event()` 生成完整的节点列表 Snapshot：

```python
def _snapshot_event(self) -> dict[str, Any]:
    return {
        "type": "tree_snapshot",
        "steps": [
            {
                "id": node.id,
                "title": node.title,
                "parent_id": node.parent_id,
                "parent_ids": node.parent_ids,
                "status": node.status.value,     # "pending"|"running"|"completed"|"error"
                "node_type": node.node_type.value, # "phase"|"tool"|"task"
                "description": node.description,   # LLM 驱动的描述
                "tool_name": node.tool_name,
                "tool_args": node.tool_args,
            }
            for nid in self.node_order
        ],
    }
```

Frontend 接收到 `tree_snapshot` Event 时，**完全重建**可视化树，而非增量更新。这种设计简化了前后端同步，避免增量 diff 的复杂性。

### SSE Endpoint（`diagnostics/server/app.py`）

`_chat_event_stream()` 是 SSE Event 的 Producer。它协调三个关键组件：

```python
async def _chat_event_stream(request, session_id, state, agent, settings):
    tree = TreeBuilder()

    # 1. 发送 session_id
    yield sse("session", {"session_id": session_id})

    # 2. 初始化树（创建首个 Phase 节点）
    for snap in tree.start():
        yield sse("tree_snapshot", snap)

    # 3. 主循环：消费 Agent Event，同时驱动 TreeBuilder
    async for event in stream_agent_events(...):
        if event.name == "text_delta":
            yield sse("text_delta", event.payload)
            # 将 text token 也送入 TreeBuilder
            for tok_evt in tree.handle_token(event.payload["text"]):
                ...

        elif event.name == "tool_start":
            # 将 Tool Call 送入 TreeBuilder（带 LLM 描述）
            tool_calls = [{
                "id": event.payload["id"],
                "name": event.payload["name"],
                "args": event.payload.get("args", {}),
                "description": event.payload.get("description", ""),
            }]
            for snap in tree.handle_tool_call(tool_calls):
                yield sse("tree_snapshot", snap)

        elif event.name == "tool_end":
            # 标记 Tool 完成 → 可能触发新 Phase 创建
            for snap in tree.handle_update(...):
                yield sse("tree_snapshot", snap)

    # 4. Finalize：关闭树，发送最终 Snapshot
    for snap in tree.finalize():
        yield sse("tree_snapshot", snap)

    yield sse("done", {"session_id": session_id})
```

### Agent 工厂（`diagnostics/agent/factory.py`）

使用 `deepagents` 的 `create_deep_agent()` 构建主 Agent，核心配置如下：

- **7 个 Subagent**（系统层 + K8s 层）：
  - 系统层：`cpu-expert`, `memory-expert`, `disk-io-expert`, `network-expert`, `gpu-expert`
  - K8s 层（二合一，遵循 Kubernetes 控制面/数据面边界）：
    - `k8s-cluster-expert` — 集群基础设施：控制面 + 节点 + CoreDNS + etcd + 跨层级联
    - `k8s-workload-expert` — 工作负载：Pod/Deployment/Service/Helm 应用层诊断
- **双层存储 Backend**：`CompositeBackend` — `/agent_data/` 路径 Route 到 `FilesystemBackend`（虚拟文件系统，支持 `read_file`/`write_file`/`edit_file`），其他使用 `StateBackend`（内存 State）。所有诊断报告通过此 Backend 写入 `reports/` 目录层级归档。
- **报告目录初始化**：`build_agent()` 时自动创建 `agent_data/reports/hosts/` 和 `agent_data/reports/kubernetes/` 目录，确保 Agent 首次写入就绪。
- **Memory 与 Skill 加载**：
  - `memory` 路径（`AGENTS.md`、`LEARNINGS.md`）和 `skills` 路径在 `create_deep_agent()` 时固化到 `CompiledStateGraph`
  - 具体文件内容在 `before_agent` Hook 中按需从磁盘读取，缓存于 LangGraph State 中
  - **同 Session 内复用缓存**，无需重复读取；**新 Session**（新 session_id）自动重新从磁盘加载，无需重启服务

```mermaid
flowchart LR
    Create["create_deep_agent()"] -->|"Embed memory/skills paths<br/>into CompiledStateGraph"| Graph["CompiledStateGraph"]

    subgraph S1["First Session Call"]
        B1["before_agent Hook"] -->|"State not cached"| R1["Read files from disk"]
        R1 -->|"Cache to State"| I1["agent.invoke()"]
    end

    subgraph S2["Same Session, later call"]
        B2["before_agent Hook"] -->|"State cached"| SK2["Skip disk read"]
        SK2 --> I2["agent.invoke()"]
    end

    subgraph S3["New Session (new session_id)"]
        B3["before_agent Hook"] -->|"New State, not cached"| R3["Re-read from disk"]
        R3 --> I3["agent.invoke()"]
    end

    Graph --> S1
    Graph --> S2
    Graph --> S3
```

## 前端关键实现

### 诊断树可视化（`static/app.js`）

Frontend 诊断树是一个**自上而下的分层树状图**，核心渲染流程：

**1. BFS 层级计算**：

```javascript
function buildLevels() {
    // 从 parentId 为空的节点（Phase 根节点）开始 BFS
    const roots = [];  // parentId === null 的节点
    const levels = [[...roots]];

    // BFS 遍历，按 parent_ids 关系构建层级
    while (queue.length > 0) {
        const nextLevel = [];
        for (const parentId of currentLevel) {
            for (const id of GRAPH.nodeOrder) {
                if (node.parentIds.includes(parentId)) {
                    nextLevel.push(id);
                }
            }
        }
        levels.push(uniqueNext);
    }
    return levels;
}
```

**2. DOM 渲染**：每个层级渲染为一个 `.tree-level` 弹性容器，层间用 `.tree-connector` 分隔。节点根据类型（Phase/Tool）应用不同的 CSS 类和图标：

```javascript
function createNodeDOM(node) {
    if (node.nodeType === "phase") {
        // ○/🔄/✓ Status 图标 + 标题 + LLM 驱动的描述
    } else {
        // Tool 节点：○/⚙/✓/✕ + 标题 + 函数签名 + 描述
        // 点击触发详情弹窗
    }
}
```

**3. SVG 边线绘制**：使用贝塞尔曲线连接父子节点，Status 驱动样式：

```javascript
function drawAllEdges() {
    // 为每条边计算贝塞尔曲线路径
    // C x1,y1  x1,midY  x2,midY  x2,y2
    const d = `M${x1},${y1} C${x1},${midY} ${x2},${midY} ${x2},${y2}`;

    // Status → 视觉样式
    if (toNode.status === "running") {
        color = "#6c8cff"; dash = 'stroke-dasharray="6 3"';  // 蓝色虚线 + 发光
    } else if (toNode.status === "completed") {
        color = "#4ade80";  // 绿色实线
    } else if (toNode.status === "error") {
        color = "#f87171";  // 红色实线
    }
}
```

边线在 resize 和 scroll Event 时实时重绘，保证拖拽分隔条和滚动画布时连线正确跟随。

**4. 完整重建策略**：每次收到 `tree_snapshot` Event，Frontend 完全重建可视化：

```javascript
function onTreeSnapshot(payload) {
    // 清空本地 GRAPH State
    GRAPH.nodes.clear();
    GRAPH.edges = [];

    // 从 payload.steps 填充
    for (const step of payload.steps) {
        GRAPH.nodes.set(step.id, { ... });
        // 构建边
        GRAPH.edges.push({ from: pid, to: step.id });
    }

    // 完全重建渲染
    renderTree();
}
```

**为什么是完整重建而非增量更新？**
- 树结构可能在任意位置插入新节点（Subagent 调用时）
- BFS 层级可能因新节点加入而整体重排
- 简化同步逻辑，避免前后端 State 不一致
- 渲染性能足够（诊断树节点数通常 < 50）

### 思考过程折叠面板（多 Round 独立，含 Tool Result）

左侧聊天区每 Round LLM 交互产生一个独立折叠块，标题为**"第N轮智能分析"**。

**架构：`thinkSections[]` 数组**

```javascript
// 每个 thinkSection 对象：
{ sectionEl, bodyEl, textEl, labelEl, rawText, startTime, finalized, round }
```

**内容构成：** 每 Round 折叠块包含该 Round 的完整交互内容：
- LLM 推理文本（`phase="thinking"`）
- 🔧 Tool Call 痕迹（`phase="thinking"`）
- 📊 Tool Result（`phase="thinking"`）

**关键行为：**

| 时机 | 行为 |
|---|---|
| Backend 发送 `phase=thinking` | `createThinkSection(round)` → eye SVG + "第N轮智能分析" + chevron，自动展开 |
| 新 Round thinking 到达 | 用 `payload.round` 检测 Round 变化 → `finalizeThink(prev)` → 上一块收起 |
| 用户点击 toggle | 只控制该 section 自身的展开/收起 |
| Stream 结束 | `finishStreamUI()` 最终化未完成的 section；Buffer 的 Coordinator 文本发送到 answer-body |

**DOM 结构：**

```
msg-bubble
├── think-section (Round 1 Analysis, collapsed)
│   ├── think-toggle  [eye SVG] Round 1 Analysis [chevron ▼]
│   └── think-body (hidden) — LLM reasoning + 🔧 Tool Call + 📊 Tool Result
├── think-section (Round 2 Analysis, collapsed)  
│   └── ...
└── answer-section.has-think
    └── answer-body — Final diagnosis report (sent at _finalize with phase=answering)
```

**Round 检测**：Backend 通过 `metadata["langgraph_node"]` 检测 LangGraph 节点转换——Coordinator 每次进入 `"model"` 节点意味着一次新 LLM 调用，在文本处理前递增 `round_number`。Frontend 通过 `payload.round` 与当前 `thinkSections[]` 中最新 section 的 `sec.round` 对比判断新 Round。当检测到新 Round 时，上一 Round section 自动收起（`finalizeThink()`），新 Round section 自动展开。所有中间推理文本（含 `<step>` Tag 解析后的步骤文本）Route 到 `phase="thinking"`，Stream 结束时 `_finalize()` 将 Coordinator 最后一 Round 的 Buffer 文本一次性以 `phase="answering"` 发送到回答区。

### Tool 详情弹窗

点击执行树中的 Tool 节点，弹出详情面板，展示：

- LLM 驱动的步骤描述
- 执行 Status（pending / running / completed / error）
- Tool Call ID（用于调试追踪）

## 技能选择器

在输入框中输入 `/` 即可调出技能列表，支持键盘导航和模糊搜索：

![技能选择](static/select-skill.png)

- **`/`** — 弹出全部 19 个诊断技能
- **继续输入** — 实时过滤，按 ID/名称/描述模糊匹配
- **`↓` `↑`** — 键盘上下选择
- **`Enter`** — 确认选中
- **`Esc`** — 关闭列表
- **选中后** — 以 `@skill:xxx` 前缀注入消息，触发技能驱动诊断模式（严格按 SKILL.md Workflow 步骤执行）

技能数据来自 SQLite 数据库，服务启动时自动从 `agent_data/skills/*/SKILL.md` 同步。

## 项目结构

```
diagnostics/
├── main.py                          # 入口：uvicorn 启动
├── pyproject.toml                   # 依赖声明
├── diagnostics/
│   ├── config.py                    # Settings: 环境变量 → 配置对象
│   ├── logging_config.py            # 日志配置
│   ├── agent/
│   │   ├── factory.py               # create_deep_agent() 构建主Agent+子代理
│   │   ├── prompt.py                # 中文系统提示词
│   │   ├── streaming.py             # 事件流处理：阶段路由、工具分组、台账事件分发
│   │   ├── ledger.py                # 诊断台账：假设树、证据链、阶段推导、退出条件
│   │   ├── ledger_middleware.py      # 台账中间件：上下文注入、write_file报告捕获
│   │   └── consolidation.py         # 学习记忆整理
│   ├── server/
│   │   ├── app.py                   # FastAPI：SSE端点 + 取消 + 树快照协调
│   │   ├── schemas.py               # ChatRequest 模型
│   │   ├── sessions.py              # 内存会话存储
│   │   ├── sse.py                   # SSE 格式化
│   │   ├── step_tracker.py          # TreeBuilder: 树构建器 + 工具标签映射
│   │   └── skills_db.py             # SQLite 技能数据库
│   └── tools/
│       ├── __init__.py
│       ├── registry.py              # 工具注册表
│       ├── live/                    # 生产环境工具
│       │   ├── hosts.py             # 主机级诊断
│       │   ├── gpu.py               # GPU诊断
│       │   ├── kubernetes.py        # K8s kubectl工具
│       │   └── scripts/             # 12个诊断Shell脚本
│       └── mock/                    # 本地测试工具（与live一一对应）
│           ├── data.py              # 场景数据工厂
│           ├── scenarios.py         # 场景管理
│           ├── hosts.py             # 主机级Mock工具
│           ├── gpu.py               # GPU Mock工具
│           ├── kubernetes.py        # K8s Mock工具
│           └── registry.py          # Mock工具注册
├── agent_data/
│   ├── AGENTS.md                    # 诊断方法论（全局记忆）
│   ├── LEARNINGS.md                 # 历史经验（Agent自动更新，跨实体通用模式）
│   ├── skills/                      # 19个专业诊断技能
│   ├── reports/                     # 持久化报告归档（分层级）
│   │   ├── hosts/                   # 主机故障报告
│   │   └── kubernetes/              # K8s 集群故障报告（含 GPU）
│   └── test_cases/                  # 验证测试用例
├── static/
│   ├── index.html                   # 双栏布局页面
│   ├── app.js                       # SSE消费 + 树可视化 + DeepSeek风格多轮折叠块
│   ├── styles.css                   # 完整设计系统
│   └── favicon.svg                  # 站点图标
└── log/                             # 日志目录
```

## 配置

通过 `.env` 文件或环境变量配置：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DIAGNOSTICS_MODEL` | `qwen/qwen3.6-27b` | LLM 模型名称（推荐 Qwen 系列，工具调用稳定） |
| `DIAGNOSTICS_BASE_URL` | `http://127.0.0.1:1234` | LLM API 地址（自动追加 `/v1`） |
| `DIAGNOSTICS_API_KEY` | `lm-studio` | API 密钥（也支持 `LM_STUDIO_API_KEY`、`LM_API_TOKEN`） |
| `DIAGNOSTICS_TEMPERATURE` | `0.2` | LLM 温度参数 |
| `DIAGNOSTICS_MAX_HISTORY_MESSAGES` | `16` | 上下文窗口大小 |

## License

MIT License. See [LICENSE](LICENSE) for details.
