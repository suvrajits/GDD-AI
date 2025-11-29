import asyncio
import azure.cognitiveservices.speech as speechsdk
from typing import Callable


class AzureSpeechStream:
    """
    Streams raw 16kHz 16-bit mono PCM into Azure STT.
    Emits partial + final via async callbacks.
    """

    def __init__(self, speech_key: str, region: str):
        # -------- SPEECH CONFIG --------
        self.speech_config = speechsdk.SpeechConfig(
            subscription=speech_key,
            region=region
        )

        # Get detailed hypotheses
        self.speech_config.output_format = speechsdk.OutputFormat.Detailed

        # -------- CORRECT PCM FORMAT --------
        pcm_format = speechsdk.audio.AudioStreamFormat(
            samples_per_second=16000,
            bits_per_sample=16,
            channels=1
        )

        # MUST attach format here
        self.audio_stream: speechsdk.audio.PushAudioInputStream = (
            speechsdk.audio.PushAudioInputStream(stream_format=pcm_format)
        )

        audio_config = speechsdk.audio.AudioConfig(stream=self.audio_stream)

        # -------- RECOGNIZER --------
        self.recognizer = speechsdk.SpeechRecognizer(
            speech_config=self.speech_config,
            audio_config=audio_config
        )

        # Callback placeholders
        self.partial_callback: Callable = None
        self.final_callback: Callable = None

    # -----------------------------------------------------------
    def set_callbacks(self, partial_cb, final_cb):
        self.partial_callback = partial_cb
        self.final_callback = final_cb

    # -----------------------------------------------------------
    def start(self):
        """
        Azure SDK event model → non-async.
        """
        loop = asyncio.get_event_loop()

        # PARTIAL
        def on_partial(evt):
            if evt.result.text and self.partial_callback:
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(
                        self.partial_callback(evt.result.text)
                    )
                )

        # FINAL
        def on_final(evt):
            if evt.result.text and self.final_callback:
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(
                        self.final_callback(evt.result.text)
                    )
                )

        # Wire events
        self.recognizer.recognizing.connect(on_partial)
        self.recognizer.recognized.connect(on_final)

        # Start continuous — DO NOT await, runs in SDK thread
        self.recognizer.start_continuous_recognition_async()

    # -----------------------------------------------------------
    def stop(self):
        try:
            self.recognizer.stop_continuous_recognition_async()
            self.audio_stream.close()
        except Exception as e:
            print("Error stopping AzureSpeechStream:", e)

    # -----------------------------------------------------------
    async def push_audio(self, audio_bytes: bytes):
        """
        Browser sends Int16 PCM → we write raw PCM bytes directly.
        """
        try:
            self.audio_stream.write(audio_bytes)
        except Exception as e:
            print("Write error:", e)
