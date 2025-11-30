class PCMProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this.buffer = [];
    }

    process(inputs, outputs, parameters) {
        const input = inputs[0];
        if (input.length > 0) {
            const samples = input[0]; // Float32 samples [-1..1]

            // Convert Float32 → Int16 PCM
            const pcm = new Int16Array(samples.length);
            for (let i = 0; i < samples.length; i++) {
                let s = samples[i];
                s = Math.max(-1, Math.min(1, s));
                pcm[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
            }

            this.port.postMessage(pcm.buffer);
        }
        return true;
    }
}

registerProcessor("pcm-processor", PCMProcessor);
