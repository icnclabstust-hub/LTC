// 錄音 AudioWorklet：把麥克風音訊轉成 16 kHz、16-bit PCM，每 0.5 秒送出一個片段。
// AudioContext 若無法以 16 kHz 建立（部分瀏覽器忽略 sampleRate），在此以區間平均降頻。
// 外接麥克風為雙聲道時，所有聲道先平均成一路（external-mic 第 4.3 節）。

const TARGET_RATE = 16000;
const CHUNK_SAMPLES = TARGET_RATE / 2;

class PcmChunker extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / TARGET_RATE;
    this.buffer = new Int16Array(CHUNK_SAMPLES);
    this.filled = 0;
    this.acc = 0;
    this.accCount = 0;
    this.pos = 0;
    this.port.onmessage = (e) => {
      if (e.data === 'flush') this.flush();
    };
  }

  push(value) {
    const clamped = Math.max(-1, Math.min(1, value));
    this.buffer[this.filled++] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    if (this.filled === CHUNK_SAMPLES) this.flush();
  }

  flush() {
    if (this.filled === 0) return;
    const out = this.buffer.slice(0, this.filled);
    this.port.postMessage(out.buffer, [out.buffer]);
    this.filled = 0;
  }

  process(inputs) {
    // 所有聲道平均成一路：雙聲道的外接麥克風（兩個發射器分軌）兩邊的聲音都保留（external-mic 第 4.3 節）
    const input = inputs[0];
    if (!input || !input.length) return true;
    const channels = input.length;
    for (let i = 0; i < input[0].length; i++) {
      let value = 0;
      for (let c = 0; c < channels; c++) value += input[c][i];
      this.sample(value / channels);
    }
    return true;
  }

  sample(value) {
    if (this.ratio === 1) {
      this.push(value);
      return;
    }
    this.acc += value;
    this.accCount++;
    this.pos++;
    if (this.pos >= this.ratio) {
      this.push(this.acc / this.accCount);
      this.pos -= this.ratio;
      this.acc = 0;
      this.accCount = 0;
    }
  }
}

registerProcessor('pcm-chunker', PcmChunker);
