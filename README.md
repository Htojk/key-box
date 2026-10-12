# keybox —— 本地 agent 共用的「平台凭据箱」

## 这是干什么的

平时我们要连一堆平台：数据库、内网服务、腾讯云、Stripe……每个的地址、端口、账号、
key、密码都不一样。以前这些信息散落在各个 agent 各自的配置里，改一处漏一处。

keybox 把它们收进**一个 SQLite 文件**，然后用 **MCP** 的方式暴露给 agent ——
你对任何 agent（Claude Code / opencode / Codex）说「把开发库里的数据查一下」，
它会自己去 keybox 里取地址、端口、账号和密码，不用你每次粘贴。

```
  你  ──▶ keybox 命令行 ──┐
                         ├──▶  C:\Users\<你>\.keybox\keybox.db   ← 唯一真相源
  agent ─▶ keybox MCP ───┘            └──▶ audit.log            ← 谁什么时候取了哪条
```

一句话理解：**一个文件存着所有平台的连接信息，所有 agent 都来这一个地方查。**

---

## 第 1 步：建库

```bash
python keybox.py init
```

会建出 `C:\Users\<你>\.keybox\`，里面只有一个 `keybox.db`。
Windows 上还会顺手把这个目录的权限收紧到只有你自己能进。

---

## 第 2 步：灌数据

两种方式，随便哪种。

### 方式一：一条一条加

```bash
# 全在命令行给（适合脚本、批量）
python keybox.py add mysql-dev \
    --title "开发库" --url 10.0.0.5 --port 3306 \
    --username root --password devpass123 --tags "数据库,内网"

# 只给代号，剩下的它一项一项问你（密码类输入不回显，也不进命令历史）
python keybox.py add tc-edgeone
```

### 方式二：从 CSV / JSON 批量导入

CSV 表头支持中文也支持英文，认得的别名见 `keybox.py` 里的 `表头别名`：

```csv
平台代号,说明,地址,端口,账号,密钥,备注,标签
mysql-dev,开发库,10.0.0.5,3306,root,devpass123,只在公司内网,数据库
tc-edgeone,腾讯云 EdgeOne Makers,https://ai-gateway.edgeone.link/v1/systemone,,sk-xxx,跑 jev_demo.py 用,腾讯云,LLM
```

```bash
python keybox.py import keys.csv --dry-run    # 先看它打算做什么
python keybox.py import keys.csv              # 确认没问题再真写
```

---

## 第 3 步：看一眼存了什么

```bash
python keybox.py list                 # 列出所有平台（只有说明和地址，不含密钥值）
python keybox.py show mysql-dev       # 看一条，密钥打码
python keybox.py show mysql-dev --reveal   # 看明文（会记一条审计）
python keybox.py rm mysql-dev         # 删（会再问一次）
python keybox.py path                 # 数据库文件到底在哪
```

---

## 第 4 步：接进各个 agent

配一次就行，之后这些 agent 都能查同一个库。

### Claude Code

```bash
claude mcp add --scope user keybox -- python "E:\学习\models\keybox\keybox.py" mcp
```

装完 `claude mcp list` 应该看到 `keybox: ... ✔ Connected`。**要重开一个 Claude Code 会话才生效。**

### opencode

编辑 `~/.config/opencode/opencode.json`，在已有的 `mcp` 块里加一条（形状和它里面现有的
`mysql-recruit` 那些完全一致）：

```jsonc
{
  "mcp": {
    "keybox": {
      "type": "local",
      "command": ["python", "E:\\学习\\models\\keybox\\keybox.py", "mcp"],
      "enabled": true
    }
  }
}
```

> 注意 JSON 里反斜杠要写成 `\\`。

### Codex

编辑 `~/.codex/config.toml`，加一段（和里面已有的 `[mcp_servers.node_repl]` 同款）：

```toml
[mcp_servers.keybox]
command = 'python'
args = ['E:\学习\models\keybox\keybox.py', 'mcp']
```

TOML 里用单引号，反斜杠就不用转义了。

---

## agent 会用到的四个工具

agent 看到的不是命令行，是这四个 MCP 工具：

| 工具 | 干什么 | 返回明文吗 |
|---|---|---|
| `keybox_search` | 模糊找平台（按代号/说明/地址/账号/标签），不知道代号时先搜 | 否，只给元数据 |
| `keybox_list` | 列出全部平台 | 否 |
| `keybox_get` | 按代号取完整信息：地址、端口、账号、key、密码、备注 | **是** |
| `keybox_set` | 新增或修改一条（只动你传的字段） | — |

设计上故意做成两步：**先 `search`（不泄漏任何值）拿到代号，再 `get` 取那一条**。
另外**「删除」没有开成 MCP 工具**，只能你手敲 `keybox rm`，防止 agent 手滑。

每个工具的 `description` 里写了"什么时候该用它"——这句话就是 agent 的说明书，
决定了它会不会在需要时主动来查。

---

## 每个字段什么意思

| 字段 | 中文 | 例子 | 说明 |
|---|---|---|---|
| `name` | 平台代号 | `mysql-dev` | **主键**，ASCII。agent 靠它来要信息，起得短、好猜、别用中文 |
| `title` | 中文说明 | `开发库` | 给人看的 |
| `url` | 地址/接口 | `10.0.0.5` 或 `https://api.stripe.com` | 域名/IP/接口地址都行 |
| `port` | 端口 | `3306` | 没有就留空 |
| `username` | 账号 | `root` | |
| `secret` | API Key | `sk-xxx` | API key / token 放这儿 |
| `password` | 密码 | `devpass123` | 密码放这儿 |
| `note` | 备注 | `只在公司内网` | 怎么用、有什么坑 |
| `tags` | 标签 | `数据库,内网` | 逗号分隔，帮助搜索 |

`secret` 和 `password` 是**敏感字段**：`list` / `search` 和打码显示时都会被遮掉。

---

## 必须知道的三件事

1. **没有加密。** 这是你明确要求的——就自己一台机器用。也就是说
   `keybox.db` 里的密码是**明文**，谁拿到这个文件谁就拿到全部。所以：
   - 这个目录**不要**放进 git、也不要放进任何云盘同步目录
   - 它的默认位置在主目录下的 `.keybox`，本来就不在项目里，别挪到项目里去

2. **agent 每取一次，明文就进了它的对话记录。**
   这是"给 agent 明文"这条路绕不开的：`keybox_get` 一返回，那串 key 就写进了
   agent 的 transcript（比如 `~/.claude/projects/**/*.jsonl`）。自用没问题，
   但要心里有数。

3. **`audit.log` 只记"谁什么时候取了哪条"，不记值。**
   想回头看有没有异常调用，翻这个文件。

---

## 怎么确认它真的能用

```bash
python test_mcp.py
```

这个脚本不接任何客户端，直接把 `keybox.py mcp` 当子进程起来，
往它嘴里灌 MCP 协议的原话，逐条检查回答对不对（握手、列工具、存取、报错、
中文编码、stdout 里有没有混进非 JSON 的杂音）。用的是临时目录，**不会动你的真库**。

全绿之后，再开个新会话对 agent 说一句：

> 我要连开发库，连接信息是多少？

它应该自己调 `keybox_search` 再 `keybox_get`，然后把地址、端口、账号、密码念给你。

---

## 文件

| 文件 | 是什么 |
|---|---|
| `keybox.py` | 全部代码：SQLite 读写 + 命令行 + MCP 服务器 |
| `test_mcp.py` | 协议层自测（26 项） |
| `keybox.cmd` | Windows 入口，把这个目录加进 PATH 就能直接敲 `keybox list` |
| `README.md` | 就是本文件 |

`C:\Users\<你>\.keybox\` 里是数据（`keybox.db` + `audit.log`），
和代码分开放，这样代码可以随便改、随便备份，数据不会跟着乱跑。
