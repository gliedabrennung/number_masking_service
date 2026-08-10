# Offline check that the voice prompts are actually intelligible.
#
# Runs a small Russian speech recogniser (Vosk) over the committed prompts and
# prints what it heard, so a regenerated prompt can be compared with its text
# without anyone having to listen.
FROM python:3.12-slim-bookworm

ARG MODEL_URL=https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip

RUN apt-get update && apt-get install -y --no-install-recommends \
        sox libsox-fmt-all curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir vosk

RUN curl -fsSL -o /tmp/model.zip "$MODEL_URL" \
    && unzip -q /tmp/model.zip -d /opt \
    && rm /tmp/model.zip \
    && mv /opt/vosk-model-small-ru-0.22 /opt/model

COPY recognise.py /usr/local/bin/recognise.py

ENTRYPOINT ["python", "/usr/local/bin/recognise.py"]
