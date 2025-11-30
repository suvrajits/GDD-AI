# backend/app/speech_engine.py
import threading
import asyncio
import time
from typing import Callable, Optional
import azure.cognitiveservices.speech as speechsdk

class AzureSpeechStream:
    def __init__(self, key: str, region: str):
        # use environment or stub if keys missing
        if not key:
            print("Warning: AZURE_SPEECH_KEY not set — recognizer will be a no-op.")
        self.key = key
        self.region = region

        # push stream for PCM int16
        self.push_stream = None
        self.recognizer = None
        self._thread = None
        self._running = False

        # callbacks (async coroutines)
        self._on_partial = None
        self._on_final = None

        # store main asyncio loop to call back coroutines
        try:
            self._loop = asyncio.get_event_loop()
        except RuntimeError:
            self._loop = None

    def set_callbacks(self, on_partial: Callable, on_final: Callable):
        """Both callbacks must be async functions (coroutines)."""
        self._on_partial = on_partial
        self._on_final = on_final

    def _ensure_config(self):
        if not self.key:
            return None
        speech_config = speechsdk.SpeechConfig(subscription=self.key, region=self.region)
        # force short phrases etc if desired
        speech_config.set_property(speechsdk.PropertyId.SpeechServiceConnection_RealtimeTranscription, "true")
        return speech_config

    def start(self):
        """Start recognizer (runs in background thread)."""
        if self._running:
            return

        # create push stream
        self.push_stream = speechsdk.AudioInputStream.create_push_stream()
        speech_config = self._ensure_config()

        # If key missing, we'll run a dummy thread that does nothing but echo later (no Azure)
        if not speech_config:
            # start dummy thread so push_audio doesn't error
            self._running = True
            self._thread = threading.Thread(target=self._dummy_thread, daemon=True)
            self._thread.start()
            return

        audio_input = speechsdk.AudioConfig(stream=self.push_stream)
        self.recognizer = speechsdk.SpeechRecognizer(speech_config=speech_config, audio_config=audio_input)

        # register events
        self.recognizer.recognizing.connect(self._on_recognizing)
        self.recognizer.recognized.connect(self._on_recognized)
        self.recognizer.session_started.connect(lambda evt: print("Recognizer session started"))
        self.recognizer.session_stopped.connect(lambda evt: print("Recognizer session stopped"))
        self.recognizer.canceled.connect(lambda evt: print("Recognizer canceled:", evt.reason))

        # run recognition in background thread
        self._running = True
        self._thread = threading.Thread(target=self._recognize_thread, daemon=True)
        self._thread.start()

    def _recognize_thread(self):
        try:
            if not self.recognizer:
                return
            self.recognizer.start_continuous_recognition()
            # keep thread alive until stopped
            while self._running:
                time.sleep(0.1)
            try:
                self.recognizer.stop_continuous_recognition()
            except Exception:
                pass
        except Exception as e:
            print("Recognizer thread error:", e)

    def _dummy_thread(self):
        # When Azure keys are missing: do nothing but keep alive
        while self._running:
            time.sleep(0.2)

    def stop(self):
        self._running = False
        try:
            if self.push_stream:
                # send close to Azure
                self.push_stream.close()
        except Exception:
            pass

    async def push_audio(self, data: bytes):
        """Called from async context in main.py. Accepts raw Int16 PCM bytes."""
        # If no azure config, do nothing
        if not self.key:
            return
        if not self.push_stream:
            # safety: create push stream if missing
            self.push_stream = speechsdk.AudioInputStream.create_push_stream()
        # write bytes into push stream
        # push_stream expects bytes for PCM
        try:
            self.push_stream.write(data)
        except Exception as e:
            print("push_audio write error:", e)

    # Event handlers called by Azure SDK thread — they must schedule coroutine callbacks
    def _on_recognizing(self, evt):
        text = evt.result.text
        if self._on_partial and self._loop:
            try:
                asyncio.run_coroutine_threadsafe(self._on_partial(text), self._loop)
            except Exception as e:
                print("Error scheduling on_partial:", e)

    def _on_recognized(self, evt):
        # final result
        if evt.result.reason == speechsdk.ResultReason.RecognizedSpeech:
            text = evt.result.text
            if self._on_final and self._loop:
                try:
                    asyncio.run_coroutine_threadsafe(self._on_final(text), self._loop)
                except Exception as e:
                    print("Error scheduling on_final:", e)
