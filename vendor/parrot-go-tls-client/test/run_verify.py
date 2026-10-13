#!/usr/bin/env python3
"""验证 parrot-go-tls-client 与 LS 真实指纹的一致性。
走 MITM(127.0.0.1:18888) 抓 ClientHello + h2 首帧，与 LS 样本逐字段对比。"""
import sys
sys.path.insert(0, '/opt/src-space/parrot-go-tls-client/python')
from parrot_go_tls_client import Client

c = Client()
try:
    r = c.post(
        "https://pgtc-verify.example.com/v1internal:loadCodeAssist",
        body=b'{"metadata":{"ideType":"ANTIGRAVITY"}}',
        headers={
            "user-agent": "antigravity/hub/2.22.0 (aidev_client; os_type=linux; arch=amd64; cl=995734886)",
            "content-type": "application/json",
            "authorization": "Bearer TEST",
        },
        proxy="http://127.0.0.1:18888",
        insecure=True,
        timeout=10,
    )
    print("HTTP:", r.status, r.error or "")
except Exception as e:
    print("EXC:", str(e)[:200])
