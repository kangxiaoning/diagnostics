"""测试: response_format type=text 是否允许普通 LLM 调用 + 简化 schema 测试"""
import json, os
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
BASE_URL = os.getenv("DIAGNOSTICS_BASE_URL", "http://127.0.0.1:1234/v1")
MODEL = os.getenv("DIAGNOSTICS_MODEL", "qwen/qwen3.6-35b-a3b")
API_KEY = os.getenv("DIAGNOSTICS_API_KEY") or "lm-studio"

client = OpenAI(base_url=BASE_URL, api_key=API_KEY)
MSG = [{"role": "user", "content": "你好，请用一句话介绍自己。"}]

# 测试1: type=text (让普通对话能工作)
print("="*60)
print("测试1: response_format type=text (普通对话)")
print("="*60)
try:
    r = client.chat.completions.create(
        model=MODEL, messages=MSG,
        response_format={"type": "text"},
    )
    content = r.choices[0].message.content
    print(f"返回 ({len(content)} chars): {repr(content[:200])}")
except Exception as e:
    print(f"❌ {e}")

# 测试2: 简化 schema (只用最核心的两个字段)
print("\n" + "="*60)
print("测试2: 最简 schema (entities + symptoms)")
print("="*60)
try:
    r = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "prod-us-east 集群 api-gateway 大量 5xx，一半 Pod 反复重启，DNS 偶发超时，半年没发布过。"}],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "simple",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "entities": {"type": "array", "items": {"type": "string"}},
                        "symptoms": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["entities", "symptoms"],
                    "additionalProperties": False,
                },
            },
        },
    )
    content = r.choices[0].message.content
    print(f"返回 ({len(content)} chars):")
    print(content)
    try:
        parsed = json.loads(content)
        print(f"✅ 解析成功: {json.dumps(parsed, ensure_ascii=False)}")
    except json.JSONDecodeError:
        print("❌ 不是合法 JSON")
except Exception as e:
    print(f"❌ {e}")
