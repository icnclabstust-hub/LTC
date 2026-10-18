"""前後文意圖模型 N0（不帶前文）的服務端推論（2026-10-14 n0-intent-model 設計文件第 4.1 節）。

模型由 train_context_intent_classifier.py 訓練（exported_ctx_care7/N0_s42/model）：一個編碼器、兩個輸出頭
（意圖 7 類、意圖歸屬 5 種角色）；輸入為「說話人：句子」。這裡只做推論，不依賴訓練腳本（訓練腳本需要 jieba 等套件）。
"""

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

UNKNOWN_SPEAKER = "未辨識"


class DualHeadIntentClassifier:
    def __init__(self, model_dir: Path) -> None:
        import torch
        from safetensors.torch import load_file
        from transformers import AutoModel, AutoTokenizer

        with open(model_dir / "spike_meta.json", encoding="utf-8") as f:
            meta = json.load(f)
        if meta.get("context") != 0:
            raise ValueError(f"{model_dir} 不是不帶前文的 N0 模型：context={meta.get('context')}")
        self._labels, self._roles, self._max_length = meta["labels"], meta["roles"], meta["max_length"]
        self._torch = torch
        self._tokenizer = AutoTokenizer.from_pretrained(model_dir / "encoder")
        self._encoder = AutoModel.from_pretrained(model_dir / "encoder").eval()
        heads = load_file(str(model_dir / "heads.safetensors"))
        hidden = self._encoder.config.hidden_size
        self._intent_head = torch.nn.Linear(hidden, len(self._labels))
        self._owner_head = torch.nn.Linear(hidden, len(self._roles))
        self._intent_head.load_state_dict({k.split(".", 1)[1]: v for k, v in heads.items() if k.startswith("intent_head.")})
        self._owner_head.load_state_dict({k.split(".", 1)[1]: v for k, v in heads.items() if k.startswith("owner_head.")})
        self._lock = threading.Lock()
        logger.info("意圖模型載入完成：%s（N0 雙輸出頭，%d 類）", model_dir, len(self._labels))

    def speaker_label(self, speaker: str | None) -> str:
        """訓練時的說話人只有 5 種角色；其餘（含尚未對應身分）一律用「未辨識」（設計文件第 5 節）。"""
        return speaker if speaker in self._roles else UNKNOWN_SPEAKER

    def classify_with_owner(self, text: str, speaker: str | None) -> tuple[str, float, str]:
        torch = self._torch
        with self._lock, torch.no_grad():
            batch = self._tokenizer(f"{self.speaker_label(speaker)}：{text}", truncation=True,
                                    max_length=self._max_length, return_tensors="pt")
            out = self._encoder(**batch)
            pooled = out.pooler_output if out.pooler_output is not None else out.last_hidden_state[:, 0]
            intent_prob = torch.softmax(self._intent_head(pooled)[0], dim=-1)
            owner_prob = torch.softmax(self._owner_head(pooled)[0], dim=-1)
        conf, idx = intent_prob.max(dim=-1)
        return self._labels[int(idx)], float(conf), self._roles[int(owner_prob.argmax())]
