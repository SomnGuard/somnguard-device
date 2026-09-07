from __future__ import annotations
import asyncio
import logging
import math
import sys
import struct
from typing import Optional

try:
    import simpleaudio as sa
    import numpy as np
    HAS_SIMPLEAUDIO = True
except ImportError:
    sa = None
    np = None
    HAS_SIMPLEAUDIO = False

if sys.platform == "win32":
    try:
        import winsound
        HAS_WINSOUND = True
    except ImportError:
        HAS_WINSOUND = False
else:
    HAS_WINSOUND = False

from app.common.models import DeviceConfig, SoundPattern, AlertCode
from app.common.config import get_sound_pattern


logger = logging.getLogger(__name__)


class SoundPlayer:
    def __init__(self, config: DeviceConfig):
        self.config = config
        self._playing: Optional[object] = None
        self._sample_rate = 44100
        self._use_winsound = HAS_WINSOUND and not HAS_SIMPLEAUDIO

    def update_config(self, config: DeviceConfig) -> None:
        self.config = config

    def effective_volume(self, pattern: SoundPattern) -> float:
        """Volumen del patrón × escala global remota (AC-005), acotado [0, 1]."""
        scale = getattr(self.config, "volume_scale", 1.0) or 1.0
        return min(1.0, max(0.0, pattern.volume * scale))

    async def play(self, pattern: SoundPattern) -> None:
        await self.stop()

        if self._use_winsound:
            await self._play_winsound(pattern)
        else:
            await self._play_simpleaudio(pattern)

    async def _play_winsound(self, pattern: SoundPattern) -> None:
        # Nota: winsound.Beep no soporta volumen; la escala global solo aplica
        # al backend simpleaudio/Linux y al cálculo effective_volume().
        freq = pattern.frequency_hz
        duration_ms = int(pattern.duration_sec * 1000)

        def play_sequence():
            for rep in range(pattern.repetitions):
                f = freq
                if pattern.alert_code == AlertCode.AS_09 and rep == 1:
                    f = 700
                winsound.Beep(f, duration_ms)
                if rep < pattern.repetitions - 1 and pattern.interval_sec > 0:
                    import time
                    time.sleep(pattern.interval_sec)

        loop = asyncio.get_running_loop()
        self._playing = await loop.run_in_executor(None, play_sequence)

    async def _play_simpleaudio(self, pattern: SoundPattern) -> None:
        if not HAS_SIMPLEAUDIO:
            logger.warning("simpleaudio no disponible, saltando reproducción")
            return

        audio_data = self._build_audio_numpy(pattern)
        try:
            audio_data = (audio_data * 32767).astype(np.int16)
            wave_obj = sa.WaveObject(audio_data.tobytes(), 1, 2, self._sample_rate)
            self._playing = wave_obj.play()
            if not pattern.loop:
                await asyncio.get_running_loop().run_in_executor(None, self._playing.wait_done)
        except Exception as e:
            logger.error("Error reproduciendo sonido: %s", e)

    def _build_audio_numpy(self, pattern: SoundPattern) -> "np.ndarray":
        total_duration = self._calc_total_duration(pattern)
        num_samples = int(total_duration * self._sample_rate)
        t = np.linspace(0, total_duration, num_samples, endpoint=False)
        audio = np.zeros(num_samples, dtype=np.float32)

        fade_samples = int(0.01 * self._sample_rate)
        offset = 0.0

        for rep in range(pattern.repetitions):
            freq = pattern.frequency_hz
            if pattern.alert_code == AlertCode.AS_09 and rep == 1:
                freq = 700

            tone_duration = pattern.duration_sec
            tone_samples = int(tone_duration * self._sample_rate)
            tone = np.sin(2 * np.pi * freq * t[offset:offset + tone_samples])

            # Envelope
            env = np.ones(tone_samples, dtype=np.float32)
            env[:fade_samples] = np.linspace(0, 1, fade_samples)
            env[-fade_samples:] = np.linspace(1, 0, fade_samples)
            tone *= env * self.effective_volume(pattern)

            audio[offset:offset + tone_samples] = tone
            offset += tone_duration + pattern.interval_sec

        return audio

    def _calc_total_duration(self, pattern: SoundPattern) -> float:
        if pattern.loop:
            return pattern.duration_sec * pattern.repetitions + pattern.interval_sec * (pattern.repetitions - 1) + 1.0
        return pattern.duration_sec * pattern.repetitions + pattern.interval_sec * (pattern.repetitions - 1)

    async def stop(self) -> None:
        if self._playing:
            if HAS_SIMPLEAUDIO and hasattr(self._playing, 'is_playing') and self._playing.is_playing():
                self._playing.stop()
            self._playing = None