"""运营后台进程入口：`uvicorn app.admin_app:app --port 3101`。

只装 `/admin` 页面与 `/api/admin/*`，商城页面和买家接口都不在这个进程里。
"""

from .main import create_app

app = create_app("admin")
