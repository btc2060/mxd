"""BaoTa entry point: one ASGI process and an independent background scheduler."""
from goldwatch.config import Environment
import uvicorn

if __name__ == "__main__":
    env = Environment.load()
    uvicorn.run("goldwatch.app:create_app", factory=True, host=env.host, port=env.port, workers=1,
                timeout_graceful_shutdown=20, log_level="info", access_log=False)
