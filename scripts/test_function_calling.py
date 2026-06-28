"""验证 function_calling 方式的结构化输出（与 ledger_middleware 修复一致）"""
import json, os
from pathlib import Path
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# 复用 langchain 的 init_chat_model（与 factory.py 完全一致的模型初始化）
import sys; sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from langchain.chat_models import init_chat_model

class FaultProfileSchema(BaseModel):
    entities: list[str] = Field(description="故障实体列表（集群名、节点名、服务名）")
    symptoms: list[str] = Field(description="故障症状列表")
    timeline: str = Field(default="", description="故障时间线描述")
    recent_changes: str = Field(default="", description="最近变更")
    prior_actions: str = Field(default="", description="用户已尝试的操作")

settings_model = os.getenv("DIAGNOSTICS_MODEL", "qwen/qwen3.6-35b-a3b")
settings_base_url = os.getenv("DIAGNOSTICS_BASE_URL", "http://127.0.0.1:1234")
api_key = os.getenv("DIAGNOSTICS_API_KEY") or "lm-studio"

# init_chat_model 需要带 /v1 的完整 URL，与 factory.py 中 openai_base_url() 行为一致
if not settings_base_url.endswith("/v1"):
    settings_base_url = f"{settings_base_url.rstrip('/')}/v1"

model = init_chat_model(
    settings_model, model_provider="openai",
    base_url=settings_base_url, api_key=api_key,
    temperature=0.2, max_tokens=1024,
)

# 与修复后的 middleware 一致：使用 function_calling 方法
structured_model = model.with_structured_output(
    FaultProfileSchema, method="function_calling",
)

print(f"模型: {settings_model}")
print(f"方法: function_calling")
print("-" * 50)

try:
    result = structured_model.invoke(
        "prod-us-east 集群 api-gateway 大量 5xx，一半 Pod 反复重启，DNS 偶发超时，半年没发布过。"
    )
    print(f"✅ 成功!")
    print(f"  entities: {result.entities}")
    print(f"  symptoms: {result.symptoms}")
    print(f"  timeline: {repr(result.timeline)}")
    print(f"  recent_changes: {repr(result.recent_changes)}")
    print(f"  prior_actions: {repr(result.prior_actions)}")
except Exception as e:
    print(f"❌ 失败: {e}")
