"""
keybox.py —— 本地 agent 共用的「业务凭据箱」

一句话：一个 SQLite 文件 + 一个 MCP 服务器，让 Claude Code / opencode / Codex
        在需要某个业务的 url / key / 密码 时，能自己来查。

两副面孔，同一个文件：
    给人用：  keybox add / list / show / rm / import      （命令行，负责灌数据）
    给 agent：keybox mcp                                  （MCP stdio 服务器，只读为主）

对应关系：
    人  ──▶ CLI ──┐
                  ├──▶ C:\\Users\\<你>\\.keybox\\keybox.db   ← 唯一真相源（明文，不加密）
    agent ─▶ MCP ─┘         └──▶ audit.log               ← 谁在什么时候取了哪条

依赖：只用 Python 自带的 sqlite3 / json / argparse / csv / getpass。不用 pip 装任何东西。

【为什么自己写而不装官方 mcp SDK】
MCP 的 stdio 协议就是「一行一个 JSON（JSON-RPC 2.0）」，握手 + 列工具 + 调工具
一共三个方法，手写一百来行。少一个依赖少一个装不上的坑。协议逻辑全在 mcp_server()
和 调工具() 里，将来想换官方 SDK，只动这两个地方。
"""

import argparse
import csv
import getpass
import json
import os
import sqlite3
import sys
import unicodedata
from datetime import datetime

# ---------------------------------------------------------------------------
# 小保险：这台机器的控制台默认是 cp936(GBK)，中文写出去会乱码，
# MCP 模式下更致命——agent 那边按 UTF-8 解码，收到乱码会解析失败。
# 所以开工第一件事：把 stdin/stdout 都强行掰成 UTF-8。
# （这段写法是从 jev_demo.py 里搬过来的，已验证可用）
# ---------------------------------------------------------------------------
for 流 in (sys.stdin, sys.stdout):
    try:
        if 流.encoding and 流.encoding.lower().replace("-", "") != "utf8":
            流.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ===========================================================================
# 【第 1 节】存哪儿 —— 路径与字段定义
# ===========================================================================

# 数据目录默认在用户主目录下的 .keybox。
# 允许用环境变量 KEYBOX_HOME 覆盖（自动化测试时指到临时目录，不污染真库）。
数据目录 = os.environ.get("KEYBOX_HOME") or os.path.join(os.path.expanduser("~"), ".keybox")
数据库文件 = os.path.join(数据目录, "keybox.db")
审计文件 = os.path.join(数据目录, "audit.log")

# 一条记录有哪些字段。name 是主键，其余都是内容。
# 顺序就是打印时的顺序：先是「这是个什么平台」，再是「怎么连」。
内容字段 = ["title", "url", "port", "username", "secret", "password", "note", "tags"]

# 哪些字段是敏感值：给 agent 的 search/list 绝不返回，报错日志里要打码。
敏感字段 = ("secret", "password")

# 字段的中文标签，打印时用。
字段中文 = {
    "name": "平台代号",
    "title": "中文说明",
    "url": "地址/接口",
    "port": "端口",
    "username": "账号",
    "secret": "API Key",
    "password": "密码",
    "note": "备注",
    "tags": "标签",
}


# ===========================================================================
# 【第 2 节】数据库 —— 建表、读写
# ===========================================================================

建表语句 = """
CREATE TABLE IF NOT EXISTS creds (
    name       TEXT PRIMARY KEY,   -- 平台代号，ASCII，比如 mysql-dev（agent 靠它要信息）
    title      TEXT,               -- 中文说明，比如 "开发库"
    url        TEXT,               -- 域名 / IP / 接口地址
    port       TEXT,               -- 端口，比如 3306
    username   TEXT,               -- 账号
    secret     TEXT,               -- API key / token
    password   TEXT,               -- 密码
    note       TEXT,               -- 备注
    tags       TEXT,               -- 逗号分隔，方便搜
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""

# 表「应该」长这样。CREATE TABLE IF NOT EXISTS 对已经存在的旧表不生效，
# 所以再加一道迁移：每次连库时对一遍，缺哪列补哪列（ALTER TABLE ADD COLUMN 不丢数据）。
应有列 = {
    "name": "TEXT", "title": "TEXT", "url": "TEXT", "port": "TEXT",
    "username": "TEXT", "secret": "TEXT", "password": "TEXT", "note": "TEXT",
    "tags": "TEXT", "created_at": "TEXT", "updated_at": "TEXT",
}


def 迁移(库) -> None:
    实有 = {行["name"] for 行 in 库.execute("PRAGMA table_info(creds)")}
    for 列, 类型 in 应有列.items():
        if 列 not in 实有:
            库.execute(f"ALTER TABLE creds ADD COLUMN {列} {类型}")
    库.execute("INSERT OR REPLACE INTO meta(k, v) VALUES ('schema_version', '1')")


def 连库() -> sqlite3.Connection:
    """打开（必要时创建）数据库。三个 agent 可能同时来读，所以设了 busy_timeout。"""
    os.makedirs(数据目录, exist_ok=True)
    库 = sqlite3.connect(数据库文件, timeout=10)
    库.row_factory = sqlite3.Row          # 这样 fetch 出来能按列名取值，好读
    库.execute("PRAGMA busy_timeout = 10000")
    库.executescript(建表语句)
    迁移(库)
    库.commit()
    return 库


def 现在() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def 取一条(库, 名字: str):
    return 库.execute("SELECT * FROM creds WHERE name = ?", (名字,)).fetchone()


def 所有条目(库, 关键词: str = ""):
    """关键词为空 = 全部；否则在 name/title/url/username/note/tags 里模糊找。"""
    if not 关键词:
        return 库.execute("SELECT * FROM creds ORDER BY name").fetchall()
    # 把 LIKE 的通配符转义掉，免得用户输入 % 时匹配到一切
    模式 = "%" + 关键词.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return 库.execute(
        """SELECT * FROM creds
           WHERE name     LIKE ? ESCAPE '\\'
              OR title    LIKE ? ESCAPE '\\'
              OR url      LIKE ? ESCAPE '\\'
              OR username LIKE ? ESCAPE '\\'
              OR note     LIKE ? ESCAPE '\\'
              OR tags     LIKE ? ESCAPE '\\'
           ORDER BY name""",
        (模式,) * 6,
    ).fetchall()


def 写入(库, 名字: str, 字段: dict) -> str:
    """
    新增或修改。**只动传进来的字段**，没传的原样保留。
    返回 '新增' 或 '更新'。
    """
    已有 = 取一条(库, 名字)
    现在时间 = 现在()

    if 已有 is None:
        if not 字段:
            raise ValueError("这是新条目，至少要给一个字段（比如 --url 或 --secret）")
        列 = ["name"] + list(字段.keys()) + ["created_at", "updated_at"]
        值 = [名字] + list(字段.values()) + [现在时间, 现在时间]
        占位 = ",".join("?" * len(列))
        库.execute(f"INSERT INTO creds ({','.join(列)}) VALUES ({占位})", 值)
        动作 = "新增"
    else:
        if not 字段:
            raise ValueError("没有要改的字段")
        改动 = ", ".join(f"{k} = ?" for k in 字段)
        库.execute(
            f"UPDATE creds SET {改动}, updated_at = ? WHERE name = ?",
            list(字段.values()) + [现在时间, 名字],
        )
        动作 = "更新"

    库.commit()
    return 动作


def 删除(库, 名字: str) -> bool:
    结果 = 库.execute("DELETE FROM creds WHERE name = ?", (名字,))
    库.commit()
    return 结果.rowcount > 0


# ===========================================================================
# 【第 3 节】审计日志 + 打码
#
# 两个规矩，都是被坑出来的：
#   1. 审计只记「动作 + 名字」，**绝不记值**。
#   2. 打码按**字段名**判断是不是敏感字段，不按长度。
#      （上次凭据排查时写了个"按长度掩码"的脚本，结果两条较短的口令原样漏进了对话记录。）
# ===========================================================================

def 记审计(动作: str, 名字: str, 来源: str = "cli") -> None:
    try:
        os.makedirs(数据目录, exist_ok=True)
        with open(审计文件, "a", encoding="utf-8") as f:
            f.write(json.dumps(
                {"ts": 现在(), "action": 动作, "name": 名字, "via": 来源, "pid": os.getpid()},
                ensure_ascii=False) + "\n")
    except Exception:
        pass   # 记审计失败不能连累主流程


def 打码(值, 露头: int = 3) -> str:
    """把敏感值变成能确认"是不是这一把"、但抄不走的样子。"""
    if not 值:
        return "（空）"
    值 = str(值)
    if len(值) <= 露头:
        return "·" * 6 + f"  （{len(值)} 位）"
    return 值[:露头] + "…" + f"  （共 {len(值)} 位）"


def 显示宽度(文本: str) -> int:
    """中文占两格，英文占一格。用 len() 对齐中文会歪，这里按显示宽度算。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in str(文本))


def 补齐(文本: str, 宽: int) -> str:
    return str(文本) + " " * max(0, 宽 - 显示宽度(文本))


def 格式化条目(行, 明文: bool) -> str:
    """把一行记录打成人能看的字。明文=False 时只给元数据，secret/password 打码。"""
    行 = dict(行)
    顺序 = ["name", "title", "url", "port", "username", "secret", "password", "note", "tags"]
    每行 = []
    for 键 in 顺序:
        值 = 行.get(键)
        if 键 in 敏感字段 and not 明文:
            值 = 打码(值)
        elif not 值:
            值 = "（空）"
        每行.append(f"{补齐(字段中文[键], 10)}: {值}")
    每行.append(f"（创建 {行.get('created_at') or '?'} ／ 更新 {行.get('updated_at') or '?'}）")
    return "\n".join(每行)


def 列表文本(行们) -> str:
    """search / list 的输出：只有元数据，一个敏感值都不带。"""
    if not 行们:
        return "没有匹配的条目。"
    每行 = []
    for 行 in 行们:
        说明 = 行["title"] or ""
        地址 = 行["url"] or ""
        # 地址是裸 IP/域名且没带端口时，顺手把端口拼上（10.0.0.5 + 3306 → 10.0.0.5:3306）；
        # 已经是 https://... 这种完整 URL 的就别画蛇添足
        if 地址 and 行["port"] and "://" not in 地址 and ":" not in 地址:
            地址 = f"{地址}:{行['port']}"
        尾部 = "  ".join(x for x in (说明, 地址) if x)
        每行.append(f"- {行['name']:<24} {尾部}")
    return ("找到 %d 条：\n" % len(行们)) + "\n".join(每行) + \
        "\n\n（这里只给元数据。要取值请用 keybox_get 取那一条。）"


# ===========================================================================
# 【第 4 节】命令行 —— 给人用的那一半
# ===========================================================================

def 命令_init(参数) -> int:
    库 = 连库()
    库.close()
    收紧权限()
    print(f"凭据库已就绪：{数据库文件}")
    print()
    print("下一步，加第一条试试：")
    print('    keybox add mysql-dev --title "开发库" \\')
    print('        --url 10.0.0.5 --port 3306 --username root --password xxx')
    print()
    print('    keybox add tc-edgeone --title "腾讯云 EdgeOne Makers" \\')
    print('        --url https://ai-gateway.edgeone.link/v1/systemone --secret sk-xxx')
    print()
    print("或者不带参数直接敲 `keybox add demo/stripe`，会一项一项问你。")
    return 0


def 命令_add(参数) -> int:
    字段 = {}
    # 命令行里给了就直接用
    for 键 in 内容字段:
        值 = getattr(参数, 键, None)
        if 值:
            字段[键] = 值

    # 一个都没给？那就坐下来一项一项问（值不回显，不进 shell 历史）
    if not 字段:
        print(f"逐项录入「{参数.name}」，不需要的项直接按回车跳过。")
        for 键 in 内容字段:
            提示 = f"  {字段中文[键]}：" + ("（输入不回显）" if 键 in 敏感字段 else " ")
            值 = (getpass.getpass(提示) if 键 in 敏感字段 else input(提示)).strip()
            if 值:
                字段[键] = 值

    库 = 连库()
    try:
        动作 = 写入(库, 参数.name, 字段)
    except ValueError as e:
        print(f"没写成：{e}")
        return 1
    记审计(动作, 参数.name, "cli")

    print(f"已{动作}条目「{参数.name}」（字段：{', '.join(字段) or '无'}）")
    行 = 取一条(库, 参数.name)
    print()
    print(格式化条目(行, 明文=False))     # 回显打码，别把明文再吐一遍
    库.close()
    return 0


def 命令_list(参数) -> int:
    库 = 连库()
    print(列表文本(所有条目(库)))
    库.close()
    return 0


def 命令_show(参数) -> int:
    库 = 连库()
    行 = 取一条(库, 参数.name)
    if 行 is None:
        print(f"没有叫「{参数.name}」的条目。用 `keybox list` 看看都有什么。")
        库.close()
        return 1
    print(格式化条目(行, 明文=参数.reveal))
    if 参数.reveal:
        记审计("reveal", 参数.name, "cli")
    else:
        print("\n（想看明文：keybox show %s --reveal）" % 参数.name)
    库.close()
    return 0


def 命令_rm(参数) -> int:
    库 = 连库()
    行 = 取一条(库, 参数.name)
    if 行 is None:
        print(f"没有叫「{参数.name}」的条目，没什么可删的。")
        库.close()
        return 1
    if not 参数.yes:
        回答 = input(f"确认删除「{参数.name}」？（yes 才删） ").strip().lower()
        if 回答 != "yes":
            print("算了，没删。")
            库.close()
            return 0
    删除(库, 参数.name)
    记审计("rm", 参数.name, "cli")
    print(f"已删除「{参数.name}」。")
    库.close()
    return 0


def 命令_path(参数) -> int:
    print(f"数据目录：{数据目录}")
    print(f"数据库  ：{数据库文件}" + ("（还不存在，先跑 keybox init）" if not os.path.exists(数据库文件) else ""))
    print(f"审计日志：{审计文件}")
    return 0


# 导入文件里的表头可能长这样，也能长成中文
表头别名 = {
    "name": "name", "业务代号": "name", "名字": "name", "名称": "name", "代号": "name",
    "title": "title", "说明": "title", "中文说明": "title", "描述": "title",
    "url": "url", "地址": "url", "接口地址": "url", "链接": "url", "host": "url", "ip": "url",
    "port": "port", "端口": "port",
    "username": "username", "user": "username", "账号": "username", "用户名": "username",
    "secret": "secret", "key": "secret", "apikey": "secret", "api key": "secret",
    "api_key": "secret", "密钥": "secret", "token": "secret",
    "password": "password", "pass": "password", "密码": "password", "口令": "password",
    "note": "note", "备注": "note", "注意事项": "note",
    "tags": "tags", "标签": "tags", "分类": "tags",
}


def 读导入文件(路径: str):
    """从 CSV 或 JSON 里读出一串 {name: ..., title: ...} 字典。"""
    if 路径.lower().endswith(".json"):
        with open(路径, encoding="utf-8") as f:
            数据 = json.load(f)
        if isinstance(数据, dict):                       # {"名字": {字段...}}
            return [dict(v, name=k) for k, v in 数据.items()]
        return list(数据)                                # [{字段...}, ...]
    with open(路径, encoding="utf-8-sig", newline="") as f:   # -sig 顺手吃掉 BOM
        行们 = []
        for 原始 in csv.DictReader(f):
            条 = {}
            for 列名, 值 in 原始.items():
                标准 = 表头别名.get((列名 or "").strip().lower())
                if 标准 and (值 or "").strip():
                    条[标准] = 值.strip()
            if 条.get("name"):
                行们.append(条)
        return 行们


def 命令_import(参数) -> int:
    try:
        条们 = 读导入文件(参数.file)
    except Exception as e:
        print(f"读不了 {参数.file}：{e}")
        return 1
    if not 条们:
        print("没读出任何条目（每条至少要有一个 name / 业务代号）。")
        return 1

    库 = 连库()
    结果 = []
    for 条 in 条们:
        名字 = 条.pop("name")
        字段 = {k: v for k, v in 条.items() if k in 内容字段 and v}
        已有 = 取一条(库, 名字)
        if 参数.dry_run:
            结果.append(f"  [试运行] {'更新' if 已有 else '新增'} {名字}  字段：{', '.join(字段) or '（无）'}")
            continue
        动作 = 写入(库, 名字, 字段)
        记审计(动作, 名字, "import")
        结果.append(f"  {动作} {名字}  字段：{', '.join(字段) or '（无）'}")
    库.close()

    print(f"共 {len(结果)} 条：" + ("（试运行，什么都没写）" if 参数.dry_run else ""))
    print("\n".join(结果))
    return 0


def 收紧权限() -> None:
    """把 .keybox 目录限成只有当前用户能进。非 Windows 直接跳过。"""
    if os.name != "nt":
        return
    用户 = os.environ.get("USERNAME")
    if not 用户:
        return
    import subprocess
    try:
        subprocess.run(
            ["icacls", 数据目录, "/inheritance:r", "/grant:r", f"{用户}:(OI)(CI)F"],
            capture_output=True, timeout=15,
        )
    except Exception:
        pass   # 改不动就算了，不打断初始化


# ===========================================================================
# 【第 5 节】MCP 服务器 —— 给 agent 用的那一半
#
# 协议：stdin 一行一个 JSON 请求，stdout 一行一个 JSON 响应。
# 必须记住的两件事：
#   1. stdout 只能出现 JSON！所有日志、报错统统走 stderr，否则客户端解析崩溃。
#   2. 没有 id 的是「通知」，不能回；有 id 的必须回一个同样 id 的响应。
# ===========================================================================

支持的协议版本 = ["2024-11-05", "2025-03-26", "2025-06-18"]
默认协议版本 = "2025-06-18"

# 工具说明书。description 写得好不好，直接决定 agent 会不会主动来问。
工具表 = [
    {
        "name": "keybox_search",
        "description": (
            "在本地凭据箱里模糊搜索平台凭据（按平台代号 / 说明 / 地址 / 账号 / 标签匹配）。"
            "**当你需要连接或调用某个平台——比如数据库、内网服务、Stripe、腾讯云——"
            "却不知道它的地址、端口、账号、API key 或密码时，先用本工具找到平台代号，"
            "再用 keybox_get 取完整信息。不要凭空猜测地址或密钥。** "
            "本工具只返回元数据，不返回值。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词，例如 stripe、mysql、腾讯云、内网网关"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "keybox_list",
        "description": "列出凭据箱里所有平台条目（只有元数据，不含密钥值）。不确定有什么的时候用它。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "keybox_get",
        "description": (
            "按平台代号取出**完整**信息：地址、端口、账号、明文 API key、密码、备注。"
            "在真正去连这个平台或写代码调它之前调用它。取到的值请直接用于当前任务，"
            "不要复述给用户，也不要写进任何会被提交的文件。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "平台代号，必须与 keybox_search 返回的完全一致，例如 mysql-dev"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "keybox_set",
        "description": (
            "新增或修改一条平台凭据。**只更新你传进来的字段**，没传的字段保持原样。"
            "当用户告诉你某个平台的新地址、新端口、新账号、新 key 或新密码，"
            "或让你记下某个平台的连接信息时用它。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "平台代号（主键，ASCII），例如 mysql-dev"},
                "title": {"type": "string", "description": "中文说明，例如「开发库」"},
                "url": {"type": "string", "description": "地址：域名 / IP / 接口地址"},
                "port": {"type": "string", "description": "端口，例如 3306、443"},
                "username": {"type": "string", "description": "账号"},
                "secret": {"type": "string", "description": "API key / token"},
                "password": {"type": "string", "description": "密码"},
                "note": {"type": "string", "description": "备注：怎么用、有什么坑"},
                "tags": {"type": "string", "description": "逗号分隔的标签，方便日后搜索，例如「数据库,内网」"},
            },
            "required": ["name"],
        },
    },
]


def 写一行(对象: dict) -> None:
    sys.stdout.write(json.dumps(对象, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def 回结果(消息id, 结果: dict) -> None:
    写一行({"jsonrpc": "2.0", "id": 消息id, "result": 结果})


def 回错误(消息id, 码: int, 消息: str) -> None:
    写一行({"jsonrpc": "2.0", "id": 消息id, "error": {"code": 码, "message": 消息}})


def 调工具(名字: str, 参数: dict):
    """真正干活的地方。返回 (给 agent 看的文本, 是不是出错了)。"""
    库 = 连库()
    try:
        if 名字 == "keybox_search":
            关键词 = (参数.get("query") or "").strip()
            if not 关键词:
                return "要给我一个关键词，比如 stripe 或者 数据库。", True
            return 列表文本(所有条目(库, 关键词)), False

        if 名字 == "keybox_list":
            return 列表文本(所有条目(库)), False

        if 名字 == "keybox_get":
            代号 = (参数.get("name") or "").strip()
            if not 代号:
                return "要给我业务代号。先 keybox_search 找到它。", True
            行 = 取一条(库, 代号)
            if 行 is None:
                return (f"凭据箱里没有「{代号}」这一条。"
                        f"先用 keybox_search 搜关键词确认业务代号。"), True
            记审计("get", 代号, "mcp")
            return 格式化条目(行, 明文=True), False

        if 名字 == "keybox_set":
            代号 = (参数.get("name") or "").strip()
            if not 代号:
                return "要给我业务代号（name）。", True
            字段 = {k: 参数[k] for k in 内容字段 if 参数.get(k) not in (None, "")}
            try:
                动作 = 写入(库, 代号, 字段)
            except ValueError as e:
                return f"没写成：{e}", True
            记审计(动作, 代号, "mcp")
            行 = 取一条(库, 代号)
            return (f"已{动作}「{代号}」（字段：{', '.join(字段) or '无'}）。"
                    f"当前状态：\n{格式化条目(行, 明文=False)}"), False

        return f"没有叫 {名字} 的工具。", True
    finally:
        库.close()


def mcp_server() -> int:
    """MCP 主循环。一行一个请求，处理完一行回一行。"""
    print(f"keybox MCP 已启动，库：{数据库文件}", file=sys.stderr, flush=True)
    连库().close()          # 先确保库和目录存在

    while True:
        # 用 readline() 而不是 for 循环：管道上 for 会预读缓冲，导致响应延迟。
        行 = sys.stdin.readline()
        if not 行:           # stdin 关了 = 客户端退出
            break
        行 = 行.strip()
        if not 行:
            continue

        try:
            消息 = json.loads(行)
        except json.JSONDecodeError as e:
            回错误(None, -32700, f"JSON 解析失败：{e}")
            continue

        方法 = 消息.get("method")
        消息id = 消息.get("id")

        if 方法 is None:
            continue                                  # 是别人对我们的响应，忽略

        try:
            if 方法 == "initialize":
                密码本 = (消息.get("params") or {})
                客户端版本 = 密码本.get("protocolVersion")
                版本 = 客户端版本 if 客户端版本 in 支持的协议版本 else 默认协议版本
                回结果(消息id, {
                    "protocolVersion": 版本,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "keybox", "version": "0.1.0"},
                })

            elif 方法 in ("notifications/initialized", "initialized"):
                continue                              # 通知，不许回

            elif 方法 == "ping":
                回结果(消息id, {})

            elif 方法 == "tools/list":
                回结果(消息id, {"tools": 工具表})

            elif 方法 == "tools/call":
                参数们 = 消息.get("params") or {}
                工具名 = 参数们.get("name") or ""
                实参 = 参数们.get("arguments") or {}
                文本, 出错了 = 调工具(工具名, 实参)
                回结果(消息id, {
                    "content": [{"type": "text", "text": 文本}],
                    "isError": 出错了,
                })

            elif 方法.startswith("notifications/"):
                continue

            else:
                if 消息id is not None:
                    回错误(消息id, -32601, f"不支持的方法：{方法}")

        except Exception as e:
            # 出错也要好好回话，绝不把栈打到 stdout
            print(f"处理 {方法} 时出错：{e!r}", file=sys.stderr, flush=True)
            if 消息id is not None:
                回错误(消息id, -32603, f"服务器内部错误：{e}")

    return 0


# ===========================================================================
# 【第 6 节】入口
# ===========================================================================

def 造命令行():
    解析器 = argparse.ArgumentParser(
        prog="keybox",
        description="本地 agent 共用的平台凭据箱（SQLite + MCP）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
常用：
  keybox init                              建库
  keybox add mysql-dev --url 10.0.0.5 --port 3306 --username root --password ...
                                           加一条（不给参数就逐项问）
  keybox list                              列出所有平台（不含密钥值）
  keybox show mysql-dev [--reveal]         看一条（默认打码）
  keybox rm mysql-dev                      删一条
  keybox import keys.csv --dry-run         从 CSV/JSON 批量导入，先试运行
  keybox mcp                               以 MCP 服务器方式启动（给 agent 用）
""")
    子命令 = 解析器.add_subparsers(dest="命令")

    子命令.add_parser("init", help="建库").set_defaults(执行=命令_init)
    子命令.add_parser("list", help="列出所有业务条目").set_defaults(执行=命令_list)
    子命令.add_parser("path", help="显示数据库文件在哪").set_defaults(执行=命令_path)

    p = 子命令.add_parser("add", help="新增或修改一条")
    p.add_argument("name", help="业务代号，ASCII，例如 stripe-prod")
    for 键 in 内容字段:
        p.add_argument(f"--{键}", default=None, help=字段中文[键])
    p.set_defaults(执行=命令_add)

    p = 子命令.add_parser("show", help="查看一条")
    p.add_argument("name")
    p.add_argument("--reveal", action="store_true", help="显示明文（默认打码）")
    p.set_defaults(执行=命令_show)

    p = 子命令.add_parser("rm", help="删除一条")
    p.add_argument("name")
    p.add_argument("-y", "--yes", action="store_true", help="不再问，直接删")
    p.set_defaults(执行=命令_rm)

    p = 子命令.add_parser("import", help="从 CSV / JSON 批量导入")
    p.add_argument("file")
    p.add_argument("--dry-run", action="store_true", help="只演示要做什么，不真写")
    p.set_defaults(执行=命令_import)

    p = 子命令.add_parser("mcp", help="以 MCP 服务器方式启动（stdio）")
    p.set_defaults(执行=lambda 参数: mcp_server())

    return 解析器


def main() -> int:
    解析器 = 造命令行()
    参数 = 解析器.parse_args()
    if not getattr(参数, "执行", None):
        解析器.print_help()
        return 0
    return 参数.执行(参数)


if __name__ == "__main__":
    sys.exit(main())
