"""模型封裝：VAD、喚醒詞（KWS）、Paraformer ASR、聲紋、意圖 BERT（A1）。

模型檔沿用 android/AweIntentSpike/model-downloads/ 與 exported_bert_care7/A1_s42/model。
共用的模型物件以鎖保護；每個錄音 session 各自持有 VAD 與 KWS 串流的狀態。
"""

import json
import logging
import threading
from pathlib import Path
from typing import Protocol

import numpy as np

from .config import SAMPLE_RATE, VAD_MIN_SILENCE_SECONDS
from .questions import insert_pause_commas

logger = logging.getLogger(__name__)

KWS_DIR = "kws/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
KWS_MODEL_TAG = "epoch-12-avg-2-chunk-16-left-64"
KEYWORDS_FILE = Path(__file__).parent / "resources" / "keywords.txt"
PUNCT_MODEL = "punct/sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8/model.int8.onnx"


class VadStream(Protocol):
    def accept(self, samples: np.ndarray) -> list[tuple[int, np.ndarray]]: ...
    def flush(self) -> list[tuple[int, np.ndarray]]: ...
    def is_speech(self) -> bool: ...


class KwsStream(Protocol):
    def accept(self, samples: np.ndarray) -> list[str]: ...


class Engines(Protocol):
    def new_vad(self, min_silence: float = VAD_MIN_SILENCE_SECONDS) -> VadStream: ...
    def new_kws(self) -> KwsStream: ...
    def transcribe(self, samples: np.ndarray) -> str: ...
    def embed(self, samples: np.ndarray) -> np.ndarray: ...
    def classify(self, text: str) -> tuple[str, float]: ...
    def punctuate(self, text: str) -> str: ...
    # 選用：同時回傳意圖歸屬（N0；A1 回傳 None）。沒有此方法時 pipeline 只呼叫 classify
    # def classify_with_owner(self, text: str, speaker: str | None) -> tuple[str, float, str | None]: ...


class SherpaVad:
    """Silero VAD：逐窗（512 樣本）餵入並取出已切好的句子（start 為 session 內的樣本位置）。"""

    WINDOW = 512

    def __init__(self, model_path: Path, min_silence: float = VAD_MIN_SILENCE_SECONDS) -> None:
        import sherpa_onnx

        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = str(model_path)
        config.silero_vad.min_silence_duration = min_silence
        config.sample_rate = SAMPLE_RATE
        self._vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=120)
        self._pending = np.zeros(0, dtype=np.float32)

    def _drain(self) -> list[tuple[int, np.ndarray]]:
        segments = []
        while not self._vad.empty():
            segments.append((self._vad.front.start, np.array(self._vad.front.samples, dtype=np.float32)))
            self._vad.pop()
        return segments

    def accept(self, samples: np.ndarray) -> list[tuple[int, np.ndarray]]:
        data = np.concatenate([self._pending, samples])
        usable = len(data) - len(data) % self.WINDOW
        segments = []
        for i in range(0, usable, self.WINDOW):
            self._vad.accept_waveform(data[i:i + self.WINDOW])
            segments.extend(self._drain())
        self._pending = data[usable:]
        return segments

    def flush(self) -> list[tuple[int, np.ndarray]]:
        if len(self._pending):
            self._vad.accept_waveform(self._pending)
            self._pending = np.zeros(0, dtype=np.float32)
        self._vad.flush()
        return self._drain()

    def is_speech(self) -> bool:
        return self._vad.is_speech_detected()


class SherpaKwsStream:
    def __init__(self, spotter, lock: threading.Lock) -> None:
        self._spotter = spotter
        self._lock = lock
        self._stream = spotter.create_stream()

    def accept(self, samples: np.ndarray) -> list[str]:
        found = []
        with self._lock:
            self._stream.accept_waveform(SAMPLE_RATE, samples)
            while self._spotter.is_ready(self._stream):
                self._spotter.decode_stream(self._stream)
                keyword = self._spotter.get_result(self._stream)
                if keyword:
                    found.append(keyword)
                    # 偵測到後須重設串流，否則同一段聲音會重複觸發
                    self._spotter.reset_stream(self._stream)
        return found


class SherpaEngines:
    def __init__(self, model_downloads: Path, intent_model_dir: Path, intent_model: str = "a1",
                 n0_model_dir: Path | None = None) -> None:
        import sherpa_onnx
        from opencc import OpenCC

        self._root = model_downloads
        kws_dir = model_downloads / KWS_DIR
        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(kws_dir / "tokens.txt"),
            encoder=str(kws_dir / f"encoder-{KWS_MODEL_TAG}.int8.onnx"),
            decoder=str(kws_dir / f"decoder-{KWS_MODEL_TAG}.int8.onnx"),
            joiner=str(kws_dir / f"joiner-{KWS_MODEL_TAG}.int8.onnx"),
            keywords_file=str(KEYWORDS_FILE),
            # 2026-10-13 以自錄 8 段調校（wake-word-miss spike）：1.5／0.25 命中 6／8，2.0／0.2 命中 7／8，誤觸發皆 0
            keywords_score=2.0,
            keywords_threshold=0.2,
            num_threads=1,
            max_active_paths=4,
        )
        self._kws_lock = threading.Lock()

        asr_dir = model_downloads / "sherpa-onnx-paraformer-zh-2023-09-14"
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_paraformer(
            paraformer=str(asr_dir / "model.int8.onnx"),
            tokens=str(asr_dir / "tokens.txt"),
            num_threads=2,
            sample_rate=SAMPLE_RATE,
        )
        self._asr_lock = threading.Lock()
        # 標點模型：問句切分用（2026-10-14 question-window）
        self._punct = sherpa_onnx.OfflinePunctuation(sherpa_onnx.OfflinePunctuationConfig(
            model=sherpa_onnx.OfflinePunctuationModelConfig(ct_transformer=str(model_downloads / PUNCT_MODEL),
                                                            num_threads=1)))
        self._punct_lock = threading.Lock()
        # Paraformer 原生輸出簡體；意圖模型 A1 以繁體原文訓練，所以先轉成台灣繁體
        self._s2tw = OpenCC("s2twp").convert

        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(model_downloads / "models/embedding/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"),
                num_threads=1,
            )
        )
        self._embed_lock = threading.Lock()

        # 意圖模型：n0（預設）或 a1；N0 模型不存在時改用 A1（2026-10-14 n0-intent-model 第 4.4 節）
        if intent_model == "n0" and n0_model_dir is not None and (n0_model_dir / "spike_meta.json").exists():
            from .intent_n0 import DualHeadIntentClassifier

            self._intent = DualHeadIntentClassifier(n0_model_dir)
        else:
            if intent_model == "n0":
                logger.warning("找不到 N0 意圖模型（%s），改用 A1", n0_model_dir)
            self._intent = BertIntentClassifier(intent_model_dir)

    def new_vad(self, min_silence: float = VAD_MIN_SILENCE_SECONDS) -> SherpaVad:
        return SherpaVad(self._root / "vad" / "silero_vad.onnx", min_silence)

    def new_kws(self) -> SherpaKwsStream:
        return SherpaKwsStream(self._spotter, self._kws_lock)

    def transcribe(self, samples: np.ndarray) -> str:
        with self._asr_lock:
            stream = self._recognizer.create_stream()
            stream.accept_waveform(SAMPLE_RATE, samples)
            self._recognizer.decode_stream(stream)
            result = stream.result
            # 依每個字的開始時間在停頓處補「，」（question-window 第 9 節）；先補再轉繁體，逗號不受轉換影響
            if "".join(result.tokens) == result.text:
                text = insert_pause_commas(list(result.tokens), list(result.timestamps))
            else:
                text = result.text
        return self._s2tw(text)

    def punctuate(self, text: str) -> str:
        with self._punct_lock:
            return self._punct.add_punctuation(text)

    def embed(self, samples: np.ndarray) -> np.ndarray:
        with self._embed_lock:
            stream = self._extractor.create_stream()
            stream.accept_waveform(SAMPLE_RATE, samples)
            stream.input_finished()
            return np.array(self._extractor.compute(stream), dtype=np.float32)

    def classify(self, text: str, speaker: str | None = "照護員") -> tuple[str, float]:
        """LINE 文字訊息、LIFF 打字提問的說話人是照護員。"""
        intent, conf, _ = self._intent.classify_with_owner(text, speaker)
        return intent, conf

    def classify_with_owner(self, text: str, speaker: str | None) -> tuple[str, float, str | None]:
        return self._intent.classify_with_owner(text, speaker)


class BertIntentClassifier:
    """意圖 BERT spike 的 A1 最終模型（hfl/chinese-roberta-wwm-ext、繁體原文、只看本句）。"""

    def __init__(self, model_dir: Path) -> None:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        with open(model_dir / "spike_meta.json", "r", encoding="utf-8") as f:
            meta = json.load(f)
        if meta["text_variant"] != "orig":
            raise ValueError(f"{model_dir} 不是 A1（繁體原文）模型：text_variant={meta['text_variant']}")
        self._labels = meta["labels"]
        self._max_length = meta["max_length"]
        self._torch = torch
        self._tokenizer = AutoTokenizer.from_pretrained(model_dir)
        self._model = AutoModelForSequenceClassification.from_pretrained(model_dir).eval()
        self._lock = threading.Lock()
        logger.info("意圖模型載入完成：%s（%d 類）", model_dir, len(self._labels))

    def classify(self, text: str) -> tuple[str, float]:
        torch = self._torch
        with self._lock, torch.no_grad():
            batch = self._tokenizer(text, truncation=True, max_length=self._max_length, return_tensors="pt")
            probs = torch.softmax(self._model(**batch).logits[0], dim=-1)
            conf, idx = probs.max(dim=-1)
        return self._labels[int(idx)], float(conf)

    def classify_with_owner(self, text: str, speaker: str | None) -> tuple[str, float, None]:
        """A1 只看句子、沒有歸屬輸出。"""
        return (*self.classify(text), None)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom else 0.0
