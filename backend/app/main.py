"""API 服务的启动入口。

    uvicorn app.main:app --reload        # 开发
    make dev                              # 同上，外加前端

Worker 是另一个入口（`app/worker/`），和 API 跑在不同进程里。
这是模块化单体的两个启动点，不是两个服务。
"""

from app.api.app import create_app

app = create_app()
