# 1. 로컬 OS에 맞춘 Ubuntu 24.04 베이스 이미지
FROM nvidia/cuda:13.0.0-devel-ubuntu24.04

# 기본 패키지 및 Python 3.12 소스 빌드를 위한 의존성 설치
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    software-properties-common \
    wget \
    curl \
    git \
    build-essential \
    libssl-dev \
    zlib1g-dev \
    libncurses5-dev \
    libgdbm-dev \
    libnss3-dev \
    libsqlite3-dev \
    libreadline-dev \
    libffi-dev \
    libbz2-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# 정확한 Python 3.12.13 버전 다운로드 및 빌드 설치
RUN wget https://www.python.org/ftp/python/3.12.13/Python-3.12.13.tgz && \
    tar -xf Python-3.12.13.tgz && \
    cd Python-3.12.13 && \
    ./configure --enable-optimizations && \
    make -j$(nproc) && \
    make altinstall && \
    cd .. && rm -rf Python-3.12.13*

# 기본 python 및 pip 명령어를 python3.12로 링크 연결
RUN ln -sf /usr/local/bin/python3.12 /usr/local/bin/python && \
    ln -sf /usr/local/bin/python3.12 /usr/local/bin/python3 && \
    curl -sS https://bootstrap.pypa.io/get-pip.py | python3.12

WORKDIR /workspace