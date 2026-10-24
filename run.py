"""moray-ghost 启动入口：python run.py（也可用 start.ps1）。"""
import uvicorn

from app.core.config import HOST, PORT

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="info")
