from diagnostics.logging_config import setup_logging
from diagnostics.server import create_app


setup_logging()
app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
