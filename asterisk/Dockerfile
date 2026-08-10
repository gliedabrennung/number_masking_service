FROM debian:bookworm-slim AS build

ARG ASTERISK_VERSION=22-current
ARG BUILD_JOBS=4

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl file libedit-dev libjansson-dev \
        libsqlite3-dev libssl-dev libxml2-dev libxslt1-dev pkg-config \
        uuid-dev libcurl4-openssl-dev libsrtp2-dev subversion xmlstarlet \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src
RUN curl -fsSL "https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-${ASTERISK_VERSION}.tar.gz" \
        -o asterisk.tar.gz \
    && mkdir asterisk && tar -xzf asterisk.tar.gz -C asterisk --strip-components=1 \
    && rm asterisk.tar.gz

WORKDIR /usr/src/asterisk
RUN ./configure --with-jansson-bundled --with-pjproject-bundled --disable-xmldoc \
    && make menuselect.makeopts \
    && menuselect/menuselect \
         --disable BUILD_NATIVE \
         --enable chan_pjsip \
         --enable res_ari \
         --enable res_ari_channels \
         --enable res_ari_bridges \
         --enable res_ari_playbacks \
         --enable res_ari_applications \
         --enable res_ari_asterisk \
         --enable res_ari_events \
         --enable res_stasis \
         --enable res_stasis_answer \
         --enable res_stasis_playback \
         --enable res_stasis_recording \
         --enable res_http_websocket \
         --enable res_prometheus \
         --enable CORE-SOUNDS-EN-ULAW \
         --disable BUILD_NATIVE \
         menuselect.makeopts \
    && make -j"${BUILD_JOBS}" \
    && make install \
    && make install-logrotate

RUN make install DESTDIR=/staging \
    && rm -rf /staging/var/run   # /var/run is a symlink to /run in the base image

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        libedit2 libjansson4 libsqlite3-0 libssl3 libxml2 libxslt1.1 \
        libuuid1 libcurl4 libsrtp2-1 ca-certificates gettext-base tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r asterisk && useradd -r -g asterisk -d /var/lib/asterisk asterisk

COPY --from=build /staging/ /

RUN ldconfig && mkdir -p /etc/asterisk/templates /etc/asterisk/static \
             /var/log/asterisk /var/spool/asterisk /var/run/asterisk \
             /var/lib/asterisk/sounds/ru/custom \
    && chown -R asterisk:asterisk /var/lib/asterisk /var/log/asterisk \
                                  /var/spool/asterisk /var/run/asterisk

COPY conf/ /etc/asterisk/static/
COPY templates/ /etc/asterisk/templates/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

COPY sounds/ru/custom/ /var/lib/asterisk/sounds/ru/custom/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && for prompt in session-expired number-unavailable enter-code \
                     wrong-code tech-error; do \
           test -s "/var/lib/asterisk/sounds/ru/custom/$prompt.wav" \
               || { echo "missing prompt: $prompt.wav; run 'make sounds'" >&2; \
                    exit 1; }; \
       done \
    && chown -R asterisk:asterisk /var/lib/asterisk/sounds

EXPOSE 5060/udp 10000-10200/udp 8088/tcp

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
