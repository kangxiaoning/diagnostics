from diagnostics.logging_config import setup_logging
from diagnostics.server import create_app


setup_logging()
app = create_app()


if __name__ == "__main__":
    import os

    import uvicorn

    # 复跑/评估期间必须关闭热重载：uvicorn --reload 默认监听工作目录且
    # include 仅 *.py（见 uvicorn Settings 文档），任何 .py 变更都会重启
    # app worker 并中断在途会话（2026-09-10 实证：worker 重启时刻与场景 18
    # 断流同秒 → 1058 次 APIConnectionError）。默认保持开发者体验（开），
    # 复跑前置检查要求显式置 DIAGNOSTICS_RELOAD=0。
    _raw_reload = os.getenv("DIAGNOSTICS_RELOAD", "1").strip().lower()
    _reload = _raw_reload not in {"0", "false", "no", "off", ""}
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=_reload)
