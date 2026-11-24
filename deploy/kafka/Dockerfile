FROM apache/kafka:4.3.2@sha256:852a0d49fc4e656b022e7f23785411802c9a088c78a7c5e39f33969de9c227a7

USER 0:0
RUN set -eu; \
    work=$(mktemp -d); \
    cd "$work"; \
    repo=https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64; \
    for package in brotli-libs-1.2.1-r0 libexpat-2.10.0-r0 libfontenc-1.1.10-r0 musl-1.2.6-r3 musl-utils-1.2.6-r3; do \
      wget -q "$repo/$package.apk"; \
    done; \
    printf '%s\n' \
      '62a7e37e43bbfc1f8525a00ad2125fd5897135dc93b849733884cb78ffd8e26c  brotli-libs-1.2.1-r0.apk' \
      'e7e7408ea787ed06d00159b0ff1f9b65a63cf634f36d9e0fa7512ce2c489a503  libexpat-2.10.0-r0.apk' \
      'cab0395163ac9f1b9ff4df80d33e4aedc3302a72310ca49487e4ca8e489d022a  libfontenc-1.1.10-r0.apk' \
      'e142255d0fa397f14318996bc10378093dcc069dc3dd1b6818c8f28a789c21da  musl-1.2.6-r3.apk' \
      '47263d05842c5e12d01e0369e5b5bea51733323501c00ca21968a5102de137ca  musl-utils-1.2.6-r3.apk' \
      | sha256sum -c -; \
    apk --no-network --no-cache --no-logfile --repositories-file /dev/null add "$work"/*.apk; \
    rm ./*.apk; \
    cd /; \
    rmdir "$work"
USER appuser
