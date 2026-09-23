"""Speech-to-text with faster-whisper (CTranslate2).

Audio is decoded and resampled to 16 kHz mono by faster-whisper's bundled PyAV (FFmpeg libraries),
so any common video/audio container works without a system ffmpeg binary. VAD filtering skips
silence, which both speeds up transcription and avoids hallucinated text on silent stretches.
"""

import threading
from dataclasses import dataclass

from app.config import get_settings
from app.core.logging import get_logger
from app.services.chunking import Segment

logger = get_logger(__name__)

_model = None
_model_lock = threading.Lock()


@dataclass
class Transcript:
    segments: list[Segment]
    language: str
    duration_s: float

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments)


def _get_model():
    """Load the Whisper model once per process (loading takes seconds; transcribing reuses it)."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from faster_whisper import WhisperModel

                s = get_settings()
                logger.info("loading whisper model", extra={"size": s.whisper_model_size})
                _model = WhisperModel(
                    s.whisper_model_size, device=s.whisper_device, compute_type=s.whisper_compute_type
                )
    return _model


def transcribe(path: str) -> Transcript:
    model = _get_model()
    segments_iter, info = model.transcribe(path, vad_filter=True, beam_size=5)
    segments = [Segment(start=s.start, end=s.end, text=s.text.strip()) for s in segments_iter]
    return Transcript(segments=segments, language=info.language, duration_s=info.duration)
