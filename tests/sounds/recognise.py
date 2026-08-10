#!/usr/bin/env python3
"""Prints what an offline recogniser hears in each voice prompt.

The prompts are telephony grade (8 kHz), which is exactly where a bad
text-to-speech engine becomes unintelligible. Recognising them back is a cheap
objective check that a human on the phone will make out the words too.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import wave

import vosk

EXPECTED = {
    "session-expired": (
        "срок действия соединения закончился обратитесь в приложение"
    ),
    "number-unavailable": "номер недоступен проверьте набранный номер",
    "enter-code": "введите код из четырёх цифр и нажмите решётку",
    "wrong-code": "неверный код попробуйте ещё раз",
    "tech-error": "техническая ошибка повторите вызов позже",
}
_ASR_RATE = 16000


def recognise(path: pathlib.Path, model: vosk.Model) -> str:
    """Returns the text the recogniser hears in a prompt."""
    with tempfile.NamedTemporaryFile(suffix=".wav") as upsampled:
        subprocess.run(
            [
                "sox",
                str(path),
                "-r",
                str(_ASR_RATE),
                "-c",
                "1",
                "-b",
                "16",
                upsampled.name,
                "rate",
                "-v",
                str(_ASR_RATE),
            ],
            check=True,
            capture_output=True,
        )
        with wave.open(upsampled.name) as wav:
            recogniser = vosk.KaldiRecognizer(model, wav.getframerate())
            heard = []
            while chunk := wav.readframes(4000):
                if recogniser.AcceptWaveform(chunk):
                    heard.append(json.loads(recogniser.Result())["text"])
            heard.append(json.loads(recogniser.FinalResult())["text"])
    return " ".join(part for part in heard if part)


def main() -> int:
    """Recognises every prompt and reports how well it matched."""
    directory = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/sounds")
    vosk.SetLogLevel(-1)
    model = vosk.Model("/opt/model")

    failures = 0
    for name, expected in EXPECTED.items():
        path = directory / f"{name}.wav"
        if not path.exists():
            print(f"{name:20} MISSING")
            failures += 1
            continue
        heard = recognise(path, model)
        expected_words = expected.split()
        heard_words = set(heard.split())
        matched = sum(1 for word in expected_words if word in heard_words)
        score = matched / len(expected_words)
        verdict = "ok" if score >= 0.7 else "UNCLEAR"
        if verdict != "ok":
            failures += 1
        print(f"{name:20} {verdict:8} {score:.0%}  heard: {heard!r}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
