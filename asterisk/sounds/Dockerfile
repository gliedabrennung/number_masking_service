FROM python:3.12-slim-bookworm

ARG VOICE=ru_RU-dmitri-medium
ARG VOICE_PATH=ru/ru_RU/dmitri/medium
ARG PIPER_VOICES=https://huggingface.co/rhasspy/piper-voices/resolve/main

RUN apt-get update && apt-get install -y --no-install-recommends \
        sox libsox-fmt-all curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir piper-tts

RUN mkdir -p /opt/voice \
    && curl -fsSL -o "/opt/voice/${VOICE}.onnx" \
         "${PIPER_VOICES}/${VOICE_PATH}/${VOICE}.onnx" \
    && curl -fsSL -o "/opt/voice/${VOICE}.onnx.json" \
         "${PIPER_VOICES}/${VOICE_PATH}/${VOICE}.onnx.json"

ENV PIPER_MODEL=/opt/voice/${VOICE}.onnx

COPY generate.sh /usr/local/bin/generate-sounds.sh
RUN chmod +x /usr/local/bin/generate-sounds.sh

ENTRYPOINT ["/usr/local/bin/generate-sounds.sh"]
