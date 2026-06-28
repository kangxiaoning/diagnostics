"""测试 LM Studio 结构化输出是否正常工作。

用法:
    python scripts/test_structured_output.py
"""

import json
import os
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

# 加载项目 .env 文件
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE_URL = os.getenv("DIAGNOSTICS_BASE_URL", "http://127.0.0.1:1234/v1")
MODEL = os.getenv("DIAGNOSTICS_MODEL", "qwen/qwen3.6-35b-a3b")
API_KEY = os.getenv("DIAGNOSTICS_API_KEY") or os.getenv("LM_STUDIO_API_KEY") or "lm-studio"

client = OpenAI(base_url=BASE_URL, api_key=API_KEY)

schema = {
    "type": "json_schema",
    "json_schema": {
        "name": "fault_profile",
        "schema": {
            "type": "object",
            "properties": {
                "entities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "故障实体列表（集群名、节点名、服务名）",
                },
                "symptoms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "故障症状列表",
                },
                "timeline": {
                    "type": "string",
                    "description": "故障时间线描述",
                },
                "recent_changes": {
                    "type": "string",
                    "description": "最近变更",
                },
                "prior_actions": {
                    "type": "string",
                    "description": "用户已尝试的操作",
                },
            },
            "required": ["entities", "symptoms"],
        },
    },
}

print(f"模型: {MODEL}")
print(f"端点: {BASE_URL}")
print(f"Schema: {json.dumps(schema, ensure_ascii=False, indent=2)}")
print("-" * 50)

try:
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "user",
                "content": "prod-us-east 集群 api-gateway 大量 5xx，一半 Pod 反复重启，DNS 偶发超时，半年没发布过。",
            }
        ],
        response_format=schema,
    )

    content = response.choices[0].message.content
    print("原始返回:")
    print(repr(content))
    print("-" * 50)

    # 尝试解析 JSON
    parsed = json.loads(content)
    print("解析成功:")
    print(json.dumps(parsed, ensure_ascii=False, indent=2))

except json.JSONDecodeError as e:
    print(f"JSON 解析失败: {e}")
    print(f"原始内容: {repr(content)}")

except Exception as e:
    print(f"请求失败: {e}")
    print(f"错误类型: {type(e).__name__}")
