import tempfile
from pathlib import Path

from pytest import approx

from podcast_processor.audio import (
    clip_segments_with_fade,
    get_audio_duration_ms,
    split_audio,
)

TEST_FILE_DURATION = 66_048
TEST_FILE_PATH = "src/tests/data/count_0_99.mp3"


def test_get_duration_ms() -> None:
    assert get_audio_duration_ms(TEST_FILE_PATH) == TEST_FILE_DURATION


def test_clip_segment_with_fade() -> None:
    fade_len_ms = 5_000
    ad_start_offset_ms, ad_end_offset_ms = 3_000, 21_000
    ad_duration_ms = ad_end_offset_ms - ad_start_offset_ms
    actual_fade_duration_ms = min(fade_len_ms, ad_duration_ms / 2)

    with tempfile.NamedTemporaryFile(delete=True, suffix=".mp3") as temp_file:
        clip_segments_with_fade(
            [(ad_start_offset_ms, ad_end_offset_ms)],
            fade_len_ms,
            TEST_FILE_PATH,
            temp_file.name,
        )

        expected_duration_ms = (
            TEST_FILE_DURATION - ad_duration_ms + (2 * actual_fade_duration_ms)
        )
        actual_duration_ms = get_audio_duration_ms(temp_file.name)
        # Use approx to allow for small ffmpeg variations
        assert actual_duration_ms == approx(expected_duration_ms, abs=150)


def test_clip_segment_with_fade_beginning() -> None:
    fade_len_ms = 5_000
    ad_start_offset_ms, ad_end_offset_ms = 0, 18_000
    ad_duration_ms = ad_end_offset_ms - ad_start_offset_ms

    with tempfile.NamedTemporaryFile(delete=True, suffix=".mp3") as temp_file:
        clip_segments_with_fade(
            [(ad_start_offset_ms, ad_end_offset_ms)],
            fade_len_ms,
            TEST_FILE_PATH,
            temp_file.name,
        )

        # Corrected: No fades added for ads at the beginning
        expected_duration_ms = TEST_FILE_DURATION - ad_duration_ms
        actual_duration_ms = get_audio_duration_ms(temp_file.name)
        # Allow slightly larger tolerance due to potential segment boundary precision
        assert actual_duration_ms == approx(expected_duration_ms, abs=150)


def test_clip_segment_with_fade_end() -> None:
    fade_len_ms = 5_000
    ad_start_offset_ms, ad_end_offset_ms = (
        TEST_FILE_DURATION - 18_000,
        TEST_FILE_DURATION,
    )
    ad_duration_ms = ad_end_offset_ms - ad_start_offset_ms
    # actual_fade_duration_ms = min(fade_len_ms, ad_duration_ms / 2) # Not used when ad is at end

    with tempfile.NamedTemporaryFile(delete=True, suffix=".mp3") as temp_file:
        clip_segments_with_fade(
            [(ad_start_offset_ms, ad_end_offset_ms)],
            fade_len_ms,
            TEST_FILE_PATH,
            temp_file.name,
        )

        # Corrected: No fades added for ads at the end
        expected_duration_ms = TEST_FILE_DURATION - ad_duration_ms
        actual_duration_ms = get_audio_duration_ms(temp_file.name)
        # Allow slightly larger tolerance due to potential segment boundary precision
        assert actual_duration_ms == approx(expected_duration_ms, abs=150)


def test_split_audio() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir_path = Path(temp_dir)
        split_audio(Path(TEST_FILE_PATH), temp_dir_path, 38_000)

        expected = {
            "0.mp3": (6_384, 25_773),
            "1.mp3": (6_384, 25_773),
            "2.mp3": (6_384, 25_773),
            "3.mp3": (6_384, 25_773),
            "4.mp3": (6_384, 25_773),
            "5.mp3": (6_384, 25_773),
            "6.mp3": (6_384, 25_773),
            "7.mp3": (6_384, 25_773),
            "8.mp3": (6_384, 25_773),
            "9.mp3": (6_384, 25_773),
            "10.mp3": (2_784, 11_373),
        }

        for split in temp_dir_path.iterdir():
            assert split.name in expected
            duration_ms, filesize = expected[split.name]
            actual_duration = get_audio_duration_ms(str(split))
            assert (
                # Use approx for duration check
                actual_duration
                == approx(duration_ms, abs=50)
            ), f"unexpected duration for {split}. found {actual_duration}, expected {duration_ms}"
            assert (
                abs(filesize - split.stat().st_size) <= 150
            ), f"filesize differs by more than 150 bytes for {split}. found {split.stat().st_size}, expected {filesize}"  # pylint: disable=line-too-long
