import logging
import shutil
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, List

import whisper  # type: ignore[import-untyped]
from openai import OpenAI
from openai.types.audio.transcription_segment import TranscriptionSegment
from pydantic import BaseModel

from podcast_processor.audio import split_audio
from shared.config import RemoteWhisperConfig, GroqWhisperConfig
from groq import Groq


class Segment(BaseModel):
    start: float
    end: float
    text: str


class Transcriber(ABC):
    @abstractmethod
    def transcribe(self, audio_file_path: str) -> List[Segment]:
        pass


class LocalTranscriptSegment(BaseModel):
    id: int
    seek: int
    start: float
    end: float
    text: str
    tokens: List[int]
    temperature: float
    avg_logprob: float
    compression_ratio: float
    no_speech_prob: float

    def to_segment(self) -> Segment:
        return Segment(start=self.start, end=self.end, text=self.text)


class TestWhisperTranscriber(Transcriber):
    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def transcribe(self, _: str) -> List[Segment]:
        self.logger.info("Using test whisper")
        return [
            Segment(start=0, end=1, text="This is a test"),
            Segment(start=1, end=2, text="This is another test"),
        ]


class LocalWhisperTranscriber(Transcriber):
    def __init__(self, logger: logging.Logger, whisper_model: str):
        self.logger = logger
        self.whisper_model = whisper_model

    @staticmethod
    def convert_to_pydantic(
        transcript_data: List[Any],
    ) -> List[LocalTranscriptSegment]:
        return [LocalTranscriptSegment(**item) for item in transcript_data]

    @staticmethod
    def local_seg_to_seg(local_segments: List[LocalTranscriptSegment]) -> List[Segment]:
        return [seg.to_segment() for seg in local_segments]

    def transcribe(self, audio_file_path: str) -> List[Segment]:
        self.logger.info("Using local whisper")
        models = whisper.available_models()
        self.logger.info(f"Available models: {models}")

        model = whisper.load_model(name=self.whisper_model)

        self.logger.info("Beginning transcription")
        start = time.time()
        result = model.transcribe(audio_file_path, fp16=False, language="English")
        end = time.time()
        elapsed = end - start
        self.logger.info(f"Transcription completed in {elapsed}")
        segments = result["segments"]
        typed_segments = self.convert_to_pydantic(segments)

        return self.local_seg_to_seg(typed_segments)


class GroqTranscriber(Transcriber):
    def __init__(self, logger: logging.Logger, config: GroqWhisperConfig):
        self.logger = logger
        self.config = config
        self.client = Groq(api_key=config.api_key, timeout=config.timeout_sec)

    def transcribe(self, audio_file_path: str) -> List[Segment]:
        self.logger.info(f"Transcribing with Groq model: {self.config.model}")
        start_time = time.time()
        try:
            with open(audio_file_path, "rb") as audio_file:
                transcription = self.client.audio.transcriptions.create(
                    file=(audio_file_path, audio_file.read()),
                    model=self.config.model,
                    response_format="verbose_json",
                    # timestamp_granularities=["segment"], # seems groq whisper doesn't support this yet
                )
        except Exception as e:
            self.logger.error(f"Groq transcription failed: {e}")
            raise

        end_time = time.time()
        elapsed = end_time - start_time
        self.logger.info(f"Groq transcription completed in {elapsed:.2f} seconds")

        # Assuming Groq's verbose_json output is compatible with OpenAI's
        # and contains a 'segments' list.
        # If the structure is different, this mapping needs adjustment.
        segments_data = getattr(transcription, "segments", None)
        if segments_data is None:
            self.logger.warning(
                "Groq response did not contain 'segments'. Trying full text." + str(transcription)
            )
            # Fallback or specific handling if segments are not available
            # For now, returning a single segment with the full text if available
            full_text = getattr(transcription, "text", "")
            if full_text:
                # We don't have timing info, so we can't accurately create segments.
                # Returning a single segment might break downstream processing.
                # Consider raising an error or returning an empty list.
                self.logger.error("Cannot create segments from Groq response without segment data.")
                return [] # Or raise an exception
            else:
                 self.logger.error("Groq response had neither segments nor text.")
                 return []


        return [
            Segment(start=seg["start"], end=seg["end"], text=seg["text"])
            for seg in segments_data
        ]


class RemoteWhisperTranscriber(Transcriber):
    def __init__(self, logger: logging.Logger, config: RemoteWhisperConfig):
        self.logger = logger
        self.config = config

        self.openai_client = OpenAI(
            base_url=config.base_url,
            api_key=config.api_key,
            timeout=config.timeout_sec,
        )

    def transcribe(self, audio_file_path: str) -> List[Segment]:
        self.logger.info("Using remote whisper")
        audio_chunk_path = audio_file_path + "_parts"

        chunks = split_audio(
            Path(audio_file_path),
            Path(audio_chunk_path),
            self.config.chunksize_mb * 1024 * 1024,
        )

        all_segments: List[TranscriptionSegment] = []

        for chunk in chunks:
            chunk_path, offset = chunk
            segments = self.get_segments_for_chunk(str(chunk_path))
            all_segments.extend(self.add_offset_to_segments(segments, offset))

        shutil.rmtree(audio_chunk_path)
        return self.convert_segments(all_segments)

    @staticmethod
    def convert_segments(segments: List[TranscriptionSegment]) -> List[Segment]:
        return [
            Segment(
                start=seg.start,
                end=seg.end,
                text=seg.text,
            )
            for seg in segments
        ]

    @staticmethod
    def add_offset_to_segments(
        segments: List[TranscriptionSegment], offset_ms: int
    ) -> List[TranscriptionSegment]:
        offset_sec = float(offset_ms) / 1000.0
        for segment in segments:
            segment.start += offset_sec
            segment.end += offset_sec

        return segments

    def get_segments_for_chunk(self, chunk_path: str) -> List[TranscriptionSegment]:
        with open(chunk_path, "rb") as f:
            self.logger.info(f"Transcribing chunk {chunk_path}")

            transcription = self.openai_client.audio.transcriptions.create(
                model=self.config.model,
                file=f,
                timestamp_granularities=["segment"],
                language=self.config.language,
                response_format="verbose_json",
            )

            self.logger.debug("Got transcription")

            segments = transcription.segments
            assert segments is not None

            self.logger.debug(f"Got {len(segments)} segments")

            return segments
