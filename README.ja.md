**Languages:** [English](README.md) | 日本語

# clab-mcp-server

Containerlab のライフサイクル管理、既存運用スクリプトの資産（コマンド
エイリアス・コンフィグ保存/復元・トポロジテストエンジン）、そして
Nornir + Netmiko によるマルチベンダー並列オペレーションを 1 つに融合した
ハイブリッド型 MCP サーバー。

固定インベントリファイルを持たず、ツール呼び出しのたびに `clab inspect`
で最新ノード状態を取得し、メモリ上で Nornir インベントリを組み立てて
並列実行する（stateless 設計）。実体は単一ファイル [server.py](server.py)。

## 前提条件

- Python 3.10 以上
- [Containerlab](https://containerlab.dev/) が動作するホスト（ローカル or リモート）
- [uv](https://docs.astral.sh/uv/)（ホストで直接実行する場合。推奨）
- Docker（コンテナで実行する場合）
- パケットキャプチャ機能を使う場合: ローカル Mac に Wireshark、
  リモートホストに `tshark` と ssh 到達性

## インストール / セットアップ

### 方法A: ホスト環境で直接実行（uv で隔離、推奨）

システムの Python 環境を汚さないよう、`uv` でプロジェクト専用の仮想環境
（`.venv/`）を作成してから実行する。

```bash
git clone <this-repo>
cd clab-mcp-server

# 依存関係を解決してプロジェクト専用の .venv/ に隔離インストール
uv sync

# 隔離環境内で MCP サーバーを起動（動作確認）
uv run python server.py
```

`uv sync` は `pyproject.toml` / `uv.lock` を読み、システムの
site-packages には一切触れずに `.venv/` へ依存関係をインストールする。
依存関係を更新した場合は `uv lock` でロックファイルを再生成すること。

### 方法B: Docker で実行

```bash
docker build -t clab-mcp .
docker run -i --rm \
  -v ~/labs:/workspace \
  -v ~/.ssh:/home/mcp/.ssh:ro \
  -e CLAB_HOST=clab-host.example.com \
  clab-mcp
```

- `/workspace` にトポロジ YAML・`save/`・`startup-configs/` を置く
  ホストディレクトリをマウントする。
- `CLAB_HOST` 経由のリモート実行や Netmiko の鍵認証を使う場合は
  `~/.ssh` を読み取り専用でマウントする。
- MCP は stdio 通信のため、必ず `-i`（標準入力をアタッチ）を付けて
  起動すること。

## MCP クライアントへの登録

stdio 起動なので、クライアント側の設定に `command`/`args` を登録する。

**uv 経由（ホスト実行）:**

```json
{
  "mcpServers": {
    "clab-hybrid": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/clab-mcp-server", "python", "server.py"],
      "env": {
        "CLAB_HOST": "clab-host.example.com"
      }
    }
  }
}
```

**Docker 経由:**

```json
{
  "mcpServers": {
    "clab-hybrid": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-v", "/path/to/labs:/workspace",
        "-v", "/Users/you/.ssh:/home/mcp/.ssh:ro",
        "-e", "CLAB_HOST=clab-host.example.com",
        "clab-mcp"
      ]
    }
  }
}
```

## 環境変数

| 変数 | 既定値 | 説明 |
|---|---|---|
| `CLAB_BIN` | `clab` | Containerlab バイナリ名 |
| `CLAB_HOST` | 未設定 | 設定するとリモートホスト上で ssh 経由で `clab` を実行 |
| `CLAB_SSH_USER` | 未設定 | リモート clab ホストへの ssh ユーザー |
| `CLAB_SUDO` | `0` | `1`/`true`/`yes` で `clab` コマンドに `sudo` を付与 |
| `CLAB_API_URL` | 未設定 | 設定すると deploy/inspect を clab-api-server (httpx) 経由に切替 |
| `NORNIR_WORKERS` | `20` | Nornir 並列実行のスレッド上限 |
| `NETMIKO_READ_TIMEOUT` | `60` | Netmiko コマンド実行の read_timeout（秒） |
| `NETMIKO_SSH_CONFIG` | 未設定 | Netmiko に渡す ssh_config ファイルパス（ProxyJump 等） |
| `CLAB_USER_<KIND>` | `KIND_DEFAULTS` 参照 | kind 別ユーザー名の上書き（例: `CLAB_USER_ARISTA_CEOS`） |
| `CLAB_PASS_<KIND>` | `KIND_DEFAULTS` 参照 | kind 別パスワードの上書き |

`<KIND>` は clab の kind 名を大文字化したもの（例: `arista_ceos` →
`ARISTA_CEOS`）。既定の認証情報は `server.py` の `KIND_DEFAULTS` を参照。

## 提供ツール

| ツール | 概要 |
|---|---|
| `deploy_lab(topo_yaml_path)` | トポロジ YAML からラボを新規デプロイ |
| `inspect_lab_topology(lab_name)` | 稼働中ノードの mgmt IP・kind・リンク情報を取得 |
| `run_parallel_command(lab_name, command_or_alias, node_filter_regex=None)` | 全ノード（or 正規表現で絞込）に対しコマンドを完全並列実行 |
| `snapshot_and_save_configs(lab_name, mode="snapshot", save_dir="save", default_startup_dir="startup-configs")` | 全台の設定を並列回収してスナップショット保存 or startup-config へ直接反映 |
| `restore_startup_configs(topo_path, snapshot_name="latest", save_dir="save")` | 保存済みスナップショットを各ノードの startup-config パスへ復元 |
| `run_topology_tests(test_file_or_dir)` | `test.yml` を再帰探索し PASS/FAIL レポートを生成 |
| `trigger_packet_capture(remote_host, container_name, interface_name)` | リモートの `tshark` キャプチャをローカル Wireshark にストリーミング |

### run_parallel_command のコマンドエイリアス

以下のエイリアスは各ノードの kind に応じたコマンドへ自動変換される
（未定義 kind やエイリアス外の文字列はリテラルとして実行）。

| エイリアス | 内容 |
|---|---|
| `bgp-summary` | BGP サマリ表示 |
| `ip-route` | ルーティングテーブル表示 |
| `interfaces` | インターフェース状態表示 |

```text
run_parallel_command(lab_name="mylab", command_or_alias="bgp-summary")
run_parallel_command(lab_name="mylab", command_or_alias="show version", node_filter_regex="^r")
```

### run_topology_tests の test.yml フォーマット

```yaml
lab: mylab
tests:
  - name: "BGP established on r1"
    nodes: "r1"              # ノード短名 or 正規表現
    command: "bgp-summary"   # エイリアス or リテラルコマンド
    assert:
      contains: "Established"   # または regex / exit_code
```

`test_file_or_dir` にディレクトリを指定すると、配下の `test.yml` /
`test.yaml` を再帰的に探索して全て実行する。

### snapshot / restore のディレクトリ構成

```text
save/
  save-20260703-021500/
    r1.conf
    r2.conf
startup-configs/
  r1.conf   # トポロジに startup-config 未定義のノードの既定保存先
```

## 開発

CIと同じ方法でローカルにテストを実行できる。

```bash
uv sync --all-groups   # dev 依存グループから pytest をインストール
uv run pytest -v
```

テストは `tests/` にあり、`server.py` の純粋ロジック部分（コマンド
エイリアス解決、kind→platform マッピング、インベントリ構築、トポロジ
YAML ヘルパー、テストエンジンのアサーション判定）を、稼働中の
Containerlab 環境無しでカバーしている。

## CI/CD

GitHub Actions ワークフロー: [.github/workflows/ci.yml](.github/workflows/ci.yml)

- **`test` ジョブ** — 全てのブランチへの push で実行される。
  `uv sync --all-groups` で依存関係をインストールし、
  `uv run pytest -v` を実行する。
- **`publish-container` ジョブ** — `main` への push（マージ含む）時、
  かつ `test` ジョブが成功した場合のみ実行される。`pyproject.toml` の
  `version` フィールドを読み取り、[Dockerfile](Dockerfile) からイメージを
  ビルドして、GitHub Container Registry へ
  `ghcr.io/<owner>/<repo>:<version>` と `ghcr.io/<owner>/<repo>:latest`
  の両タグでプッシュする。

新しいバージョンのイメージを公開するには、`main` へマージする前に
`pyproject.toml` の `version` を上げること。GHCR に push されるタグは
常にその値と一致する。

```bash
docker pull ghcr.io/<owner>/<repo>:<version>
```

## トラブルシューティング

- **`clab` コマンドが見つからない**: `CLAB_HOST` を設定してリモート
  ホストで実行するか、ローカルに Containerlab をインストールする。
- **ノードへの接続に失敗する**: `CLAB_HOST` 使用時、Netmiko は
  mgmt IP へ直接 SSH するため mgmt 網への到達性が必要。踏み台が
  必要な場合は `NETMIKO_SSH_CONFIG` に ProxyJump 入りの ssh_config
  を指定する。
- **`use_textfsm` の解析結果が生テキストになる**: 対応する
  ntc-templates が無いコマンド。`server.py` 内で自動的に生テキストへ
  フォールバックする仕様のため異常ではない。
