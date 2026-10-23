"""Exact local management details. No upstream refresh and no legacy chat-state mutation."""
from . import subjects, content, actions, store
from .providers import REGISTRY


def records(event, actor):
    target = subjects.current(event["subject"])
    if not target:
        return ["原对象已删除；不读取同名新账户账本。"]
    provider = REGISTRY.get(event["subject"]["provider"])
    lines = ["<b>真实操作账本（本地）</b>"]
    if provider and provider.records and actions._control is not None:
        values = provider.records(actions._control, actions._context(actor), target["key"], target["account"])
        for row in values[-10:]:
            lines.append(content.text(row.get("action", "操作")) + "：" + content.text(row.get("status", "unknown"))
                         + ("；业务日期 " + content.text(row["business_date"]) if row.get("business_date") else ""))
    for row in store.items("policy").values():
        if row.get("kind") == "operation" and row.get("generation") == event["subject"]["generation"]:
            lines.append(content.text(row["action"]) + "：" + content.text(row["status"]) + " · #" + content.text(row["id"]))
    if len(lines) == 1:
        lines.append("当前没有保留的业务操作记录；这不表示未发生过操作。")
    lines.append("unknown 表示待核对，不会自动重复提交。")
    return lines


def storage_lines():
    from .. import config
    from ..openai import store as response_store
    from pathlib import Path
    import stat
    cfg = (config.get().get("openai") or {}).get("store") or {}
    lines = ["<b>Responses 历史存储当前状态（只读）</b>", "配置启用：" + str(cfg.get("enabled", True)),
             "TTL(秒)：" + content.text(cfg.get("ttlSeconds", "使用默认")),
             "本进程已初始化：" + str(response_store._initialized)]
    raw = response_store._db_path
    if not raw:
        lines.append("尚未取得已初始化的存储路径；没有创建或重建数据库。")
    else:
        try:
            info = Path(raw).stat()
            lines += ["已初始化文件：" + content.text(Path(raw).name), "文件大小：" + str(info.st_size) + " 字节",
                      "权限：" + oct(stat.S_IMODE(info.st_mode))]
        except OSError:
            lines.append("已初始化文件当前不可读取；未执行写入/修复。")
    return lines
