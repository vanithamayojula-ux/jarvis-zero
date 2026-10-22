"""python -m jarvis  →  starts the backend on http://127.0.0.1:8600"""
import uvicorn

from .server import PORT, app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
