#!/bin/sh
#
# Generates the five Russian voice prompts with an offline neural TTS (Piper,
# voice ru_RU-dmitri-medium) and converts them to what Asterisk plays without
# transcoding: WAV, PCM signed 16 bit, 8 kHz, mono.
#
# Run through `make sounds`; the resulting files are committed to the
# repository, and the Asterisk image only copies them.
#
# Why not espeak-ng: its Russian voice is barely intelligible once downsampled
# to 8 kHz. Every phrase below is checked by running it back through an
# offline recogniser — see tests/sounds/README.md.
#
set -eu

OUT_DIR="${1:-/out}"
MODEL="${PIPER_MODEL:?PIPER_MODEL must point at a Piper voice}"
# Slightly slower than the default: telephony bandwidth eats consonants.
LENGTH_SCALE="${LENGTH_SCALE:-1.1}"

mkdir -p "$OUT_DIR"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

say() {
    name="$1"
    text="$2"
    printf '%s\n' "$text" \
        | piper --model "$MODEL" --length-scale "$LENGTH_SCALE" \
                --output_file "$TMP_DIR/$name.wav" 2>/dev/null
    # gain -6 first: the filters below overshoot on a full-scale signal.
    # highpass/lowpass keep the signal inside the telephony band, so the
    # downsampling to 8 kHz cannot alias.
    sox "$TMP_DIR/$name.wav" -c 1 -b 16 -e signed-integer "$OUT_DIR/$name.wav" \
        gain -6 highpass 60 lowpass 3400 rate -v -s 8000 pad 0.2 0.3 gain -n -3
    echo "  $OUT_DIR/$name.wav"
}

echo "generating prompts into $OUT_DIR"
say session-expired \
    "Срок действия соединения закончился. Обратитесь в приложение."
say number-unavailable \
    "Номер недоступен. Проверьте набранный номер."
say enter-code \
    "Введите код из четырёх цифр и нажмите решётку."
say wrong-code \
    "Неверный код. Попробуйте ещё раз."
say tech-error \
    "Техническая ошибка. Повторите вызов позже."
echo "done"
