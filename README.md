# web-relay

**English** · [中文](#中文说明)

Let your local AI agent borrow the long context and deep reasoning of a **web LLM** (Gemini / Claude / ChatGPT) for real engineering work — code review, refactoring, algorithm design.

No reverse-engineered APIs. No cookie extraction. **No account ban risk.**

> Your existing web subscription keeps working, your local tools keep working. One `Ctrl+V` / `Ctrl+C` in between.

---

## The problem

Local agents (Claude Code, Cursor, Aider…) are good at execution but limited by their model's context window and reasoning depth. Web LLMs have both, but no access to your files, shell, or test suite.

The usual workaround — a "reverse-engineered gateway" that replays web requests with your cookies — pays for it with:

- **DBSC hardware binding**: exported credentials die within hours and cannot refresh
- **Silent HTTP 200 downgrades**: quota exhausted, the server quietly answers with a weaker model
- **Terms of service violations**: real risk of losing your account

**web-relay takes a different route.** It never touches credentials and never impersonates a browser. A local script packs your context into a structured prompt; *you* paste it into the web UI. When the reply comes back, the script parses structured tags to apply files, run checks, and prepare the next round.

---

## How it works

```
[You: "use web relay to do X"]
        │
        ▼
[Local agent] ── scans context, assembles an XML-contract prompt
        │       writes it to clipboard + archives under .web-relay/
        ▼
[You] ── Ctrl+V into the web UI
        │
        ▼
[Web LLM] ── long context + top-tier reasoning
        │
        ▼
[You] ── copy the reply
        │
        ▼
[Local agent] ── parses <file>/<exec>/<status>/<summary>
        │       guardrails → diff preview → your confirmation
        │       atomic write → run verification commands
        │
        ├── failure / unfinished ──> next-round prompt auto-copied (back to Ctrl+V)
        └── passed ─────────────────> done
```

**The paste is deliberate.** Each round you press the key. That is the security boundary: the web service always sees a genuine human conversation, and your files are always applied after you've read the diff.

---

## Install

### 1. Clone

```bash
git clone https://github.com/Big-mangoo/web-relay.git
```

### 2. Copy the skill into your Claude Code skill directory

**Windows**

```powershell
Copy-Item -Recurse .\skills\web-relay "$env:USERPROFILE\.claude\skills\web-relay"
```

**macOS / Linux**

```bash
mkdir -p ~/.claude/skills
cp -r ./skills/web-relay ~/.claude/skills/web-relay
```

### 3. Verify

```bash
python ~/.claude/skills/web-relay/scripts/gemini_relay.py selftest
```

Expected: `自测全部通过：66/66 项` (all 66 self-tests passed).

Requires Python 3.8+. No third-party packages.

---

## Usage

Inside Claude Code, just say:

```
use web relay to add retry logic to src/parser.py
```

The agent bundles the context and tells you to paste. Then:

1. Switch to the web UI, `Ctrl+V`, send
2. Wait for the reply, click "copy full response"
3. Switch back and say "copied"

Or drive it manually:

```bash
# Pack (writes clipboard + archive)
python <skill-dir>/scripts/gemini_relay.py pack --root <project> -f src/ -p "task description"

# Catch (read clipboard → verify → archive → diff preview → apply → run checks)
python <skill-dir>/scripts/gemini_relay.py catch --root <project>
```

---

## Commands

| Command | Purpose |
|---|---|
| `pack` | Bundle context; write to clipboard and archive |
| `catch` | Read the reply, apply files, run verification commands |
| `relay` | `pack` + `catch` chained |
| `selftest` | Built-in regression suite |

Common flags:

| Flag | Meaning |
|---|---|
| `--root` | Project root (defaults to current directory) |
| `-f` | Files or directories to bundle (repeatable) |
| `-p` | Task description |
| `--once` | Handle a single round |
| `--no-exec` | Apply files but skip `<exec>` commands |
| `--include-secrets` | Allow `.env`, `*.pem`, etc. (**blocked by default**) |
| `--timeout` | Per-command timeout in seconds (default 600) |

---

## Safety

| Risk | Mitigation |
|---|---|
| Model writes to a malicious path | `safe_target` rejects absolute paths, `..` traversal, and `.web-relay/` |
| Model suggests destructive commands | Denylist for `rm -rf`, `format`, `diskpart`, and similar |
| Credential leakage | Skips `.env`, `*.pem`, `*.key`, `id_rsa*`, `*credentials*.json` by default |
| Tag literals inside code executed as real tags | `<file>` spans are masked; `<exec>` inside a file body is ignored |
| Forgot to copy the reply | Clipboard echo check via hash — rejects the outbound prompt |
| Overwrite destroys code | Pre-write backup to `.web-relay/backups/`, diff preview, manual confirmation |
| Invisible characters from browser copy | NBSP / NNBSP / zero-width / BOM cleaned automatically |
| Prompt loss | Every round archived to `.web-relay/<project>/rNNN-{out,in}-<timestamp>.md` |

**Recommended:** add `.web-relay/` to your project's `.gitignore`.

---

## Output contract

The prompt forces the model to answer using exactly four tags:

```xml
<file path="src/a.py">
complete file contents
</file>

<exec>python -m pytest -q</exec>

<status>done</status>

<summary>one-line description of the change</summary>
```

---

## Archive layout

```
<project>/.web-relay/
├── <project-name>/
│   ├── r001-out-20261125T101500123Z.md   # outbound prompt
│   ├── r001-in-20261125T101612456Z.md    # web reply, verbatim
│   └── r002-out-20261125T101700789Z.md
├── backups/
│   └── 20261125T101612456Z-r001/src/a.py  # pre-overwrite copy
└── last_outbound.sha256                    # echo-check hash
```

---

## Known limitations

- **One manual paste per round.** That is the design, not a bug to be fixed.
- Output is whole files, not diffs. Changing three lines still emits the entire file. Only worth introducing patch mode once large files start hitting truncation.
- Archives are evidence, not garbage-collected. Delete `.web-relay/backups/` manually when needed.
- The model must obey the XML contract. If the web UI starts producing pleasantries or code fences, `catch` refuses to parse and asks you to repack.

---

## Compatibility

| Platform | Clipboard backend |
|---|---|
| Windows | pyperclip → PowerShell → Win32 ctypes (three-level fallback) |
| macOS | pbcopy / pbpaste |
| Linux | xclip / xsel / wl-clipboard |

Python 3.8+. Zero third-party dependencies.

---

## License

[MIT](LICENSE)

---

---

# 中文说明

让本地智能体借用**网页端大模型**（Gemini / Claude / ChatGPT 等）的超长上下文与推理能力，完成本地工程、代码重构与算法实现——全程人在回路，不使用任何逆向 API 或 Cookie，因此**不承担封号与风控风险**。

> 你的网页订阅额度照常使用，本地工具照常调用。中间只隔一次 `Ctrl+V` / `Ctrl+C`。

## 它解决什么问题

本地智能体（Claude Code、Cursor、Aider…）擅长执行，但受限于本地模型的上下文长度与推理深度；网页端大模型上下文长、推理强，却没有本地工具权限。

常见的「逆向网关」方案用 Cookie 模拟网页请求，代价是：DBSC 硬件绑定导致凭据小时级失效、HTTP 200 静默降级到弱模型、账号触犯服务条款。

**web-relay 走另一条路**：不碰凭据，不模拟请求。由本地脚本把上下文打包成结构化提示词，由你亲手粘贴到网页端；拿到回复后，脚本解析结构化标签完成落盘、验证与多轮续球。

## 核心机制

```
[你: "用网页接力，执行 XXX"]
        │
        ▼
[本地 Agent] ── 扫描上下文，组装 XML 契约提示词
        │      写入剪贴板 + 归档到 .web-relay/
        ▼
[你] ── Ctrl+V 发送到网页端
        │
        ▼
[网页端大模型] ── 超长上下文 + 顶级推理
        │
        ▼
[你] ── 复制回复
        │
        ▼
[本地 Agent] ── 解析 <file>/<exec>/<status>/<summary>
        │      安全门禁 → diff 预览 → 人工确认 → 原子落盘 → 跑验证命令
        │
        ├── 报错/未完成 ──> 自动生成下一轮提示词写入剪贴板（回到 Ctrl+V）
        └── 通过 ────────> 完成
```

**关键设计**：每一轮都由你按下粘贴键。这不是自动化缺陷，而是安全边界——网页端始终以为是真人在对话，本地文件始终有你在 diff 预览后确认。

## 安装

**1. 克隆仓库**

```bash
git clone https://github.com/Big-mangoo/web-relay.git
```

**2. 复制技能到 Claude Code 技能目录**

Windows：

```powershell
Copy-Item -Recurse .\skills\web-relay "$env:USERPROFILE\.claude\skills\web-relay"
```

macOS / Linux：

```bash
mkdir -p ~/.claude/skills
cp -r ./skills/web-relay ~/.claude/skills/web-relay
```

**3. 验证**

```bash
python ~/.claude/skills/web-relay/scripts/gemini_relay.py selftest
```

预期输出：`自测全部通过：66/66 项`

无需安装任何第三方包，仅依赖 Python 3.8+ 标准库。

## 使用

在 Claude Code 里直接说：

```
用网页接力，把 src/parser.py 的异常处理改成重试三次
```

Agent 会打包上下文并提示你粘贴。之后：

1. 切到网页端，`Ctrl+V` 发送
2. 等生成完，点「复制全部回复」
3. 切回来说「已复制」

也可以手动操作：

```bash
# 打包（写入剪贴板 + 归档）
python <技能目录>/scripts/gemini_relay.py pack --root <项目目录> -f src/ -p "任务描述"

# 接球（读剪贴板 → 校验 → 归档 → diff 预览 → 落盘 → 跑命令）
python <技能目录>/scripts/gemini_relay.py catch --root <项目目录>
```

## 命令参考

| 命令 | 作用 |
|---|---|
| `pack` | 打包上下文，写入剪贴板与归档 |
| `catch` | 读取回复，解析落盘并执行验证命令 |
| `relay` | `pack` + `catch` 串联 |
| `selftest` | 内置回归自测 |

常用参数：

| 参数 | 说明 |
|---|---|
| `--root` | 项目根目录（默认当前目录） |
| `-f` | 要打包的文件或目录（可多个） |
| `-p` | 任务描述 |
| `--once` | 只处理一轮 |
| `--no-exec` | 只落盘，不执行 `<exec>` 命令 |
| `--include-secrets` | 允许打包 `.env`、`*.pem` 等（**默认拦截**） |
| `--timeout` | 单条命令超时秒数（默认 600） |

## 安全设计

| 风险 | 防护 |
|---|---|
| 模型回写恶意路径 | `safe_target` 拒绝绝对路径、`..` 越级、`.web-relay/` |
| 模型给出破坏性命令 | 危险命令黑名单（`rm -rf`、`format`、`diskpart` 等） |
| 凭证泄露 | 默认跳过 `.env`、`*.pem`、`*.key`、`id_rsa*`、`*credentials*.json` |
| 代码里的标签字面量被误执行 | `<file>` 区间掩码，正文内的 `<exec>` 不会被当真 |
| 忘复制回复导致误落盘 | 剪贴板回显哈希比对，命中即拒 |
| 覆写丢代码 | 落盘前自动备份到 `.web-relay/backups/`，diff 预览 + 人工确认 |
| 浏览器复制带入不可见字符 | NBSP / NNBSP / 零宽空格 / BOM 自动清洗 |
| 提示词丢失 | 每轮归档到 `.web-relay/<项目>/rNNN-{out,in}-<时间戳>.md` |

**建议**：把 `.web-relay/` 加入你的项目 `.gitignore`。

## 输出契约

提示词首尾强制约束模型只用四种标签输出：

```xml
<file path="src/a.py">
完整文件内容
</file>

<exec>python -m pytest -q</exec>

<status>done</status>

<summary>一句话说明改了什么</summary>
```

## 归档结构

```
<项目>/.web-relay/
├── <项目名>/
│   ├── r001-out-20261125T101500123Z.md   # 发出的提示词
│   ├── r001-in-20261125T101612456Z.md    # 网页端回复原文
│   └── r002-out-20261125T101700789Z.md
├── backups/
│   └── 20261125T101612456Z-r001/src/a.py  # 覆写前原文件
└── last_outbound.sha256                    # 回显校验
```

## 已知限制

- **每轮需要人工粘贴**。这是安全设计，不是待修复的缺陷。
- 输出的是完整文件而非 diff。改 3 行也要吐整个文件，token 消耗较大。超大文件反复截断时才考虑引入 patch 模式。
- 归档只留证，不自动清理。`.web-relay/backups/` 需要时手动删除。
- 模型必须遵守 XML 契约。若网页端开始输出寒暄或代码围栏，`catch` 会拒绝解析并提示重新打包。

## 兼容性

| 平台 | 剪贴板后端 |
|---|---|
| Windows | pyperclip → PowerShell → Win32 ctypes（三级兜底） |
| macOS | pbcopy / pbpaste |
| Linux | xclip / xsel / wl-clipboard |

Python 3.8+。零第三方依赖。

## 许可证

[MIT](LICENSE)
