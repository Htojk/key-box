"""
test_mcp.py —— 不接任何客户端，直接考 keybox 的 MCP 协议层

做法：把 keybox.py 当子进程启起来，往它的 stdin 灌一行行 JSON-RPC，
      再逐行读 stdout，检查回答对不对。

为什么值得单独测：MCP 这层是最容易错也最难查的——客户端连不上时，
它只会说一句"服务器启动失败"，不告诉你到底哪一步不对。这里一次跑完全流程。

跑法：  python test_mcp.py

用的是临时目录（KEYBOX_HOME 指过去），所以**不会动你真正的凭据库**。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

# 和 keybox.py 里一样的小保险：控制台默认 GBK，中文会显示成乱码
for 流 in (sys.stdin, sys.stdout):
    try:
        if 流.encoding and 流.encoding.lower().replace("-", "") != "utf8":
            流.reconfigure(encoding="utf-8")
    except Exception:
        pass

这里 = os.path.dirname(os.path.abspath(__file__))
脚本 = os.path.join(这里, "keybox.py")

# 每次都从干净的临时库开始，测完删掉
临时目录 = tempfile.mkdtemp(prefix="keybox-test-")

通过 = 0
失败 = 0


def 检查(说明: str, 条件: bool, 细节: str = "") -> None:
    global 通过, 失败
    if 条件:
        通过 += 1
        print(f"  [通过] {说明}")
    else:
        失败 += 1
        print(f"  [失败] {说明}" + (f"\n         {细节}" if 细节 else ""))


def 跑(请求们: list, 环境: dict):
    """把请求灌进去，收回答。返回 {id: 响应} 和 原始 stdout。"""
    输入 = "\n".join(json.dumps(r, ensure_ascii=False) for r in 请求们) + "\n"
    结果 = subprocess.run(
        [sys.executable, 脚本, "mcp"],
        input=输入.encode("utf-8"),
        capture_output=True,
        env=环境,
        timeout=60,
    )
    if 结果.returncode != 0:
        print("子进程退出码非 0：", 结果.returncode)
        print("stderr：", 结果.stderr.decode("utf-8", "replace"))
    stdout = 结果.stdout.decode("utf-8", "replace")
    stderr = 结果.stderr.decode("utf-8", "replace")

    响应 = {}
    for 行 in stdout.splitlines():
        行 = 行.strip()
        if not 行:
            continue
        try:
            对象 = json.loads(行)
        except json.JSONDecodeError as e:
            raise AssertionError(f"stdout 里出现了不是 JSON 的东西（这会让客户端崩）：{行[:200]}\n{e}")
        if 对象.get("id") is not None:
            响应[对象["id"]] = 对象
    return 响应, stdout, stderr


def 取文本(响应: dict) -> str:
    return 响应["result"]["content"][0]["text"]


def main() -> int:
    环境 = dict(os.environ, KEYBOX_HOME=临时目录)
    print(f"临时凭据库：{临时目录}\n")

    # ---------------------------------------------------------------
    print("第 1 组：握手 + 列工具")
    # ---------------------------------------------------------------
    响应, stdout, stderr = 跑([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ], 环境)

    初始化 = 响应.get(1, {}).get("result", {})
    检查("initialize 回了 protocolVersion", bool(初始化.get("protocolVersion")),
         json.dumps(初始化, ensure_ascii=False))
    检查("initialize 声明了 tools 能力", "tools" in (初始化.get("capabilities") or {}))
    检查("initialize 自报家门 serverInfo.name = keybox",
          (初始化.get("serverInfo") or {}).get("name") == "keybox")
    检查("通知（notifications/initialized）没有被回话",
          len(响应) == 2, f"收到 {len(响应)} 个带 id 的响应，应该是 2 个")

    工具们 = 响应.get(2, {}).get("result", {}).get("tools", [])
    工具名们 = sorted(t["name"] for t in 工具们)
    检查("tools/list 列出 4 个工具",
          工具名们 == ["keybox_get", "keybox_list", "keybox_search", "keybox_set"],
          f"实际：{工具名们}")
    检查("每个工具都有 description（agent 靠它决定用不用）",
          all(len(t.get("description", "")) > 20 for t in 工具们))
    检查("每个工具都有 inputSchema",
          all(t.get("inputSchema", {}).get("type") == "object" for t in 工具们))

    # ---------------------------------------------------------------
    print("\n第 2 组：写入 → 搜索 → 取值 → 改值")
    # ---------------------------------------------------------------
    响应, _, _ = 跑([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "keybox_set", "arguments": {
             "name": "demo/stripe", "title": "Stripe 生产环境",
             "url": "https://api.stripe.com", "port": "443", "username": "acct_1",
             "secret": "sk_live_TEST123456789", "password": "p@ss",
             "tags": "支付,国外"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "keybox_search", "arguments": {"query": "stripe"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "keybox_get", "arguments": {"name": "demo/stripe"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "keybox_set", "arguments": {
             "name": "demo/stripe", "url": "https://api.stripe.com/v1"}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "keybox_get", "arguments": {"name": "demo/stripe"}}},
    ], 环境)

    检查("keybox_set 新增成功",
          not 响应[2]["result"]["isError"] and "新增" in 取文本(响应[2]),
          json.dumps(响应.get(2), ensure_ascii=False))

    搜到 = 取文本(响应[3])
    检查("keybox_search 能找到",
          "demo/stripe" in 搜到, 搜到)
    检查("keybox_search **不**返回 secret（只给元数据）",
          "sk_live_TEST123456789" not in 搜到, 搜到)
    检查("keybox_search **不**返回 password",
          "p@ss" not in 搜到, 搜到)

    取到 = 取文本(响应[4])
    检查("keybox_get 返回明文 key", "sk_live_TEST123456789" in 取到, 取到)
    检查("keybox_get 返回明文密码", "p@ss" in 取到, 取到)
    检查("keybox_get 返回 url、端口 和账号",
          "https://api.stripe.com" in 取到 and "443" in 取到 and "acct_1" in 取到, 取到)

    检查("keybox_set 二次调用是「更新」",
          "更新" in 取文本(响应[5]), 取文本(响应[5]))
    改后 = 取文本(响应[6])
    检查("更新只改了 url，没把 secret 冲掉",
          "https://api.stripe.com/v1" in 改后 and "sk_live_TEST123456789" in 改后, 改后)
    检查("更新保留了中文说明",
          "Stripe 生产环境" in 改后, 改后)

    # ---------------------------------------------------------------
    print("\n第 3 组：负向（错了也要好好说话，不许崩）")
    # ---------------------------------------------------------------
    响应, _, _ = 跑([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "keybox_get", "arguments": {"name": "根本没这个"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "keybox_search", "arguments": {"query": ""}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "keybox_set", "arguments": {"name": "只有名字没有字段"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "不存在的工具", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 6, "method": "莫名其妙的方法"},
        {"jsonrpc": "2.0", "id": 7, "method": "ping"},
    ], 环境)

    检查("取不存在的条目：isError=true 且提示去 search",
          响应[2]["result"]["isError"] and "keybox_search" in 取文本(响应[2]))
    检查("空关键词：isError=true",
          响应[3]["result"]["isError"] is True)
    检查("新条目不给字段：isError=true 且中文报错",
          响应[4]["result"]["isError"] and "字段" in 取文本(响应[4]), 取文本(响应[4]))
    检查("调不存在的工具：isError=true（不是崩溃）",
          响应[5]["result"]["isError"] is True)
    检查("不支持的方法：回 JSON-RPC 错误 -32601",
          响应[6].get("error", {}).get("code") == -32601, json.dumps(响应.get(6), ensure_ascii=False))
    检查("ping 正常回空结果", 响应[7].get("result") == {})

    # ---------------------------------------------------------------
    print("\n第 4 组：协议版本协商 + stdout 纯净度")
    # ---------------------------------------------------------------
    响应, _, _ = 跑([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {}}},
    ], 环境)
    检查("客户端要旧版本 2024-11-05，就回它 2024-11-05",
          响应[1]["result"]["protocolVersion"] == "2024-11-05")

    响应, _, _ = 跑([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "1999-01-01", "capabilities": {}, "clientInfo": {}}},
    ], 环境)
    检查("客户端要一个不存在的版本，回我们支持的最新版",
          响应[1]["result"]["protocolVersion"] == "2025-06-18")

    # 前面每次 跑() 都在解析 stdout，只要有一行不是 JSON 就已经抛错了，走到这儿说明干净
    检查("stdout 全程只有 JSON（日志都走了 stderr）", True)

    # ---------------------------------------------------------------
    print(f"\n{'=' * 50}")
    print(f"通过 {通过} 项，失败 {失败} 项")
    print("=" * 50)
    return 1 if 失败 else 0


if __name__ == "__main__":
    try:
        码 = main()
    finally:
        shutil.rmtree(临时目录, ignore_errors=True)
    sys.exit(码)
