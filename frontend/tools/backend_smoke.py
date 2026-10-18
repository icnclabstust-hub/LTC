"""對真的 LTC-BE 跑一次串接冒煙測試：建帳號 → 綁定 → 依題型提問，印出狀態與拒答原因。

會在後端建立一個測試帳號（名稱「冒煙測試」），請只對本機開發用的後端執行。
用法（frontend/ 目錄）：python tools/backend_smoke.py --url http://127.0.0.1:8001 --token <BACKEND_TOKEN>
"""

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xiaozhao import services  # noqa: E402
from xiaozhao.backend import HttpBackend  # noqa: E402
from xiaozhao.db import Database  # noqa: E402
from xiaozhao.identity import BackendIdentity  # noqa: E402

QUESTIONS = (
    "你是誰",  # 身分（不經檢索）
    "個案昨天漏吃一次安眠藥，今天可以補吃嗎？",  # sensitive → 轉督導
    "他可以用嗎？",  # ambiguous → 追問
    "台積電股票現在可以買嗎？",  # out_of_scope（需要檢索）
    "喘息服務怎麼申請？",  # 一般問題（需要檢索＋生成）
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8001")
    parser.add_argument("--token", required=True)
    args = parser.parse_args()

    identity = BackendIdentity(args.url, args.token)
    db = Database(":memory:")
    _, code = services.create_account(db, "冒煙測試", "caregiver", identity)
    line_user_id = f"U_smoke_{uuid.uuid4().hex}"
    services.bind(db, code, line_user_id, identity)
    caller = services.caller_fields(db, line_user_id)
    print(f"身分：{caller}")

    backend = HttpBackend(args.url, token=args.token, poll_seconds=0.5, timeout_seconds=120)
    for text in QUESTIONS:
        result = backend.query({"client_request_id": uuid.uuid4().hex, **caller, "text": text,
                                "intent": None, "intent_conf": None, "keywords": [], "provisional": False})
        print(f"\n【{text}】{result.status}｜{result.refusal_reason or '—'}｜{result.reason or ''}")
        print((result.answer or "")[:300])


if __name__ == "__main__":
    main()
