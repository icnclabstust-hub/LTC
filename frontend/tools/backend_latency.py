"""統計送後端的回覆時間：從 xiaozhao_data/backend_requests-YYYYMMDD.jsonl 讀出每次「送出到收到回覆」的毫秒數。

（2026-10-14 async-backend 第 7 節；回覆目標 3 秒，P95，10/05 會議決議）

用法：python tools/backend_latency.py [記錄檔 …]（不給時讀 xiaozhao_data/ 下全部記錄檔）
"""

import json
import statistics
import sys
from pathlib import Path

TARGET_MS = 3000


def percentile(values: list[float], q: float) -> float:
    values = sorted(values)
    k = (len(values) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def main() -> None:
    files = [Path(a) for a in sys.argv[1:]] or sorted(Path("xiaozhao_data").glob("backend_requests-*.jsonl"))
    groups: dict[str, list[float]] = {}
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            if entry.get("elapsed_ms") is None:
                continue  # 2026-10-14 之前的記錄沒有回覆時間
            payload = entry["payload"]
            if entry["kind"] == "scene":
                key = "scene（一輪紀錄）"
            elif payload.get("line_case_id"):
                key = "LINE 文字提問"
            elif payload.get("text_question_id"):
                key = "LIFF 打字提問"
            else:
                key = "錄音（預取）" if payload.get("provisional") else "錄音（輪結束）"
            groups.setdefault(key, []).append(entry["elapsed_ms"])
    if not groups:
        print("沒有含回覆時間的記錄。")
        return
    print(f"{'來源':<14}{'次數':>6}{'P50(ms)':>10}{'P95(ms)':>10}{'最大(ms)':>10}{'3 秒內':>10}")
    for key, values in groups.items():
        within = sum(v <= TARGET_MS for v in values) / len(values)
        print(f"{key:<14}{len(values):>6}{statistics.median(values):>10.0f}{percentile(values, 0.95):>10.0f}"
              f"{max(values):>10.0f}{within:>10.0%}")


if __name__ == "__main__":
    main()
