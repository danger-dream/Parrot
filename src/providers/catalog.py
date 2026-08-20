"""Telegram 渠道向导使用的内置提供商目录。

这不是运行时 provider adapter registry。目录只为新建/显式重新选择渠道提供模板；
已持久化渠道始终使用其 resolved baseUrl/apiPath，不会被目录更新重写。

端点、套餐与静态模型清单按 2026-08-20 的厂商官方文档核验。没有官方固定
兼容端点、需要云签名/部署 ID/WorkspaceId，或只能由用户控制台生成地址的产品不
放入目录；没有官方模型发现接口的 preset 使用静态候选或回退手工输入。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    display_name: str
    models_url: str | None
    models_auth: str
    models_parser: str
    protocols: Mapping[str, str]
    static_models: tuple[str, ...] = ()
    cc_mimicry: bool = False


@dataclass(frozen=True)
class ProviderBrand:
    id: str
    display_name: str
    presets: tuple[ProviderPreset, ...]


def _preset(
    pid: str,
    name: str,
    models_url: str | None,
    protocols: dict[str, str],
    *,
    models_auth: str = "bearer",
    models_parser: str = "openai-data-id",
    static_models: tuple[str, ...] = (),
    cc_mimicry: bool = False,
) -> ProviderPreset:
    return ProviderPreset(
        pid,
        name,
        models_url,
        models_auth,
        models_parser,
        protocols,
        static_models,
        cc_mimicry,
    )


def _openai_region(pid: str, name: str, host: str) -> ProviderPreset:
    # OpenAI 的地区文档明确列出 Chat/Responses，但未把 /models 列为地区服务；
    # 因此模型发现继续使用官方全球 models endpoint。
    return _preset(pid, name, "https://api.openai.com/v1/models", {
        "openai-chat": f"https://{host}/v1/chat/completions",
        "openai-responses": f"https://{host}/v1/responses",
    })


def _mimo_token(pid: str, name: str, host: str) -> ProviderPreset:
    return _preset(pid, name, None, {
        "anthropic": f"https://{host}/anthropic/v1/messages",
        "openai-chat": f"https://{host}/v1/chat/completions",
        "openai-responses": f"https://{host}/v1/responses",
    }, static_models=("mimo-v2.5-pro", "mimo-v2.5"), cc_mimicry=True)


def _minimax(pid: str, name: str, host: str, *, token_plan: bool = False) -> ProviderPreset:
    return _preset(pid, name, f"https://{host}/v1/models", {
        "anthropic": f"https://{host}/anthropic/v1/messages",
        "openai-chat": f"https://{host}/v1/chat/completions",
        "openai-responses": f"https://{host}/v1/responses",
    }, cc_mimicry=token_plan)


def _siliconflow(pid: str, name: str, host: str) -> ProviderPreset:
    return _preset(pid, name, f"https://{host}/v1/models", {
        "anthropic": f"https://{host}/v1/messages",
        "openai-chat": f"https://{host}/v1/chat/completions",
    }, cc_mimicry=True)


PROVIDER_CATALOG: tuple[ProviderBrand, ...] = (
    ProviderBrand("zhipu", "智谱 GLM", (
        _preset("coding-cn", "Coding Plan（中国）", "https://open.bigmodel.cn/api/v1/models", {
            "anthropic": "https://open.bigmodel.cn/api/anthropic/v1/messages",
            "openai-chat": "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions",
            "openai-responses": "https://open.bigmodel.cn/api/v1/responses",
        }, cc_mimicry=True),
        _preset("coding-global", "Coding Plan（国际）", "https://api.z.ai/api/v1/models", {
            "anthropic": "https://api.z.ai/api/anthropic/v1/messages",
            "openai-chat": "https://api.z.ai/api/coding/paas/v4/chat/completions",
            "openai-responses": "https://api.z.ai/api/v1/responses",
        }, cc_mimicry=True),
        _preset("api-cn", "API 按量付费（中国）", "https://open.bigmodel.cn/api/v1/models", {
            "anthropic": "https://open.bigmodel.cn/api/anthropic/v1/messages",
            "openai-chat": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        }),
        _preset("api-global", "API 按量付费（国际）", "https://api.z.ai/api/v1/models", {
            "anthropic": "https://api.z.ai/api/anthropic/v1/messages",
            "openai-chat": "https://api.z.ai/api/paas/v4/chat/completions",
        }),
    )),
    ProviderBrand("kimi", "Kimi", (
        _preset("code", "Kimi Code", None, {
            "anthropic": "https://api.kimi.com/coding/v1/messages",
            "openai-chat": "https://api.kimi.com/coding/v1/chat/completions",
        }, static_models=("k3", "k3-256k", "kimi-for-coding", "kimi-for-coding-highspeed")),
        _preset("api-cn", "API 按量付费（中国）", "https://api.moonshot.cn/v1/models", {
            "anthropic": "https://api.moonshot.cn/anthropic/v1/messages",
            "openai-chat": "https://api.moonshot.cn/v1/chat/completions",
        }),
        _preset("api-global", "API 按量付费（国际）", "https://api.moonshot.ai/v1/models", {
            "anthropic": "https://api.moonshot.ai/anthropic/v1/messages",
            "openai-chat": "https://api.moonshot.ai/v1/chat/completions",
        }),
    )),
    ProviderBrand("deepseek", "DeepSeek", (
        _preset("standard", "标准 API", "https://api.deepseek.com/models", {
            "anthropic": "https://api.deepseek.com/anthropic/v1/messages",
            "openai-chat": "https://api.deepseek.com/chat/completions",
            "openai-responses": "https://api.deepseek.com/responses",
        }),
    )),

    # 1. OpenAI：全球 API + 官方数据驻留主机。
    ProviderBrand("openai", "OpenAI", (
        _openai_region("global", "全球 API", "api.openai.com"),
        _openai_region("us", "数据驻留（美国）", "us.api.openai.com"),
        _openai_region("eu", "数据驻留（欧洲）", "eu.api.openai.com"),
        _openai_region("gb", "数据驻留（英国）", "gb.api.openai.com"),
        _openai_region("ca", "数据驻留（加拿大）", "ca.api.openai.com"),
        _openai_region("jp", "数据驻留（日本）", "jp.api.openai.com"),
        _openai_region("kr", "数据驻留（韩国）", "kr.api.openai.com"),
        _openai_region("sg", "数据驻留（新加坡）", "sg.api.openai.com"),
        _openai_region("in", "数据驻留（印度）", "in.api.openai.com"),
        _openai_region("au", "数据驻留（澳大利亚）", "au.api.openai.com"),
        _openai_region("ae", "数据驻留（阿联酋）", "ae.api.openai.com"),
    )),

    # 2. Anthropic：Claude 订阅 OAuth 不等于 API Key，因此这里只收 Claude API。
    ProviderBrand("anthropic", "Anthropic", (
        _preset("api", "Claude API", "https://api.anthropic.com/v1/models?limit=1000", {
            "anthropic": "https://api.anthropic.com/v1/messages",
            "openai-chat": "https://api.anthropic.com/v1/chat/completions",
        }, models_auth="anthropic-x-api-key"),
    )),

    # 3. 科大讯飞：星火、星辰 MaaS 与 Astron 套餐使用不同 key/hostname。
    ProviderBrand("iflytek", "科大讯飞（星辰/星火）", (
        _preset("spark-http", "讯飞星火 HTTP API", None, {
            "openai-chat": "https://spark-api-open.xf-yun.com/v1/chat/completions",
        }, static_models=("4.0Ultra", "generalv3", "pro-128k", "max-32k", "lite")),
        _preset("spark-x2", "讯飞星火 X2", None, {
            "openai-chat": "https://spark-api-open.xf-yun.com/v2/chat/completions",
        }, static_models=("spark-x",)),
        _preset("xingchen-maas", "讯飞星辰 MaaS（新服务）", None, {
            "openai-chat": "https://maas-api.cn-huabei-1.xf-yun.com/v2/chat/completions",
        }),
        _preset("astron-coding", "Astron Coding Plan", None, {
            "anthropic": "https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic/v1/messages",
            "openai-chat": "https://maas-coding-api.cn-huabei-1.xf-yun.com/v2/chat/completions",
            "openai-responses": "https://maas-coding-api.cn-huabei-1.xf-yun.com/v1/responses",
        }, static_models=(
            "astron-code-latest", "xsparkx2agent", "xsparkx2", "xsparkx2flash", "auto",
            "xopglm52", "xopglm51", "xopglm5", "xopdeepseekv4pro", "xopdeepseekv4flash",
            "xopdeepseekv32", "xopkimik26", "xopkimik25", "xminimaxm25", "xopqwen35397b",
            "xopqwen36v35b", "xopqwen35v35b", "xop3qwencodernext", "xopglmv47flash",
            "xopkimi27code",
        ), cc_mimicry=True),
        _preset("astron-token", "Astron Token Plan", None, {
            "anthropic": "https://maas-token-api.cn-huabei-1.xf-yun.com/anthropic/v1/messages",
            "openai-chat": "https://maas-token-api.cn-huabei-1.xf-yun.com/v2/chat/completions",
        }, static_models=(
            "xsparkx2agent", "xsparkx2", "xsparkx2flash", "xopglm52", "xopglm51", "xopglm5",
            "xopdeepseekv4pro", "xopdeepseekv4flash", "xopdeepseekv32", "xopkimik26",
            "xopkimik25", "xminimaxm25", "xopqwen35397b", "xopqwen36v35b",
            "xopqwen35v35b", "xop3qwencodernext", "xopglmv47flash",
        ), cc_mimicry=True),
    )),

    # 4. 百炼：固定共享地域可直接预置；东京/法兰克福等 WorkspaceId 专属域名
    # 无法由当前固定 URL schema 表达，仍可走“自定义 Base URL”。
    ProviderBrand("alibaba-bailian", "阿里云百炼", (
        _preset("api-cn", "API 按量（北京）", "https://dashscope.aliyuncs.com/api/v1/models", {
            "anthropic": "https://dashscope.aliyuncs.com/apps/anthropic/v1/messages",
            "openai-chat": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "openai-responses": "https://dashscope.aliyuncs.com/compatible-mode/v1/responses",
        }, models_parser="dashscope-output-model"),
        _preset("api-sg", "API 按量（新加坡）", "https://dashscope-intl.aliyuncs.com/api/v1/models", {
            "anthropic": "https://dashscope-intl.aliyuncs.com/apps/anthropic/v1/messages",
            "openai-chat": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
            "openai-responses": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/responses",
        }, models_parser="dashscope-output-model"),
        _preset("api-us", "API 按量（弗吉尼亚）", "https://dashscope-us.aliyuncs.com/api/v1/models", {
            "anthropic": "https://dashscope-us.aliyuncs.com/apps/anthropic/v1/messages",
            "openai-chat": "https://dashscope-us.aliyuncs.com/compatible-mode/v1/chat/completions",
            "openai-responses": "https://dashscope-us.aliyuncs.com/compatible-mode/v1/responses",
        }, models_parser="dashscope-output-model"),
        _preset("token-plan-cn", "Token Plan（北京）", None, {
            "anthropic": "https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic/v1/messages",
            "openai-chat": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions",
            "openai-responses": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/responses",
        }, static_models=("qwen3.8-max", "qwen3.7-max", "qwen3.7-plus", "qwen3.6-plus", "qwen3.6-flash"),
            cc_mimicry=True),
        _preset("coding-cn", "Coding Plan（中国站）", None, {
            "anthropic": "https://coding.dashscope.aliyuncs.com/apps/anthropic/v1/messages",
            "openai-chat": "https://coding.dashscope.aliyuncs.com/v1/chat/completions",
        }, static_models=(
            "qwen3.7-plus", "qwen3.6-plus", "qwen3.5-plus", "qwen3-max-2026-01-23",
            "qwen3-coder-next", "qwen3-coder-plus", "kimi-k2.5", "glm-5", "glm-4.7",
            "MiniMax-M2.5",
        ), cc_mimicry=True),
        _preset("coding-global", "Coding Plan（国际站）", None, {
            "anthropic": "https://coding-intl.dashscope.aliyuncs.com/apps/anthropic/v1/messages",
            "openai-chat": "https://coding-intl.dashscope.aliyuncs.com/v1/chat/completions",
        }, static_models=(
            "qwen3.7-plus", "qwen3.6-plus", "qwen3.5-plus", "qwen3-coder-next",
            "qwen3-coder-plus", "kimi-k2.5", "glm-5", "glm-4.7", "MiniMax-M2.5",
        ), cc_mimicry=True),
    )),

    # 5. 火山引擎方舟：公开固定主机均为北京；Coding Plan Responses 尚无可直接
    # 核验的完整 URL，首版保守省略。
    ProviderBrand("volcengine-ark", "火山引擎方舟", (
        _preset("api-cn", "方舟 API（北京）", None, {
            "anthropic": "https://ark.cn-beijing.volces.com/api/compatible/v1/messages",
            "openai-chat": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
            "openai-responses": "https://ark.cn-beijing.volces.com/api/v3/responses",
        }, static_models=(
            "doubao-seed-2-1-pro-260628", "doubao-seed-2-0-lite-260428",
            "doubao-seed-1-8-251228", "doubao-seed-evolving",
        ), cc_mimicry=True),
        _preset("agent-plan", "Agent Plan", None, {
            "anthropic": "https://ark.cn-beijing.volces.com/api/plan/v1/messages",
            "openai-chat": "https://ark.cn-beijing.volces.com/api/plan/v3/chat/completions",
            "openai-responses": "https://ark.cn-beijing.volces.com/api/plan/v3/responses",
        }, static_models=("ark-code-latest",), cc_mimicry=True),
        _preset("coding-plan", "Coding Plan", None, {
            "anthropic": "https://ark.cn-beijing.volces.com/api/coding/v1/messages",
            "openai-chat": "https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions",
        }, static_models=(
            "doubao-seed-2.0-code", "doubao-seed-2.0-pro", "doubao-seed-2.0-lite",
            "doubao-seed-code", "minimax-m2.7", "minimax-m2.5", "glm-5.1", "glm-4.7",
            "deepseek-v3.2", "kimi-k2.6", "kimi-k2.5",
        ), cc_mimicry=True),
    )),

    # 6. 腾讯云：TokenHub 地域、旧混元与两类套餐凭证互不通用。
    ProviderBrand("tencent-cloud", "腾讯云", (
        _preset("tokenhub-cn", "TokenHub（广州 / 中国大陆）", "https://tokenhub.tencentmaas.com/v1/models", {
            "anthropic": "https://tokenhub.tencentmaas.com/v1/messages",
            "openai-chat": "https://tokenhub.tencentmaas.com/v1/chat/completions",
            "openai-responses": "https://tokenhub.tencentmaas.com/v1/responses",
        }, cc_mimicry=True),
        _preset("tokenhub-global", "TokenHub（新加坡 / 全球）", "https://tokenhub-intl.tencentmaas.com/v1/models", {
            "anthropic": "https://tokenhub-intl.tencentmaas.com/v1/messages",
            "openai-chat": "https://tokenhub-intl.tencentmaas.com/v1/chat/completions",
            "openai-responses": "https://tokenhub-intl.tencentmaas.com/v1/responses",
        }, cc_mimicry=True),
        _preset("hunyuan-legacy", "腾讯混元兼容 API（存量）", None, {
            "anthropic": "https://api.hunyuan.cloud.tencent.com/anthropic/v1/messages",
            "openai-chat": "https://api.hunyuan.cloud.tencent.com/v1/chat/completions",
        }, cc_mimicry=True),
        _preset("coding-plan", "腾讯云 Coding Plan", None, {
            "anthropic": "https://api.lkeap.cloud.tencent.com/coding/anthropic/v1/messages",
            "openai-chat": "https://api.lkeap.cloud.tencent.com/coding/v3/chat/completions",
        }, static_models=("tc-code-latest",), cc_mimicry=True),
        _preset("token-plan", "腾讯云 Token Plan", None, {
            "anthropic": "https://api.lkeap.cloud.tencent.com/plan/anthropic/v1/messages",
            "openai-chat": "https://api.lkeap.cloud.tencent.com/plan/v3/chat/completions",
        }, cc_mimicry=True),
    )),

    # 7. 京东云 JoyBuilder 2.0；套餐 URL 来自官方兼容 Base URL + 标准协议路径。
    ProviderBrand("jd-cloud", "京东云", (
        _preset("api", "JoyBuilder 模型服务", None, {
            "openai-chat": "https://modelservice.jdcloud.com/v1/chat/completions",
        }),
        _preset("coding-plan", "JoyBuilder Coding Plan", None, {
            "openai-chat": "https://modelservice.jdcloud.com/coding/openai/v1/chat/completions",
        }),
        _preset("token-plan", "JoyBuilder Token Plan", None, {
            "anthropic": "https://modelservice.jdcloud.com/tokenPlan/anthropic/v1/messages",
            "openai-chat": "https://modelservice.jdcloud.com/tokenPlan/openai/v1/chat/completions",
        }, static_models=("maas-token-latest",), cc_mimicry=True),
    )),

    # 8. 百度千帆：v2、Token Plan 与停售但仍服务存量用户的 Coding Plan。
    ProviderBrand("baidu-qianfan", "百度智能云千帆", (
        _preset("api-v2", "千帆模型服务 v2", "https://qianfan.baidubce.com/v2/models", {
            "anthropic": "https://qianfan.baidubce.com/anthropic/v1/messages",
            "openai-chat": "https://qianfan.baidubce.com/v2/chat/completions",
            "openai-responses": "https://qianfan.baidubce.com/v2/responses",
        }, cc_mimicry=True),
        _preset("token-personal", "Token Plan 个人版", None, {
            "anthropic": "https://qianfan.baidubce.com/anthropic/tokenplan/personal/v1/messages",
            "openai-chat": "https://qianfan.baidubce.com/v2/tokenplan/personal/chat/completions",
        }, cc_mimicry=True),
        _preset("token-team", "Token Plan 企业版", None, {
            "anthropic": "https://qianfan.baidubce.com/anthropic/tokenplan/team/v1/messages",
            "openai-chat": "https://qianfan.baidubce.com/v2/tokenplan/team/chat/completions",
        }, cc_mimicry=True),
        _preset("coding-legacy", "Coding Plan（存量）", None, {
            "anthropic": "https://qianfan.baidubce.com/anthropic/coding/v1/messages",
            "openai-chat": "https://qianfan.baidubce.com/v2/coding/chat/completions",
        }, static_models=("qianfan-code-latest",), cc_mimicry=True),
    )),

    # 9. Xiaomi MiMo：按量与 Token Plan key 不互通，Token Plan 分三地域。
    ProviderBrand("xiaomi-mimo", "Xiaomi MiMo", (
        _preset("api", "API 按量付费", "https://api.xiaomimimo.com/v1/models", {
            "anthropic": "https://api.xiaomimimo.com/anthropic/v1/messages",
            "openai-chat": "https://api.xiaomimimo.com/v1/chat/completions",
            "openai-responses": "https://api.xiaomimimo.com/v1/responses",
        }, static_models=("mimo-v2.5-pro", "mimo-v2.5"), cc_mimicry=True),
        _mimo_token("token-cn", "Token Plan（中国）", "token-plan-cn.xiaomimimo.com"),
        _mimo_token("token-sg", "Token Plan（新加坡）", "token-plan-sgp.xiaomimimo.com"),
        _mimo_token("token-eu", "Token Plan（欧洲）", "token-plan-ams.xiaomimimo.com"),
    )),

    # 10. OpenCode Go：不同模型组只支持一种 wire protocol，按协议拆 preset，
    # 避免把动态 /models 的全部模型错误地投到同一协议。
    ProviderBrand("opencode-go", "OpenCode Go", (
        _preset("responses", "Responses 模型", None, {
            "openai-responses": "https://opencode.ai/zen/go/v1/responses",
        }, models_auth="none", static_models=(
            "grok-4.5", "gpt-5.6-luna", "muse-spark-1.2-contributor",
        )),
        _preset("chat", "OpenAI Chat 模型", None, {
            "openai-chat": "https://opencode.ai/zen/go/v1/chat/completions",
        }, models_auth="none", static_models=(
            "glm-5.3", "glm-5.2", "glm-5.1", "kimi-k3", "kimi-k2.7-code", "kimi-k2.6",
            "deepseek-v4-pro", "deepseek-v4-flash", "mimo-v2.5", "mimo-v2.5-pro", "hy3",
        )),
        _preset("anthropic", "Anthropic 模型", None, {
            "anthropic": "https://opencode.ai/zen/go/v1/messages",
        }, models_auth="none", static_models=(
            "minimax-m3", "minimax-m2.7", "minimax-m2.5", "qwen3.8-max", "qwen3.7-max",
            "qwen3.7-plus", "qwen3.6-plus",
        ), cc_mimicry=True),
    )),

    # 11. 天翼云当前统一主机为 ai.ctaigw.cn；旧 wishub-x1/x6 不再预置。
    ProviderBrand("ctyun-xirang", "天翼云息壤（星辰 TokenHub）", (
        _preset("api", "标准 API", "https://ai.ctaigw.cn/v1/models", {
            "openai-chat": "https://ai.ctaigw.cn/v1/chat/completions",
        }),
        _preset("token-plan", "Token Plan", None, {
            "openai-chat": "https://ai.ctaigw.cn/coding/v1/chat/completions",
        }),
        _preset("coding-plan", "编程 Token Plan", None, {
            "anthropic": "https://ai.ctaigw.cn/coding/v1/messages",
            "openai-chat": "https://ai.ctaigw.cn/coding/v1/chat/completions",
        }, static_models=("GLM-5-Pro", "DeepSeek-V3.2-Pro"), cc_mimicry=True),
    )),

    # 12. Ollama Cloud；本地 localhost:11434 不属于云端 provider preset。
    ProviderBrand("ollama-cloud", "Ollama Cloud", (
        _preset("cloud", "Cloud API", "https://ollama.com/v1/models", {
            "openai-chat": "https://ollama.com/v1/chat/completions",
            "openai-responses": "https://ollama.com/v1/responses",
        }),
    )),

    # 13. OpenRouter 的 Anthropic skin 使用 Bearer；cc_mimicry 仅在选择该协议时生效。
    ProviderBrand("openrouter", "OpenRouter", (
        _preset("standard", "标准 API", "https://openrouter.ai/api/v1/models", {
            "anthropic": "https://openrouter.ai/api/v1/messages",
            "openai-chat": "https://openrouter.ai/api/v1/chat/completions",
            "openai-responses": "https://openrouter.ai/api/v1/responses",
        }, cc_mimicry=True),
    )),

    # 14. MiniMax：中/国际站与按量/Token Plan credential 均分别展示。
    ProviderBrand("minimax", "MiniMax", (
        _minimax("api-cn", "API 按量（中国）", "api.minimaxi.com"),
        _minimax("token-cn", "Token Plan（中国）", "api.minimaxi.com", token_plan=True),
        _minimax("api-global", "API 按量（国际）", "api.minimax.io"),
        _minimax("token-global", "Token Plan（国际）", "api.minimax.io", token_plan=True),
    )),

    # 15. 硅基流动：当前正式中国/国际主机；旧 api.ap 主机不再预置。
    ProviderBrand("siliconflow", "硅基流动 SiliconFlow", (
        _siliconflow("api-cn", "API（中国）", "api.siliconflow.cn"),
        _siliconflow("api-global", "API（国际）", "api.siliconflow.com"),
    )),
)


def get_brand(provider_id: str) -> ProviderBrand | None:
    return next((brand for brand in PROVIDER_CATALOG if brand.id == provider_id), None)


def get_preset(provider_id: str, preset_id: str) -> ProviderPreset | None:
    brand = get_brand(provider_id)
    return next((preset for preset in brand.presets if preset.id == preset_id), None) if brand else None
