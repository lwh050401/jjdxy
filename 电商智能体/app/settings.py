"""运行时配置：只从环境变量与 store/.env 读取，代码里不写任何凭据。"""

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    mysql_host: str = Field("127.0.0.1", alias="MYSQL_HOST")
    mysql_port: int = Field(3306, alias="MYSQL_PORT")
    mysql_user: str = Field("root", alias="MYSQL_USER")
    mysql_password: str = Field("", alias="MYSQL_PASSWORD")
    mysql_database: str = Field("lumen_store", alias="MYSQL_DATABASE")

    app_host: str = Field("127.0.0.1", alias="APP_HOST")
    app_port: int = Field(3100, alias="APP_PORT")
    admin_port: int = Field(3101, alias="ADMIN_PORT")
    env: str = Field("development", alias="APP_ENV")

    store_name: str = Field("Lumen 精选商城", alias="STORE_NAME")
    support_email: str = Field("support@lumen.shop", alias="SUPPORT_EMAIL")
    session_secret: str = Field("dev-only-insecure-secret-change-me", alias="SESSION_SECRET")
    session_ttl_seconds: int = Field(2_592_000, alias="SESSION_TTL_SECONDS")
    cookie_name: str = "lumen_sid"

    currency: str = "CNY"
    tax_rate: float = Field(0.06, alias="TAX_RATE")
    order_hold_minutes: int = Field(30, alias="ORDER_HOLD_MINUTES")
    # 超时释放的扫描间隔：只在开机跑一次的话，服务不重启过期单就一直占着库存
    order_sweep_seconds: int = Field(60, alias="ORDER_SWEEP_SECONDS")
    max_qty_per_item: int = Field(50, alias="MAX_QTY_PER_ITEM")
    free_shipping_threshold_cents: int = Field(19_900, alias="FREE_SHIPPING_THRESHOLD_CENTS")

    deepseek_api_key: str = Field("", alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field("https://api.deepseek.com/v1", alias="DEEPSEEK_BASE_URL")
    deepseek_model: str = Field("deepseek-chat", alias="DEEPSEEK_MODEL")

    static_dir: str = "static"
    uploads_url: str = "/img/products"

    # 聊天里上传的图单独放一个目录，描述子和文件一起留档，运营复盘能看到用户当时传的图
    chat_upload_dir: str = Field("data/uploads", alias="CHAT_UPLOAD_DIR")
    chat_upload_prefix: str = Field("img/chat", alias="CHAT_UPLOAD_PREFIX")
    chat_upload_max_bytes: int = Field(5_242_880, alias="CHAT_UPLOAD_MAX_BYTES")

    @property
    def dsn(self) -> str:
        return (
            f"mysql+pymysql://{quote_plus(self.mysql_user)}:{quote_plus(self.mysql_password)}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}?charset=utf8mb4"
        )

    @property
    def server_dsn(self) -> str:
        """不带库名，建库时用。"""
        return (
            f"mysql+pymysql://{quote_plus(self.mysql_user)}:{quote_plus(self.mysql_password)}"
            f"@{self.mysql_host}:{self.mysql_port}?charset=utf8mb4"
        )

    @property
    def agent_online(self) -> bool:
        return bool(self.deepseek_api_key.strip())

    # 两个进程各监听一个端口，模板里跨进程的链接必须是绝对地址：相对地址在后台端口上
    # 会指回后台自己，点「全部商品」就 404。部署到域名上时用这两个环境变量覆盖。
    shop_base_url: str = Field("", alias="SHOP_BASE_URL")
    admin_base_url: str = Field("", alias="ADMIN_BASE_URL")

    @property
    def shop_url(self) -> str:
        return self.shop_base_url.rstrip("/") or f"http://{self.app_host}:{self.app_port}"

    @property
    def admin_url(self) -> str:
        return self.admin_base_url.rstrip("/") or f"http://{self.app_host}:{self.admin_port}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
