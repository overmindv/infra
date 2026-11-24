FROM postgres:17.12-alpine3.24@sha256:f9cf3e1f0066f0eb0c48707d864f0ac5941e9ff454a072077d97a4d278d07d73

RUN set -eu; \
    main=https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64; \
    wget -q -O /tmp/libxml2.apk "$main/libxml2-2.13.9-r3.apk"; \
    wget -q -O /tmp/su-exec.apk "$main/su-exec-0.3-r0.apk"; \
    printf '%s\n' \
      '8b37229c4761cf0eef5089045283526ef43bf335073050515010d34a313186ed  /tmp/libxml2.apk' \
      '4003163f58cd4a75df79973171d25e4912df4284d8bac9850ac267b58b969be5  /tmp/su-exec.apk' \
      | sha256sum -c -; \
    apk --no-network --no-cache --no-logfile --repositories-file /dev/null add /tmp/libxml2.apk /tmp/su-exec.apk; \
    entrypoint=/usr/local/bin/docker-entrypoint.sh; \
    test "$(grep -Fc 'exec gosu postgres ' "$entrypoint")" = 1; \
    sed -i 's|exec gosu postgres |exec /sbin/su-exec postgres |' "$entrypoint"; \
    rm /tmp/libxml2.apk /tmp/su-exec.apk /usr/local/bin/gosu
