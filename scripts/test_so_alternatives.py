"""测试 LM Studio 结构化输出的各种方式，找出可用的方案。"""
import json, os
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
BASE_URL = os.getenv("DIAGNOSTICS_BASE_URL", "http://127.0.0.1:1234/v1")
MODEL = os.getenv("DIAGNOSTICS_MODEL", "qwen/qwen3.6-35b-a3b")
API_KEY = os.getenv("DIAGNOSTICS_API_KEY") or os.getenv("LM_STUDIO_API_KEY") or "lm-studio"

client = OpenAI(base_url=BASE_URL, api_key=API_KEY)
MSG = [{"role": "user", "content": "prod-us-east 集群 api-gateway 大量 5xx，一半 Pod 反复重启，DNS 偶发超时，半年没发布过。请用 JSON 格式返回故障实体和症状列表。"}]

tests = [
    (
        "1) json_object 模式",
        {"response_format": {"type": "json_object"}},
    ),
    (
        "2) 无 response_format + system prompt 要求 JSON",
        {
            "messages": [{"role": "system", "content": "你必须只输出合法的 JSON 对象，不要输出任何其他文本、解释或 markdown。格式: {\"entities\": [\"...\"], \"symptoms\": [\"...\"]}"}] + MSG,
        },
    ),
]

for label, kwargs in tests:
    print(f"\n{'='*60}")
    print(f"测试: {label}")
    print(f"{'='*60}")
    try:
        params = {"model": MODEL, "messages": MSG}
        params.update(kwargs)
        r = client.chat.completions.create(**params)
        content = r.choices[0].message.content
        print(f"原始: {repr(content)}")
        try:
            parsed = json.loads(content)
            print(f"JSON解析成功: {json.dumps(parsed, ensure_ascii=False, indent=2)}")
        except json.JSONDecodeError:
            print("不是合法JSON，尝试提取...")
            # 尝试从 markdown 代码块中提取
            import re
            m = re.search(r'```(?:json)?\s*([\s\S]*?)```', content or "")
            if m:
                try:
                    parsed = json.loads(m.group(1))
                    print(f"从代码块提取成功: {json.dumps(parsed, ensure_ascii=False, indent=2)}")
                except json.JSONDecodeError:
                    print("代码块也不是合法JSON")
    except Exception as e:
        print(f"请求失败: {e}")
