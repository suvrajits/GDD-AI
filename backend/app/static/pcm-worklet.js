class PCMProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
  }

  process(inputs, outputs, parameters) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;

    const floatSamples = input[0]; // Float32Array from mic

    // Convert Float32 → Int16 PCM
    const pcmBuffer = new ArrayBuffer(floatSamples.length * 2);
    const pcmView = new DataView(pcmBuffer);

    for (let i = 0; i < floatSamples.length; i++) {
      let s = Math.max(-1, Math.min(1, floatSamples[i]));
      pcmView.setInt16(i * 2, s * 0x7fff, true);
    }

    // Send raw PCM bytes back to main thread
    this.port.postMessage(pcmBuffer);

    return true;
  }
}

registerProcessor("pcm-processor", PCMProcessor);
