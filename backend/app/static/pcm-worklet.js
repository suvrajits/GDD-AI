class PCMProcessor extends AudioWorkletProcessor {
    constructor() {
        super();

        // AudioWorkletGlobalScope sampleRate (usually 48000)
        this.inputSampleRate = sampleRate;
        this.outputSampleRate = 16000;
        this.ratio = this.inputSampleRate / this.outputSampleRate;

        this.buffer = [];
        this.FRAME_SIZE = 320; // 20ms at 16kHz (required by Azure STT)
    }

    process(inputs) {
        const input = inputs[0];
        if (!input || input.length === 0) return true;

        const samples = input[0]; // mono

        // Downsample: pick samples based on ratio
        for (let i = 0; i < samples.length; i += this.ratio) {
            const index = Math.floor(i);
            const s = samples[index];
            const clipped = Math.max(-1, Math.min(1, s));
            const int16 = clipped * 32767;
            this.buffer.push(int16);

            // When we reach a full frame, send it
            if (this.buffer.length >= this.FRAME_SIZE) {
                const frame = new Int16Array(this.buffer.slice(0, this.FRAME_SIZE));
                this.buffer = this.buffer.slice(this.FRAME_SIZE);
                this.port.postMessage(frame.buffer);
            }
        }

        return true;
    }
}

registerProcessor("pcm-processor", PCMProcessor);
