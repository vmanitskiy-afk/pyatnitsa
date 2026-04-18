"""Конфигурация Пятница.ai"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseSettings):
    """Настройки LLM провайдеров."""
    
    primary_provider: str = "gigachat"
    fallback_provider: str | None = None
    
    # GigaChat (основной)
    gigachat_credentials: str = ""  # base64(client_id:client_secret)
    gigachat_model: str = "GigaChat-2-Max"
    gigachat_scope: str = "GIGACHAT_API_PERS"  # PERS / B2B / CORP
    gigachat_verify_ssl: bool = False
    gigachat_ca_bundle_file: str = ""  # путь к сертификату Минцифры
    gigachat_max_tokens: int = 4096
    
    # Claude (опциональный fallback)
    claude_api_key: str = ""
    claude_model: str = "claude-sonnet-4-20250514"
    claude_max_tokens: int = 4096

    # Ollama (локальная модель)
    ollama_base_url: str = ""  # e.g. http://10.0.100.127:11434
    ollama_model: str = "gemma4:31b"
    ollama_max_tokens: int = 4096


class ChannelSettings(BaseSettings):
    """Настройки каналов связи."""
    
    # MAX messenger
    max_bot_token: str = ""
    max_webhook_url: str = ""
    max_use_polling: bool = True
    
    # Telegram
    telegram_bot_token: str = ""
    telegram_use_polling: bool = True


class MemorySettings(BaseSettings):
    """Настройки системы памяти."""
    
    db_path: str = "data/memory.db"
    max_context_messages: int = 20
    summary_after_messages: int = 50


class SchedulerSettings(BaseSettings):
    """Настройки планировщика."""
    
    heartbeat_interval_minutes: int = 5
    enabled: bool = True


class IntegrationSettings(BaseSettings):
    """Настройки интеграций."""
    
    # EasyRedmine
    redmine_url: str = ""
    redmine_api_key: str = ""
    
    # Битрикс24
    bitrix_webhook_url: str = ""
    
    # 1С
    onec_base_url: str = ""
    onec_username: str = ""
    onec_password: str = ""


class Settings(BaseSettings):
    """Главные настройки Пятница.ai."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )
    
    # General
    app_name: str = "Пятница.ai"
    debug: bool = False
    log_level: str = "INFO"
    data_dir: str = "data"
    skills_dir: str = "skills"
    
    # API server
    api_host: str = "0.0.0.0"
    api_port: int = 8080
    admin_password: str = ""  # пароль для админ-панели
    
    # Sub-settings
    llm: LLMSettings = Field(default_factory=LLMSettings)
    channels: ChannelSettings = Field(default_factory=ChannelSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    integrations: IntegrationSettings = Field(default_factory=IntegrationSettings)


# Singleton
_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
