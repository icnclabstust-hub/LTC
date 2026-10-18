"""設定：讀取 repo 根目錄的 .env 與固定常數。

金鑰只放 .env（已列入 .gitignore）；本模組不記錄任何金鑰內容。
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_DOWNLOADS = REPO_ROOT / "android" / "AweIntentSpike" / "model-downloads"

SAMPLE_RATE = 16000

# 輪的結束條件：靜音 3 秒（設計文件 2.1 節路線 1 第 3 項）
TURN_SILENCE_SECONDS = 3.0
# 切句用的 VAD 靜音長度：0.25 秒會把句中停頓切成兩句（2026-10-12 手機實測），改為 0.5 秒
VAD_MIN_SILENCE_SECONDS = 0.5
# 自適應模式（2026-10-12 adaptive-endpointing spike）：VAD 先以較短靜音細切，再依說話人門檻合併
ADAPTIVE_VAD_MIN_SILENCE_SECONDS = 0.25
ENDPOINT_MODES = ("fixed", "adaptive", "pause_veto")

# 說話人：每次錄音內匿名代號 01、02…（整句聲紋即時分群、只存在記憶體），不再依住民長期保存聲紋
# （2026-10-13 correction-and-cases-tabs；跨錄音聲紋辨識不可靠，見 speaker-identification 設計文件第 4 節）

# 確認／修正紀錄（records）：未經督導確認者保存 7 天（與督導回聽音訊的上限一致）；
# 已確認者於後端取走後 30 天、或確認後一直未被取走時最長 90 天刪除（P3）
RECORD_UNCONFIRMED_TTL_SECONDS = 7 * 24 * 3600
# 匿名說話人分群門檻：與切句實驗用的 endpointing.GROUP_THRESHOLD 分開。
# 2026-10-13 評估（10 段人工標註錄音）：0.5 分群 F1 0.785（同一代號是同一人 0.685、同一人在同一代號 0.919）；
# 0.6 為 0.835（0.793、0.883）。曾改為 0.6，提案學生同日指示改回 0.5
SPEAKER_GROUP_THRESHOLD = 0.5
RECORD_AFTER_EXPORT_TTL_SECONDS = 30 * 24 * 3600
RECORD_CONFIRMED_MAX_TTL_SECONDS = 90 * 24 * 3600
EXPORT_MAX_LIMIT = 500

# 觸發預取的意圖類別（cache-strategy 第 4 節，待老師確認）
PREFETCH_INTENTS = frozenset({"規範詢問", "服務請求", "身體不適陳述"})

# 保存期限（設計文件 2.1 節「資料保存」）
RECORD_TTL_SECONDS = 24 * 3600
BINDING_CODE_TTL_SECONDS = 24 * 3600
CLEANUP_INTERVAL_SECONDS = 600

ROLES_SPEAKER = ("照護員", "家屬", "長輩", "督導", "其他工作人員", "未辨識")
# 意圖七類（BERT A1 的標籤，順序依 spike_meta.json）
INTENT_LABELS = ("情緒安撫或閒聊", "拒絕或抗拒", "服務請求", "確認或同意", "規範詢問", "資訊告知或交接", "身體不適陳述")
WAKE_KEYWORD = "小照你好"
# KWS 漏抓時的補抓：輪外片段只轉開頭這麼多秒、比對喚醒詞，結果不保存；0 表示關閉（2026-10-13 wake-word-miss spike）
WAKE_ASR_FALLBACK_SECONDS = 2.0
# 結束詞：原「小照給我回答結果」太長，2026-10-12 老師指示改短；「給我結果」實測後再改為「小照請回答」
ANSWER_KEYWORD = "小照請回答"


def _load_dotenv(path: Path) -> None:
    """簡易 .env 讀取：KEY=VALUE，# 開頭為註解；已存在的環境變數不覆蓋。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _split_ids(value: str) -> frozenset[str]:
    return frozenset(v.strip() for v in value.split(",") if v.strip())


@dataclass(frozen=True)
class Settings:
    channel_secret: str
    channel_access_token: str
    login_channel_id: str
    liff_id: str
    ngrok_domain: str
    admin_user_ids: frozenset[str]
    data_dir: Path
    intent_model_dir: Path
    prefetch_threshold: float
    backend_mode: str
    port: int
    # 意圖模型：n0（雙輸出頭，2026-10-14 n0-intent-model）或 a1（回滾用）
    intent_model: str = "n0"
    n0_model_dir: Path | None = None
    # 切句／切輪門檻：fixed（固定 0.5／3 秒）、adaptive（依說話人語速調整）或 pause_veto（0.5 秒＋沒說完不切）
    endpoint_mode: str = "fixed"
    # 片段中間的喚醒詞／結束詞依位置切輪（2026-10-13 keyword-split-turns）；False 恢復以整個片段處理
    keyword_split: bool = True
    # 輪內音訊存到伺服器供督導回聽（correction-and-cases-tabs 第 4.7 節）；督導確認即刪、最長 7 天
    keep_turn_audio: bool = True
    # 外接麥克風音量偏小時，伺服器自動放大到手機的水準（2026-10-14 external-mic 第 8 節）
    auto_gain: bool = True
    # 以問句為單位切句，問句附相關前文送後端（2026-10-14 question-window）
    question_window: bool = True
    # 後端抓取已確認紀錄用的權杖（GET /api/export/records）；空字串表示關閉匯出
    export_token: str = ""
    # 把送給後端的 JSON 與回應寫入 xiaozhao_data/backend_requests-YYYYMMDD.jsonl（測試用，保存 1 天）
    backend_log: bool = True
    # 依後端組 API 合約串接（BACKEND_MODE=http，2026-10-14 http-backend）；權杖只放 .env
    backend_url: str = ""
    backend_token: str = ""
    backend_poll_seconds: float = 0.1
    # 最長等待（逾時轉督導）；回覆目標 3 秒（P95，10/05 會議決議）由 reply_target_seconds 處理
    backend_timeout_seconds: float = 30.0
    backend_scene_text: bool = True
    # 後端呼叫在背景執行（http 模式；2026-10-14 async-backend）；超過 reply_target_seconds 先告知「還在查詢」
    backend_async: bool = True
    backend_workers: int = 4
    reply_target_seconds: float = 3.0
    # 測試用：略過 LINE ID token 驗證，直接把 Bearer 值當成 userId（只允許綁 127.0.0.1 時使用）
    dev_fake_auth: bool = False
    model_downloads: Path = field(default=MODEL_DOWNLOADS)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "xiaozhao.sqlite3"

    @property
    def turn_audio_dir(self) -> Path:
        return self.data_dir / "turn_audio"

    @property
    def liff_url(self) -> str:
        return f"https://liff.line.me/{self.liff_id}"


def load_settings() -> Settings:
    _load_dotenv(REPO_ROOT / ".env")
    env = os.environ
    return Settings(
        channel_secret=env.get("LINE_CHANNEL_SECRET", ""),
        channel_access_token=env.get("LINE_CHANNEL_ACCESS_TOKEN", ""),
        login_channel_id=env.get("LINE_LOGIN_CHANNEL_ID", ""),
        liff_id=env.get("LIFF_ID", ""),
        ngrok_domain=env.get("NGROK_DOMAIN", ""),
        admin_user_ids=_split_ids(env.get("LINE_ADMIN_USER_IDS", "")),
        data_dir=Path(env.get("XIAOZHAO_DATA_DIR", REPO_ROOT / "xiaozhao_data")),
        intent_model_dir=Path(env.get("INTENT_MODEL_DIR", REPO_ROOT / "exported_bert_care7" / "A1_s42" / "model")),
        intent_model=env.get("INTENT_MODEL", "n0").lower(),
        n0_model_dir=Path(env.get("N0_MODEL_DIR", REPO_ROOT / "exported_ctx_care7" / "N0_s42" / "model")),
        # BERT 信心偏高，門檻須以交叉驗證資料訂定（cache-strategy 第 4 節）；0.9 只是暫定值
        prefetch_threshold=float(env.get("PREFETCH_THRESHOLD", "0.9")),
        backend_mode=env.get("BACKEND_MODE", "mock"),
        port=int(env.get("XIAOZHAO_PORT", "8010")),
        endpoint_mode=_endpoint_mode(env.get("ENDPOINT_MODE", "fixed")),
        keyword_split=env.get("KEYWORD_SPLIT", "1") != "0",
        keep_turn_audio=env.get("KEEP_TURN_AUDIO", "1") != "0",
        auto_gain=env.get("AUTO_GAIN", "1") != "0",
        question_window=env.get("QUESTION_WINDOW", "1") != "0",
        export_token=env.get("EXPORT_TOKEN", ""),
        backend_log=env.get("BACKEND_LOG", "1") != "0",
        backend_url=env.get("BACKEND_URL", ""),
        backend_token=env.get("BACKEND_TOKEN", ""),
        backend_poll_seconds=float(env.get("BACKEND_POLL_SECONDS", "0.1")),
        backend_timeout_seconds=float(env.get("BACKEND_TIMEOUT_SECONDS", "30")),
        backend_scene_text=env.get("BACKEND_SCENE_TEXT", "1") != "0",
        backend_async=env.get("BACKEND_ASYNC", "1") != "0",
        backend_workers=int(env.get("BACKEND_WORKERS", "4")),
        reply_target_seconds=float(env.get("REPLY_TARGET_SECONDS", "3")),
        dev_fake_auth=env.get("XIAOZHAO_DEV_FAKE_AUTH", "") == "1",
    )


def _endpoint_mode(value: str) -> str:
    if value not in ENDPOINT_MODES:
        raise ValueError(f"ENDPOINT_MODE 須為 {'／'.join(ENDPOINT_MODES)}，目前為 {value!r}")
    return value
