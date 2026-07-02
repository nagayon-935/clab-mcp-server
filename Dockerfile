# clab-hybrid MCP server
#
# stdio ベースの MCP サーバーのため、実行は必ず `docker run -i ...`
# （標準入出力をアタッチした状態）で行うこと。
#
# トポロジ YAML / スナップショット保存先 / startup-configs はホスト側の
# ファイルとやり取りするため、/workspace にホストディレクトリをマウントする。
# CLAB_HOST 経由のリモート実行や Netmiko の鍵認証を使う場合は、
# ホストの ~/.ssh を読み取り専用でマウントすること。

FROM python:3.12-slim

# ssh: CLAB_HOST 経由のリモート clab 実行 / trigger_packet_capture の ssh 呼び出しに必要
# gcc等のビルド依存: netmiko/cryptography のソースビルドが必要な環境向けの保険
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        openssh-client \
        gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

RUN useradd --create-home --uid 1000 mcp \
    && mkdir -p /workspace \
    && chown -R mcp:mcp /app /workspace

USER mcp
WORKDIR /workspace

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["python", "/app/server.py"]
