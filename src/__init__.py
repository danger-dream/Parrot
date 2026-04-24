"""Parrot 🦜 多家族 AI 协议代理。

单一版本号真相源。所有其他位置（server.py /health、TG 主菜单、TG /help、
README 徽章、Dockerfile LABEL、docker-compose 注释、deploy.sh banner、
GitHub Release tag）都应从这里读取或与此保持同步。

修改版本时只改这里，其他引用位置由 `scripts/bump_version.py`（如提供）或
构建流程同步。
"""

__version__ = "0.6.0"
