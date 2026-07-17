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
- パケットキャプチャ機能を使う場合: MCP サーバーを実行するマシン
  （macOS / Windows / Linux。`PATH` またはプラットフォームの既定
  インストール先から解決）に Wireshark、リモートホストに `tshark` と
  ssh 到達性

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
| `deploy_lab(topo_yaml_path, reconfigure=False)` | トポロジ YAML からラボを新規デプロイ(`reconfigure=True` で `--reconfigure` を付与し設定成果物を再生成) |
| `apply_lab(topo_yaml_path, dry_run=False)` | トポロジ YAML と稼働中ラボの差分だけを反映(containerlab 0.77+ の `apply`)。未デプロイなら新規デプロイ、稼働中ならノード/リンクの追加・削除など変更部分のみ反映し、無関係なノードは再作成しない。`dry_run=True` で適用せず変更内容のみ表示。containerlab 0.77 以上が必要 |
| `destroy_lab(topo_yaml_path, cleanup=False)` | トポロジ YAML からラボを破棄(`cleanup=True` で `--cleanup` を付与しラボディレクトリごと完全削除) |
| `redeploy_lab(topo_yaml_path, cleanup=False)` | ラボを破棄してから同じトポロジで再デプロイ(`clab redeploy`)。`cleanup=True` で `--cleanup` を付与 |
| `restart_lab_nodes(lab_name, node_names=None)` | 稼働中ラボのノードを1台・複数台・全台再起動(`clab restart`、コンテナ再作成無しのseamless dataplane)。`node_names` 省略で全ノード対象 |
| `inspect_lab_topology(lab_name)` | 稼働中ノードの mgmt IP・kind・リンク情報を取得 |
| `run_parallel_command(lab_name, command_or_alias, node_filter_regex=None)` | 全ノード（or 正規表現で絞込）に対しコマンドを完全並列実行 |
| `run_node_command(lab_name, node_name, command=None)` | 1ノードだけに絞ってコマンドを実行（`scripts/clab-cli` の非対話版）。使用した接続方式・宛先を明示するので、並列実行では埋もれがちな個別ノードの疎通/認証失敗を切り分けられる。`command` 省略時は `interfaces` エイリアスを実行 |
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

### kind ごとの接続方式

`run_parallel_command` / `run_node_command` は各ノードの `kind` で接続方式を振り分ける。

- **`kind: linux`**（FRR / 素の Linux コンテナ）: これらのイメージは通常
  `sshd` を持たないため、`docker exec <container> sh -c "<command>"` で
  コンテナへ直接コマンドを送り込む（`scripts/clab-exec-all` /
  `scripts/clab-cli` と同じ方式）。`CLAB_HOST` 設定時は Docker がリモート
  ホスト側にしか存在しないため、ssh 経由でそのリモートホスト上で
  `docker exec` を実行する（ローカルでは実行しない）。
- **それ以外の kind**（`cisco_xrd`, `arista_ceos`, `juniper_crpd` 等）:
  上記のとおり Netmiko/SSH でノードの mgmt IP へ接続する。

`linux` kind のノードでコマンドが失敗し続ける場合は、`run_node_command`
で実際に使われた接続方式・宛先（`docker exec (<container>)` か
`ssh (<mgmt_ip>)` か）と生のエラーを確認すると、並列実行のサマリだけでは
わからない原因を切り分けやすい。

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

**`exit_code` アサーションは `kind: linux` ノードのみ対応。** テスト
エンジンは `linux` kind のノードに対してのみコマンド末尾に
`; echo __RC__=$?` を付与しシェルの終了コードを回収する。それ以外の
kind（Cisco/Arista/Juniper 等）には同等の仕組みが無いため、それらに
対する `exit_code` アサーションは「`__RC__` マーカーが無い」旨の詳細と
共に必ず FAIL となる（黙って成功扱いにはしない）。

### トポロジ YAML の自動探索

`inspect_lab_topology(lab_name)`（リンク情報の補完用）と
`snapshot_and_save_configs(lab_name, mode="startup")` はトポロジパスを
直接受け取らず、カレントディレクトリ配下を再帰的に探索して `name:`
フィールドが `lab_name` と一致する `*.clab.yml` / `*.clab.yaml` を
探す。一致するファイルが無い場合は、無関係な別ラボの YAML へ推測で
フォールバックすることはせず、その旨を明示する（`links` を空にして
警告を付与、または `mode="startup"` の場合はエラー）。該当のトポロジ
YAML を含む（またはその上位の）ディレクトリから MCP サーバーを
起動すること。

### snapshot / restore のディレクトリ構成

```text
save/
  save-20260703-021500/
    r1.conf
    r2.conf
startup-configs/
  r1.conf   # トポロジに startup-config 未定義のノードの既定保存先
```

`snapshot_and_save_configs` は `kind: linux`（FRR）ノードの設定も
`docker exec ... vtysh -c 'show running-config'` 経由で取得するように
なった。`vtysh` を持たないプレーンな linux コンテナ（L2スイッチ役等）は、
`KIND_COMMAND` 未定義の kind と同様に取得対象外としてスキップされる。

**ローカルファイルシステムに関する注意:** `deploy_lab`/`destroy_lab` 等と
異なり、`snapshot_and_save_configs`/`restore_startup_configs` のファイル
入出力(`save_dir`・`default_startup_dir`・トポロジファイルの読み込み)は
常にこの MCP サーバーを実行しているマシン自身に対して行われ、
`CLAB_HOST` 経由でリモートホストへは行かない。リモートの containerlab
ホストに対して使う場合は、`save_dir`/トポロジのディレクトリがローカルの
同じパスから参照できるようにしておくこと(リモートのラボディレクトリを
マウント/同期する等)。そうしないと実際のラボと噛み合わない。
`CLAB_HOST` 設定時は、両ツールとも結果サマリの先頭に `⚠` 警告行を
付与し、この注意点を実行時にも思い出せるようにしている。

## 開発

CIと同じ方法でローカルにテストを実行できる。

```bash
uv sync --all-groups   # dev 依存グループから pytest / ruff / mypy をインストール
uv run ruff check .
uv run mypy server.py
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
  `ruff check .` でlint、`mypy server.py` で型チェックした上で
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
- **ノードへの接続に失敗する**: まず `kind: linux` のノードかどうかを
  確認する。これらは `docker exec` 経由でアクセスするため、`docker` が
  ローカル（`CLAB_HOST` 未設定時）または `CLAB_HOST` 上（設定時）で
  実行可能であることを確認する。それ以外の kind は `CLAB_HOST` 使用時、
  Netmiko が mgmt IP へ直接 SSH するため mgmt 網への到達性が必要。
  踏み台が必要な場合は `NETMIKO_SSH_CONFIG` に ProxyJump 入りの
  ssh_config を指定する。`run_node_command` で対象ノードを1つに絞ると
  実際の接続方式・宛先・エラーが確認できる。
- **`use_textfsm` の解析結果が生テキストになる**: 対応する
  ntc-templates が無いコマンド。`server.py` 内で自動的に生テキストへ
  フォールバックする仕様のため異常ではない。
- **`CLAB_HOST` への ssh がハングする / いきなり失敗する**: MCP
  サーバーは非対話で動作するため、全ての ssh 呼び出しは
  `BatchMode=yes` で実行される（ホストキーやパスワードのプロンプトを
  一切出さず即座に失敗する）。事前に `CLAB_HOST` のホストキーを
  `known_hosts` に登録しておく（一度手動で接続する、または
  `ssh-keyscan` を使う）こと、および鍵認証を設定しておくことが必須。
- **`CLAB_SUDO=1` が sudo エラーで失敗する**: 同じ理由で `sudo` は
  `sudo -n`（非対話）で実行される。リモートユーザーの `sudo` にパス
  ワードが必要な場合は、対象コマンドについて `CLAB_HOST` 側で
  パスワード無し sudo（`NOPASSWD`）を設定すること。
- **長時間実行コマンドが rc=124（timeout）で失敗する**: `CLAB_HOST`
  設定時、リモートコマンドは coreutils の `timeout` でラップされて
  おり、リモート側の `clab`/`docker exec` プロセスが固まった場合でも
  孤児化・ゾンビ化しないようになっている。正当に時間のかかる処理が
  タイムアウトする場合は、そのツールに渡されているタイムアウト値
  （例: `deploy_lab`/`destroy_lab` は既定 600秒）が上限であり、現状
  呼び出し単位での上書きはできない。
