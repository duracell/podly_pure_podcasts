from pathlib import Path
from typing import Any, List, Optional, Tuple

import ffmpeg  # type: ignore[import-untyped]


def get_audio_duration_ms(file_path: str) -> Optional[int]:
    try:
        probe = ffmpeg.probe(file_path)
        format_info = probe["format"]
        duration_seconds = float(format_info["duration"])
        duration_milliseconds = duration_seconds * 1000
        return int(duration_milliseconds)
    except ffmpeg.Error as e:
        print("An error occurred while trying to probe the file:")
        print(e.stderr.decode() if hasattr(e, "stderr") else str(e))
        return None
    except Exception as e:  # pylint: disable=broad-exception-caught
        print(f"Unexpected error probing file {file_path}: {e}")
        return None


def _calculate_keep_intervals(
    ad_segments_ms: List[Tuple[int, int]], audio_duration_ms: int
) -> List[Tuple[int, int]]:
    """Calculates the time intervals of audio segments to keep."""
    keep_intervals = []
    current_start_ms = 0
    # Ensure ads are sorted and clamped within duration
    sorted_ads = sorted(
        [
            (max(0, start), min(audio_duration_ms, end))
            for start, end in ad_segments_ms
            if start < end
        ]
    )
    for ad_start_ms, ad_end_ms in sorted_ads:
        if ad_start_ms > current_start_ms:
            keep_intervals.append((current_start_ms, ad_start_ms))
        current_start_ms = max(current_start_ms, ad_end_ms)

    if current_start_ms < audio_duration_ms:
        keep_intervals.append((current_start_ms, audio_duration_ms))
    return keep_intervals


def _create_silent_file(out_path: str) -> None:
    """Creates a minimal silent MP3 file."""
    print("Warning: No segments to keep/process. Outputting minimal silent file.")
    try:
        sample_rate = 44100
        (
            ffmpeg.input(
                f"anullsrc=channel_layout=mono:sample_rate={sample_rate}",
                f="lavfi",
                t=0.001,
            )
            .output(out_path, acodec="libmp3lame", ar=sample_rate, ac=1)
            .overwrite_output()
            .run()
        )
    except ffmpeg.Error as e:
        print(
            f"Error creating silent file: "
            f"{e.stderr.decode() if hasattr(e, 'stderr') else str(e)}"
        )
        raise


def _process_segment(
    in_stream: Any,
    keep_start_ms: int,
    keep_end_ms: int,
) -> Any:
    """Processes a single audio segment: trims and resets PTS."""

    # 1. Extract & Reset PTS
    current_stream = in_stream.filter(
        "atrim", start=keep_start_ms / 1000.0, end=keep_end_ms / 1000.0
    ).filter("asetpts", expr="PTS-STARTPTS")

    return current_stream


def clip_segments_with_fade(
    ad_segments_ms: List[Tuple[int, int]],
    fade_ms: int,
    in_path: str,
    out_path: str,
) -> None:
    """
    Clips out ads using keep intervals but applies the *original* fade logic,
    adding faded portions of the ad segment back.
    """
    try:
        in_stream = ffmpeg.input(in_path)
        audio_duration_ms = get_audio_duration_ms(in_path)
        if audio_duration_ms is None:
            raise ValueError(f"Could not determine duration of {in_path}")

        # Get validated ad segments to find boundaries
        sorted_ads = sorted(
            [
                (max(0, start), min(audio_duration_ms, end))
                for start, end in ad_segments_ms
                if start < end
            ]
        )
        # Create a lookup for ad end times based on ad start times
        ad_boundaries = dict(sorted_ads)

        keep_intervals = _calculate_keep_intervals(ad_segments_ms, audio_duration_ms)

        if not keep_intervals:
            _create_silent_file(out_path)
            return

        processed_streams = []
        for _, (keep_start_ms, keep_end_ms) in enumerate(keep_intervals):
            segment_duration_ms = keep_end_ms - keep_start_ms
            if segment_duration_ms > 0:
                # Get the trimmed/PTS-reset segment
                processed_stream = _process_segment(
                    in_stream,
                    keep_start_ms,
                    keep_end_ms,
                )
                processed_streams.append(processed_stream)

                # --- Start: Re-implement old fade logic for chunks BETWEEN segments ---
                ad_start_ms = keep_end_ms
                if ad_start_ms in ad_boundaries:
                    ad_end_ms = ad_boundaries[ad_start_ms]
                    ad_duration_ms = ad_end_ms - ad_start_ms

                    # 2. Faded segment from START of ad (Original flawed logic)
                    actual_fade_duration_start = min(fade_ms, ad_duration_ms)
                    if actual_fade_duration_start > 0:
                        fade_start_segment = (
                            in_stream.filter(
                                "atrim",
                                start=ad_start_ms / 1000.0,
                                end=(ad_start_ms + actual_fade_duration_start) / 1000.0,
                            )
                            .filter(
                                "afade",
                                type="out",
                                duration=actual_fade_duration_start / 1000.0,
                            )
                            .filter("asetpts", expr="PTS-STARTPTS")
                        )
                        processed_streams.append(fade_start_segment)

                    # 3. Faded segment from END of ad (Original flawed logic)
                    actual_fade_duration_end = min(fade_ms, ad_duration_ms)
                    fade_in_start_time_ms = max(
                        ad_start_ms, ad_end_ms - actual_fade_duration_end
                    )
                    if actual_fade_duration_end > 0:
                        fade_end_segment = (
                            in_stream.filter(
                                "atrim",
                                start=fade_in_start_time_ms / 1000.0,
                                end=ad_end_ms / 1000.0,
                            )
                            .filter(
                                "afade",
                                type="in",
                                duration=actual_fade_duration_end / 1000.0,
                            )
                            .filter("asetpts", expr="PTS-STARTPTS")
                        )
                        processed_streams.append(fade_end_segment)
                # --- End: Re-implement old fade logic for chunks BETWEEN segments ---

        if not processed_streams:
            _create_silent_file(out_path)
            return

        # Concatenate and Output
        joined_stream = ffmpeg.concat(*processed_streams, v=0, a=1)
        try:
            ffmpeg.output(joined_stream, out_path).overwrite_output().run()
        except ffmpeg.Error as e:
            err_msg = e.stderr.decode() if hasattr(e, "stderr") else str(e)
            print(f"Error during ffmpeg final output: {err_msg}")
            raise

    except Exception as e:
        print(f"Error in clip_segments_with_fade: {e}")
        raise


def trim_file(in_path: Path, out_path: Path, start_ms: int, end_ms: int) -> None:
    try:
        ffmpeg.input(str(in_path)).filter(
            "atrim", start=start_ms / 1000.0, end=end_ms / 1000.0
        ).output(str(out_path)).overwrite_output().run()
    except ffmpeg.Error as e:
        err_msg = e.stderr.decode() if hasattr(e, "stderr") else str(e)
        print(f"Error during trim_file execution: {err_msg}")
        raise
    except Exception as e:  # pylint: disable=broad-exception-caught
        print(f"Unexpected error during trim_file: {e}")
        raise


def split_audio(
    audio_file_path: Path,
    audio_chunk_path: Path,
    chunk_size_bytes: int,
) -> List[Tuple[Path, int]]:

    audio_chunk_path.mkdir(exist_ok=True)

    duration_ms = get_audio_duration_ms(str(audio_file_path))
    if duration_ms is None:
        raise ValueError(f"Could not get duration for {audio_file_path}")

    if audio_file_path.stat().st_size == 0:
        raise ValueError(f"Audio file {audio_file_path} has zero size.")

    chunk_duration_ms = (
        chunk_size_bytes / audio_file_path.stat().st_size
    ) * duration_ms
    chunk_duration_ms = int(chunk_duration_ms)

    if chunk_duration_ms <= 0:
        print(
            f"Warning: Calculated chunk duration is {chunk_duration_ms}ms. Setting to 1ms."
        )
        chunk_duration_ms = 1  # Set a minimum duration
        # Alternatively, could raise an error or handle as a single chunk

    num_chunks = (duration_ms // chunk_duration_ms) + 1

    chunks: List[Tuple[Path, int]] = []

    for i in range(num_chunks):
        start_offset_ms = i * chunk_duration_ms
        end_offset_ms = min(
            duration_ms, (i + 1) * chunk_duration_ms
        )  # Ensure end doesn't exceed total duration

        if start_offset_ms >= duration_ms:
            continue

        export_path = audio_chunk_path / f"{i}.mp3"
        try:
            trim_file(audio_file_path, export_path, start_offset_ms, end_offset_ms)
            chunks.append((export_path, start_offset_ms))
        except Exception as e:  # pylint: disable=broad-exception-caught
            # Decide how to handle errors during trimming a chunk
            print(
                f"Error trimming chunk {i} ({start_offset_ms}ms - {end_offset_ms}ms): {e}"
            )
            # Option: continue to next chunk, or raise the error
            # raise # Uncomment to stop processing on first trim error

    return chunks
