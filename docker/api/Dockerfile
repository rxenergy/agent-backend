# API container: CUDA 13 + Python 3.12.13 + e5 (sentence-transformers) + fermi (transformers)
# 베이스/Python 빌드 절차는 .devcontainer/Dockerfile 와 동일하게 맞춰 환경 차이를 줄였다.
FROM nvidia/cuda:13.0.0-devel-ubuntu24.04

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

RUN wget https://www.python.org/ftp/python/3.12.13/Python-3.12.13.tgz && \
    tar -xf Python-3.12.13.tgz && \
    cd Python-3.12.13 && \
    ./configure --enable-optimizations && \
    make -j$(nproc) && \
    make altinstall && \
    cd .. && rm -rf Python-3.12.13*

RUN ln -sf /usr/local/bin/python3.12 /usr/local/bin/python && \
    ln -sf /usr/local/bin/python3.12 /usr/local/bin/python3 && \
    curl -sS https://bootstrap.pypa.io/get-pip.py | python3.12

WORKDIR /workspace

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/workspace/.cache/huggingface \
    TRANSFORMERS_CACHE=/workspace/.cache/huggingface/transformers \
    SENTENCE_TRANSFORMERS_HOME=/workspace/.cache/huggingface/sentence-transformers

COPY requirements.txt /workspace/requirements.txt
RUN pip install --upgrade pip && pip install -r /workspace/requirements.txt

COPY app /workspace/app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
