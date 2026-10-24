FROM ghcr.io/linuxserver/baseimage-ubuntu:noble

# set version label
ARG BUILD_DATE
ARG VERSION
ARG SYFT_VERSION=1.55.0
LABEL build_version="Linuxserver.io version:- ${VERSION} Build-date:- ${BUILD_DATE}"
LABEL maintainer="linuxserver.io"

ENV PYTHONUNBUFFERED=1

COPY pyproject.toml README.md LICENSE /app/
COPY ci /app/ci

RUN \
  echo "**** add 3rd party repos ****" && \
  mkdir -p /etc/apt/keyrings && \
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
    gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
  curl -fsSL "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0xF23C5A6CF475977595C89F51BA6932366A755776" | \
    gpg --dearmor -o /etc/apt/keyrings/deadsnakes.gpg && \
  curl -fsSL https://dl-ssl.google.com/linux/linux_signing_key.pub | \
    gpg --dearmor -o /etc/apt/keyrings/google.gpg && \
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu noble stable" > \
    /etc/apt/sources.list.d/docker.list && \
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/deadsnakes.gpg] https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu noble main" > \
    /etc/apt/sources.list.d/deadsnakes.list && \
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > \
    /etc/apt/sources.list.d/google-chrome.list && \
  echo "**** install runtime packages ****" && \
  apt-get update && \
  apt-get install -y --no-install-recommends \
    docker-buildx-plugin \
    docker-ce-cli \
    google-chrome-stable \
    python3.13 \
    python3.13-venv \
    unzip && \
  echo "**** install chromedriver ****" && \
  CHROME_VERSION=$(google-chrome --version | awk '{print $3}') && \
  CHROME_MAJOR=${CHROME_VERSION%%.*} && \
  CHROME_RELEASE=$(curl -fsSL "https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_${CHROME_MAJOR}" || true) && \
  if [ -z "${CHROME_RELEASE}" ]; then \
    CHROME_RELEASE=$(curl -fsSL https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_STABLE); \
  fi && \
  echo "Installed Chrome ${CHROME_VERSION}; retrieving ChromeDriver ${CHROME_RELEASE}" && \
  curl -fsSL -o /tmp/chromedriver.zip \
    "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_RELEASE}/linux64/chromedriver-linux64.zip" && \
  unzip -q /tmp/chromedriver.zip -d /tmp && \
  mv /tmp/chromedriver-linux64/chromedriver /usr/bin/chromedriver && \
  chown root:root /usr/bin/chromedriver && \
  chmod +x /usr/bin/chromedriver && \
  echo "**** install syft ****" && \
  curl -fsSL "https://github.com/anchore/syft/releases/download/v${SYFT_VERSION}/syft_${SYFT_VERSION}_linux_amd64.tar.gz" | \
    tar xz -C /usr/local/bin syft && \
  chmod +x /usr/local/bin/syft && \
  echo "**** install python deps ****" && \
  python3.13 -m venv /lsiopy && \
  pip install -U --no-cache-dir \
    pip && \
  pip install --no-cache-dir \
    /app && \
  printf "Linuxserver.io version: ${VERSION}\nBuild-date: ${BUILD_DATE}" > /build_version && \
  echo "**** cleanup ****" && \
  apt-get autoclean && \
  rm -rf \
    /var/lib/apt/lists/* \
    /var/tmp/* \
    /tmp/*

VOLUME /out

ENTRYPOINT [ "python3", "-m", "ci" ]
