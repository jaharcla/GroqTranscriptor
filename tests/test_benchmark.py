from courseai_lectures.benchmark import (
    reference_error_rates,
    sample_windows,
    transcript_metrics,
)


def test_sample_windows_cover_recording_without_intro_bias():
    windows = sample_windows(3000, 75)
    assert len(windows) == 4
    assert windows[0][0] > 0
    assert windows[-1][0] + windows[-1][1] <= 3000
    assert all(length == 75 for _start, length in windows)


def test_artifact_metrics_penalize_repetition():
    clean = (
        "Planck constant relates photon energy and frequency. "
        "The photoelectric effect depends on the work function of the metal."
    )
    noisy = ("Uptime Ute Uptime Ute Department of Education " * 30).strip()

    clean_metrics = transcript_metrics(clean, [], 60, 1)
    noisy_metrics = transcript_metrics(noisy, [], 60, 1)

    assert noisy_metrics["heuristic_artifact_score"] > clean_metrics[
        "heuristic_artifact_score"
    ]
    assert noisy_metrics["repeated_trigram_rate"] > clean_metrics[
        "repeated_trigram_rate"
    ]


def test_reference_error_rates_are_zero_for_exact_match():
    result = reference_error_rates("Energy equals h nu.", "Energy equals h nu.")
    assert result == {"wer": 0.0, "cer": 0.0}


def test_reference_error_rates_detect_word_error():
    result = reference_error_rates(
        "The photon energy equals h times frequency.",
        "The proton energy equals h times frequency.",
    )
    assert result["wer"] > 0
    assert result["cer"] > 0
