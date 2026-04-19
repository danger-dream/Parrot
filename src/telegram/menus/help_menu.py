"""帮助页：图标含义、菜单导航、常见问题速查。"""

from __future__ import annotations

from typing import Optional

from .. import ui


_HELP_TEXT = (
    "❓ <b>帮助</b>\n"
    "─────────\n\n"
    "<b>主要菜单</b>\n"
    "📊 状态总览 — 一眼看服务健康度（运行时长 / 渠道 / 配额 / 问题）\n"
    "📈 统计汇总 — 按时间×维度查看 token 用量、缓存命中、调用样本\n"
    "📋 最近日志 — 最新 15 条请求；详情可看重试链 + 完整 body\n"
    "🔀 渠道管理 — 第三方 API 渠道增删改、测试、清错误/亲和\n"
    "🔐 管理 OAuth — Anthropic OAuth 账户登录、刷新、配额查看\n"
    "🔑 管理 API Key — 下游客户端使用的代理 Key\n"
    "⚙ 系统设置 — 超时 / 错误阶梯 / 评分 / 亲和 / CCH / 黑名单\n\n"

    "<b>渠道状态图标</b>\n"
    "✅ 可用 · ⬛ 已禁用 · 🟢 健康 · 🟡 一般 · 🔴 异常\n"
    "🟠 临时冷却 · 🔴 永久冷却 · ⚪ 暂无数据\n"
    "🚫 用户禁用 · 🔒 配额禁用 · ❌ 认证失败\n"
    "🔐 OAuth 渠道 · 🔀 第三方 API 渠道\n\n"

    "<b>调度模式</b>\n"
    "<code>smart</code>: 按评分（延迟 + 失败惩罚）排序，含 20% 探索\n"
    "<code>order</code>: 按 config 中渠道定义顺序，固定优先级\n\n"

    "<b>会话亲和</b>\n"
    "同一会话（指纹 = api_key + ip + 倒数两条消息）绑定到首次成功的渠道；\n"
    "30 分钟无新请求自动释放，分数差距 > 3× 时打破。\n\n"

    "<b>错误冷却阶梯（默认）</b>\n"
    "1 / 3 / 5 / 10 / 15 / 0(永久) 分钟，连续失败递进，成功一次清零。\n\n"

    "<b>常见操作速查</b>\n"
    "• 加新 OAuth 账号 → 「🔐 管理 OAuth」→「➕ 新增账户」→ 登录或粘贴 JSON\n"
    "• 加第三方渠道 → 「🔀 渠道管理」→「➕ 添加渠道」→ 4 步向导 + 测试\n"
    "• 调代理超时 → 「⚙ 系统设置」→「⏱ 超时设置」\n"
    "• 紧急清所有冷却 → 「🔀 渠道管理」→「🧹 清全部错误」\n"
)


def _kb() -> dict:
    return ui.inline_kb([ui.back_to_main_row()])


def show(chat_id: int, message_id: int, cb_id: Optional[str] = None) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, ui.truncate(_HELP_TEXT), reply_markup=_kb())


def send_new(chat_id: int) -> None:
    ui.send(chat_id, ui.truncate(_HELP_TEXT), reply_markup=_kb())


def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str) -> bool:
    if data == "menu:help":
        show(chat_id, message_id, cb_id)
        return True
    return False
