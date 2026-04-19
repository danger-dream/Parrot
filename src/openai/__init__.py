"""OpenAI 家族协议支持子树。

这棵子树只处理 OpenAI `/v1/chat/completions` 与 `/v1/responses` 两种入口
以及对应上游协议 openai-chat / openai-responses；与 anthropic 家族完全隔离。

本子树内部永远不 import src/transform/*、src/channel/api_channel.py、
src/channel/oauth_channel.py —— 任何协议无关的基础设施（scheduler / scorer /
cooldown / affinity / state_db / log_db / notifier 等）通过根包暴露的扩展点
或带默认值的新参数访问。
"""
