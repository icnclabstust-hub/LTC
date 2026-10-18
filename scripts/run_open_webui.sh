#!/bin/bash
# 啟動 Open WebUI。資料（上傳檔、快取、.webui_secret_key）放在獨立資料根目錄，
# 不放在 pip 套件安裝目錄內，避免升級／重裝 open-webui 時被一併清除。
DATA_ROOT="${LTC_DATA_ROOT:-/srv/ltc-data}"
export DATA_DIR="$DATA_ROOT/open-webui"

# 帳號、對話紀錄與知識庫向量一律存 PostgreSQL（不使用 SQLite）。
# 連線字串含密碼，放在資料目錄的設定檔，不進 repo／git。設定檔需提供：
#   DATABASE_URL=postgresql://openwebui:<密碼>@localhost:5432/openwebui
#   PGVECTOR_DB_URL=postgresql://openwebui:<密碼>@localhost:5432/openwebui
ENV_FILE="$DATA_ROOT/open-webui.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "找不到 $ENV_FILE，請先建立（見 README「資料存放」一節）" >&2
    exit 1
fi
set -a
. "$ENV_FILE"
set +a
export VECTOR_DB=pgvector

# 模型連線只接本機：OpenAI 相容端點指向小照後端（預設會指向 api.openai.com，資料外送）。
# 僅在資料庫首次初始化時生效，之後以管理介面設定為準。
export ENABLE_OPENAI_API=true
export OPENAI_API_BASE_URL=http://localhost:8001/v1
export OPENAI_API_KEY=local-no-auth
export OLLAMA_BASE_URL=http://localhost:11434

# 關閉遙測與使用統計回報，資料不外送
export DO_NOT_TRACK=true
export SCARF_NO_ANALYTICS=true
export ANONYMIZED_TELEMETRY=false

# open-webui serve 會在工作目錄讀寫 .webui_secret_key，故切到資料目錄而非 repo
cd "$DATA_DIR" || exit 1
exec "$HOME/.local/bin/open-webui" serve --port 8080
