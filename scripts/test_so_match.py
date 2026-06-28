"""测试 LM Studio 结构化输出 — 匹配用户在 LM Studio 中的配置。"""
import json, os
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
BASE_URL = os.getenv("DIAGNOSTICS_BASE_URL", "http://127.0.0.1:1234/v1")
MODEL = os.getenv("DIAGNOSTICS_MODEL", "qwen/qwen3.6-35b-a3b")
API_KEY = os.getenv("DIAGNOSTICS_API_KEY") or os.getenv("LM_STUDIO_API_KEY") or "lm-studio"

client = OpenAI(base_url=BASE_URL, api_key=API_KEY)

# 与用户在 LM Studio 中配置完全一致的 schema
schema_config = {
    "type": "json_schema",
    "json_schema": {
        "name": "fault_profile",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "entities": {"type": "array", "items": {"type": "string"}},
                "symptoms": {"type": "array", "items": {"type": "string"}},
                "timeline": {"type": "string"},
                "recent_changes": {"type": "string"},
                "prior_actions": {"type": "string"},
            },
            "required": ["entities", "symptoms"],
            "additionalProperties": False,
        },
    },
}

MSG = "prod-us-east 集群 api-gateway 大量 5xx，一半 Pod 反复重启，DNS 偶发超时，半年没发布过。"

print("="*60)
print("测试: 匹配 LM Studio 配置的 json_schema (strict=true)")
print("="*60)
try:
    r = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": MSG}],
        response_format=schema_config,
    )
    content = r.choices[0].message.content
    print(f"原始返回 ({len(content)} chars):")
    print(content)
    print()
    try:
        parsed = json.loads(content)
        print("✅ JSON 解析成功:")
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
    except json.JSONDecodeError:
        print("❌ 不是合法 JSON")
except Exception as e:
    print(f"❌ 请求失败: {e}")
