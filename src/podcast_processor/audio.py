from pathlib import Path
from typing import Any, List, Optional, Tuple

import ffmpeg  # type: ignore[import-untyped]

from app import logger # Import the logger


def get_audio_duration_ms(file_path: str) -> Optional[int]:
    """Gets the audio duration in milliseconds using ffmpeg.probe."""
    try:
        probe = ffmpeg.probe(file_path)
        format_info = probe["format"]
        duration_seconds = float(format_info["duration"])
        duration_milliseconds = duration_seconds * 1000
        return int(duration_milliseconds)
    except ffmpeg.Error as e:
        stderr_output = e.stderr.decode('utf-8', errors='replace') if hasattr(e, 'stderr') and e.stderr else "No stderr available"
        logger.error(f"An ffmpeg error occurred while trying to probe the file {file_path}: {stderr_output}")
        return None
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error(f"Unexpected error probing file {file_path}: {e}")
        return None


def _calculate_keep_intervals(
    ad_segments_ms: List[Tuple[int, int]], audio_duration_ms: int
) -> List[Tuple[int, int]]:
    """Calculates the time intervals of audio segments to keep."""
    keep_intervals = []
    current_start_ms = 0
    sorted_ads = sorted(
        [
            (max(0, start), min(audio_duration_ms, end))
            for start, end in ad_segments_ms
            if start < end
        ]
    )
    merged_ads = []
    if not sorted_ads:
        if audio_duration_ms > 0:
           return [(0, audio_duration_ms)]
        else:
           return []

    current_ad_start, current_ad_end = sorted_ads[0]

    for next_ad_start, next_ad_end in sorted_ads[1:]:
        if next_ad_start <= current_ad_end: # Overlap or adjacent
            current_ad_end = max(current_ad_end, next_ad_end)
        else: # Gap between ads
            merged_ads.append((current_ad_start, current_ad_end))
            current_ad_start, current_ad_end = next_ad_start, next_ad_end
    merged_ads.append((current_ad_start, current_ad_end)) # Add the last merged ad

    # Calculate keep intervals based on merged ads
    last_keep_end = 0
    for ad_start, ad_end in merged_ads:
         # Clamp ad_start just in case validation wasn't perfect
        ad_start_clamped = max(0, ad_start)
        if ad_start_clamped > last_keep_end:
            keep_intervals.append((last_keep_end, ad_start_clamped))
        # Ensure ad_end is also clamped and progresses time
        last_keep_end = max(last_keep_end, min(audio_duration_ms, ad_end))

    if last_keep_end < audio_duration_ms:
        keep_intervals.append((last_keep_end, audio_duration_ms))

    # Filter out zero-duration intervals that might arise from edge cases
    return [(s, e) for s, e in keep_intervals if e > s]


def _create_silent_file(out_path: str) -> None:
    """Creates a minimal silent MP3 file."""
    logger.warning(f"No segments to keep/process for {out_path}. Outputting minimal silent file.")
    try:
        sample_rate = 44100
        (
            ffmpeg.input(
                f"anullsrc=channel_layout=mono:sample_rate={sample_rate}",
                f="lavfi",
                t=0.001,
            )
            .output(out_path, acodec="libmp3lame", ar=sample_rate, ac=1, ab="16k")
            .overwrite_output()
            .run(quiet=True)
        )
    except ffmpeg.Error as e:
        stderr_output = e.stderr.decode('utf-8', errors='replace') if hasattr(e, 'stderr') and e.stderr else str(e)
        logger.error(f"Error creating silent file '{out_path}': {stderr_output}")
        raise
    except Exception as e:
         logger.error(f"Unexpected error creating silent file '{out_path}': {e}")
         raise


def clip_segments_with_fade(
    ad_segments_ms: List[Tuple[int, int]],
    fade_ms: int,
    in_path: str,
    out_path: str,
) -> None:
    """
    Clips out specified ad segments using precise timing (aselect), applying fades.
    Handles ads starting at time 0 correctly.
    Builds the output by concatenating keep segments and appropriate faded ad portions.
    """
    try:
        audio_duration_ms = get_audio_duration_ms(in_path)
        if audio_duration_ms is None:
            raise ValueError(f"Could not determine duration of {in_path}")
        if audio_duration_ms <= 0:
             _create_silent_file(out_path)
             logger.warning(f"Input file {in_path} has zero or negative duration. Outputting silent file.")
             return

        keep_intervals = _calculate_keep_intervals(ad_segments_ms, audio_duration_ms)

        if not keep_intervals:
            _create_silent_file(out_path)
            logger.warning(f"No audio segments to keep for {in_path} based on ad times. Outputting silent file.")
            return

        in_stream = ffmpeg.input(in_path)
        processed_streams = []

        for i, (keep_start_ms, keep_end_ms) in enumerate(keep_intervals):
            keep_segment = in_stream.filter(
                "aselect", f"between(t,{keep_start_ms / 1000.0},{keep_end_ms / 1000.0})"
            ).filter("asetpts", expr="PTS-STARTPTS")
            processed_streams.append(keep_segment)

            # Add Fade Transition *after* this keep segment, *if* it's not the last one
            if fade_ms > 0 and i < len(keep_intervals) - 1:
                # The gap corresponds to an ad segment.
                ad_start_ms = keep_end_ms
                next_keep_start_ms = keep_intervals[i+1][0]
                ad_end_ms = next_keep_start_ms

                if ad_start_ms < ad_end_ms:
                    ad_duration_ms = ad_end_ms - ad_start_ms
                    # Fade can't exceed half the gap
                    actual_fade_duration_ms = min(fade_ms, ad_duration_ms / 2)

                    if actual_fade_duration_ms > 0:
                        # Fade Out
                        fade_start_segment = (
                            in_stream.filter(
                                "aselect",
                                f"between(t,{ad_start_ms / 1000.0},{(ad_start_ms + actual_fade_duration_ms) / 1000.0})"
                            )
                            .filter(
                                "afade",
                                type="out", start_time=0, duration=actual_fade_duration_ms / 1000.0,
                            )
                            .filter("asetpts", expr="PTS-STARTPTS")
                        )
                        processed_streams.append(fade_start_segment)

                        # Fade In
                        fade_in_start_time_ms = ad_end_ms - actual_fade_duration_ms
                        fade_end_segment = (
                             in_stream.filter(
                                "aselect",
                                f"between(t,{fade_in_start_time_ms / 1000.0},{ad_end_ms / 1000.0})"
                            )
                            .filter(
                                "afade",
                                type="in", start_time=0, duration=actual_fade_duration_ms / 1000.0,
                            )
                            .filter("asetpts", expr="PTS-STARTPTS")
                        )
                        processed_streams.append(fade_end_segment)


        # Concatenate and Output
        # Check again in case filtering failed silently (though unlikely with aselect)
        if not processed_streams:
             _create_silent_file(out_path)
             logger.warning(f"No streams generated after filtering for {in_path}. Outputting silent file.")
             return

        joined_stream = ffmpeg.concat(*processed_streams, v=0, a=1)
        try:
            # Use run() and capture stdout/stderr. It raises ffmpeg.Error on failure.
            (
                ffmpeg
                .output(joined_stream, out_path)
                .overwrite_output()
                .run(capture_stdout=True, capture_stderr=True)
            )

        except ffmpeg.Error as e:
            err_msg = e.stderr.decode('utf-8', errors='replace') if hasattr(e, 'stderr') and e.stderr else str(e)
            logger.error(f"FFmpeg error during final concatenation for {out_path}: {err_msg}")
            raise

        except Exception as general_e:
             logger.error(f"General error during ffmpeg run for {out_path}: Type={type(general_e).__name__}, Error={general_e}")
             raise

    except Exception as e:
        # Catch potential errors from duration check, ffmpeg input, value errors etc.
        logger.error(f"Error in clip_segments_with_fade function for {in_path}: Type={type(e).__name__}, Error={e}")
        raise

def trim_file(in_path: Path, out_path: Path, start_ms: int, end_ms: int) -> None:
    """
    Trims the input audio file to the specified time range using precise 'aselect'.
    Avoids forced re-encoding, relying on FFmpeg's defaults for the output format.
    """
    try:
        (
            ffmpeg.input(str(in_path))
            .filter("aselect", f"between(t,{start_ms/1000},{end_ms/1000})")
            .filter("asetpts", "PTS-STARTPTS")
            .output(str(out_path)) # Use default settings based on out_path extension
            .overwrite_output()
            .run(quiet=True)
        )
    except ffmpeg.Error as e:
        err_msg = e.stderr.decode('utf-8', errors='replace') if hasattr(e, 'stderr') and e.stderr else str(e)
        logger.error(f"Error trimming file '{in_path}' from {start_ms}ms to {end_ms}ms into '{out_path}': {err_msg}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error trimming file '{in_path}' to '{out_path}': {e}")
        raise


def split_audio(
    audio_file_path: Path,
    audio_chunk_path: Path,
    chunk_size_bytes: int,
) -> List[Tuple[Path, int]]:
    """
    Splits the audio file into chunks of approximately chunk_size_bytes.
    Uses precise trimming via trim_file (aselect).
    Returns a list of (chunk_path, start_offset_ms).
    """
    audio_chunk_path.mkdir(parents=True, exist_ok=True)

    duration_ms = get_audio_duration_ms(str(audio_file_path))
    if duration_ms is None:
        raise ValueError(f"Could not get duration for {audio_file_path}")
    if duration_ms <= 0:
         logger.warning(f"Audio file {audio_file_path} has zero or negative duration. No chunks generated.")
         return []

    try:
        file_size = audio_file_path.stat().st_size
        if file_size <= 0:
            logger.warning(f"Audio file {audio_file_path} has zero size despite duration {duration_ms}ms. No chunks generated.")
            return []
    except FileNotFoundError:
         logger.error(f"Input audio file not found at {audio_file_path}")
         raise

    # Estimate chunk duration based on average bitrate
    # Use float division for better precision before int conversion
    avg_bytes_per_ms = file_size / duration_ms
    if avg_bytes_per_ms <= 0:
         # Avoid division by zero or negative duration issues if checks above failed
         chunk_duration_ms = duration_ms
         logger.warning(f"Could not calculate positive average bitrate for {audio_file_path}. Treating as single chunk.")
    else:
         # Add a small epsilon to avoid potential zero duration if chunk_size is tiny
         chunk_duration_ms = int((chunk_size_bytes / avg_bytes_per_ms) + 0.001)


    if chunk_duration_ms <= 0:
        logger.warning(
            f"Calculated chunk duration is non-positive ({chunk_duration_ms}ms) for {audio_file_path} "
            f"with target size {chunk_size_bytes}. Using minimum of 1ms."
        )
        chunk_duration_ms = 1  # Ensure minimum duration

    # Use ceiling division for correct number of chunks
    num_chunks = (duration_ms + chunk_duration_ms - 1) // chunk_duration_ms

    chunks: List[Tuple[Path, int]] = []
    logger.info(f"Splitting '{audio_file_path.name}' ({duration_ms}ms) into approx {num_chunks} chunks of {chunk_duration_ms}ms...")

    for i in range(num_chunks):
        start_offset_ms = i * chunk_duration_ms
        end_offset_ms = min(duration_ms, (i + 1) * chunk_duration_ms)

        # Important: Skip if the calculated start time is already at or beyond the duration
        # This can happen if the last chunk calculation slightly overshoots
        if start_offset_ms >= duration_ms:
            continue

        # Ensure end > start, important if chunk_duration_ms became 1ms
        # or if duration_ms is very small
        if end_offset_ms <= start_offset_ms:
             logger.warning(f"Skipping chunk {i} due to end offset ({end_offset_ms}ms) <= start offset ({start_offset_ms}ms).")
             continue


        export_path = audio_chunk_path / f"{audio_file_path.stem}_chunk_{i}.mp3"
        try:
            trim_file(audio_file_path, export_path, start_offset_ms, end_offset_ms)
            chunks.append((export_path, start_offset_ms))
        # Catch specific ffmpeg errors from trim_file
        except ffmpeg.Error as e:
             err_msg = e.stderr.decode('utf-8', errors='replace') if hasattr(e, 'stderr') and e.stderr else str(e)
             logger.error(f"Failed to trim chunk {i} for {audio_file_path.name}. Skipping chunk. Error: {err_msg}")
        except Exception as e:
            logger.error(f"Unexpected error processing chunk {i} ({start_offset_ms}ms - {end_offset_ms}ms): {e}")

    logger.info(f"Generated {len(chunks)} chunks for {audio_file_path.name}.")
    return chunks