"""本地微信小宠物应用包。"""

from pathlib import Path
import os

from dotenv import load_dotenv

# 服务器默认仍读取 .env；本机正式号联调显式指定独立配置，不混用模拟数据。
_project_root = Path(__file__).resolve().parent.parent
_selected_env = os.getenv("XIAOMAN_ENV_FILE", "").strip()
_env_path = Path(_selected_env) if _selected_env else _project_root / ".env"
if _selected_env:
    if not _env_path.is_absolute():
        _env_path = _project_root / _env_path
    if not _env_path.is_file():
        raise RuntimeError("指定的 XIAOMAN_ENV_FILE 不存在，不回退到其他账号配置")
load_dotenv(_env_path, override=bool(_selected_env))
