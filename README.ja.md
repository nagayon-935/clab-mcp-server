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
| `list_labs()` | 稼働ホストのラボ一覧。名前・状態・ノード数・ホスト側トポロジパスを取得 |
| `list_topologies(search_dir=".")` | MCPサーバー側のトポロジYAML一覧。未デプロイのファイルも探索 |
| `diagnose_environment(lab_name=None, check_node_connections=False)` | ホストSSH、clab、Docker、探索場所、ラボ検出を個別に診断。指定時のみノード接続・認証も確認 |
| `deploy_lab(topo_yaml_path, reconfigure=False)` | トポロジ YAML からラボを新規デプロイ(`reconfigure=True` で `--reconfigure` を付与し設定成果物を再生成) |
| `apply_lab(topo_yaml_path, dry_run=False)` | トポロジ YAML と稼働中ラボの差分だけを反映(containerlab 0.77+ の `apply`)。未デプロイなら新規デプロイ、稼働中ならノード/リンクの追加・削除など変更部分のみ反映し、無関係なノードは再作成しない。`dry_run=True` で適用せず変更内容のみ表示。containerlab 0.77 以上が必要 |
| `destroy_lab(topo_yaml_path, cleanup=False)` | トポロジ YAML からラボを破棄(`cleanup=True` で `--cleanup` を付与しラボディレクトリごと完全削除) |
| `redeploy_lab(topo_yaml_path, cleanup=False)` | ラボを破棄してから同じトポロジで再デプロイ(`clab redeploy`)。`cleanup=True` で `--cleanup` を付与 |
| `restart_lab_nodes(lab_name, node_names=None)` | 稼働中ラボのノードを1台・複数台・全台再起動(`clab restart`、コンテナ再作成無しのseamless dataplane)。`node_names` 省略で全ノード対象 |
| `inspect_lab_topology(lab_name)` | 稼働中ノードの mgmt IP・kind・リンク情報を取得 |
| `run_parallel_command(lab_name, command_or_alias, node_filter_regex=None, node_names=None, node_labels=None, max_output_chars=5000)` | ノード名の完全一致、ラベル、または正規表現で絞り込み並列実行 |
| `run_node_command(lab_name, node_name, command=None, max_output_chars=5000)` | 1ノードの接続方式・宛先・出力・失敗原因を取得。省略コマンドは `interfaces` |
| `read_command_output(output_id, offset=0, max_output_chars=5000)` | 省略された出力の続きを取得。元のコマンドを再実行しない |
| `snapshot_and_save_configs(lab_name, mode="snapshot", save_dir="save", default_startup_dir="startup-configs", node_names=None, node_labels=None)` | 全台または指定ノードの設定を回収してスナップショット保存 or startup-config へ書き込み |
| `restore_startup_configs(topo_path, snapshot_name="latest", save_dir="save")` | 保存済みスナップショットを各ノードの startup-config パスへ復元 |
| `run_topology_tests(test_file_or_dir)` | `test.yml` を再帰探索し PASS/FAIL レポートを生成 |
| `trigger_packet_capture(remote_host, container_name, interface_name)` | リモートの `tshark` キャプチャをローカル Wireshark にストリーミング |

### ラボを見つけて操作する

会話では「動いているラボを見せて」「mylab の r1 と r2 の状態を確認して」
と依頼できる。AIが呼び出すツールの例:

```text
list_labs()
list_topologies(search_dir="/path/to/labs")
inspect_lab_topology(lab_name="mylab")
run_parallel_command(lab_name="mylab", command_or_alias="interfaces", node_names=["r1", "r2"])
run_parallel_command(lab_name="mylab", command_or_alias="bgp-summary", node_labels={"role": "leaf"})
diagnose_environment()
diagnose_environment(lab_name="mylab", check_node_connections=True)
```

`node_names` は完全一致で、`r1` は `r10` に一致しない。空リストや存在しない
名前は実行前にエラーとなる。従来の `node_filter_regex` と同時指定は不可。
`node_labels` は指定した全キー・値の一致で選択し、名前指定とも組み合わせられる。
inspectにラベルが無い場合はホストのDockerから取得する。それも取得できない場合は、
一致するローカルYAMLのdefaults・kinds・groups・
nodesから継承して補完し、`warnings` に明示する。稼働構成とYAMLの一致を確認すること。

環境診断は設定変更を行わない。ノード接続確認は明示的に有効にした場合のみ行い、
Linuxは `docker exec ... true`、他kindは管理IPへのSSH接続・認証を確認する。
ホストへのSSHが失敗すると依存するチェックは未実施として表示する。
`list_labs` のトポロジパスは稼働ホスト側、`list_topologies` のパスはMCPサーバー側。
リモート構成では同じパスとは限らない。

### 共通の結果形式と0.2への移行

**0.2では全ツールの戻り値を構造化したオブジェクトへ変更した。** 従来の文章や
JSON文字列を解析していたクライアントは、`status` と `data` を参照するよう変更する。
以前の `inspect_lab_topology` の `nodes`・`links`、コマンドの `results` は `data` 配下になる。

```json
{
  "tool": "run_parallel_command",
  "status": "partial",
  "summary": "3台中1台で成功、1台で失敗、1台をスキップしました。",
  "counts": {"total": 3, "succeeded": 1, "failed": 1, "skipped": 1},
  "data": {"lab": "mylab", "results": {}},
  "warnings": [],
  "errors": [{"code": "AUTHENTICATION_FAILED", "message": "認証に失敗しました", "node": "r2", "next_step": "SSH鍵・ユーザー名・機器の認証設定を確認してください。"}],
  "next_steps": ["SSH鍵・ユーザー名・機器の認証設定を確認してください。"]
}
```

`status` は `success` / `partial` / `error` / `skipped`。
`counts` の単位はツールごとにラボ・ファイル・ノード・テスト結果・診断チェック。
`data.results` の各ノードには状態、接続方式、出力、失敗原因またはスキップ理由が入る。
接続失敗を含む場合も、取得できたノードの結果は返す。

コマンド出力は各ノードにつき既定5000文字。`max_output_chars` は1〜100000で指定する。
省略時には `output_info.truncated=true`、文字数、`output_id`、`next_offset` を返す。
`read_command_output` にIDとオフセットを渡すと続きを取得できる。構造化出力を
省略した場合はJSONのテキスト断片になる。出力の省略は操作の失敗を意味しない。
保持は同一サーバープロセス内で最大1時間・100件・1件100万文字まで。
再起動、期限切れ、件数上限による削除でIDは無効になる。上限を超える出力は
保持せず `unavailable_reason` を返す。ラボのインベントリは引き続き毎回取得する。

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
  管理IPを持たない Linux ノードも操作対象になる。
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
    nodes: ["r1", "r2"]      # 完全一致。文字列を指定すると従来の正規表現
    command: "bgp-summary"   # エイリアス or リテラルコマンド
    assert:
      contains: "Established"   # または regex / exit_code
```

`test_file_or_dir` にディレクトリを指定すると、配下の `test.yml` /
`test.yaml` を再帰的に探索して全て実行する。

**`exit_code` アサーションは `kind: linux` ノードのみ対応。** コマンドを
子シェルで実行し、`exit` や末尾コメントがあっても終了コードを回収する。
それ以外の kind（Cisco/Arista/Juniper 等）は、コマンド実行前に
非対応のアサーションとして FAIL を返す。
不正なテストファイルやラボ名の未指定も、サマリの失敗件数に含まれる。

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
同じラボ名のファイルが複数ある場合、リンク補完は警告、startup保存はエラーとなる。
`list_topologies` で候補を確認できる。

### snapshot / restore のディレクトリ構成

```text
save/
  save-20260703-021500-123456-abcdefgh/
    r1.conf
    r2.conf
startup-configs/
  r1.conf   # トポロジに startup-config 未定義のノードの既定保存先
```

`snapshot_and_save_configs` は `kind: linux`（FRR）ノードの設定も
`docker exec ... vtysh -c 'show running-config'` 経由で取得するように
なった。`vtysh` を持たないプレーンな linux コンテナ（L2スイッチ役等）は、
`KIND_COMMAND` 未定義の kind と同様に取得対象外としてスキップされる。
接続失敗やタイムアウトはエラーとして報告される。スナップショット名には
マイクロ秒と一意な接尾辞が含まれ、同時保存による上書きを防ぐ。
既存の `save-<timestamp>` ディレクトリも復元できる。明示名・`latest` の
どちらも `save_dir` 配下のディレクトリに限定し、保存先外を指す
シンボリックリンクは拒否する。

**ローカルファイルシステムに関する注意:** `deploy_lab`/`destroy_lab` 等と
異なり、`snapshot_and_save_configs`/`restore_startup_configs` のファイル
入出力(`save_dir`・`default_startup_dir`・トポロジファイルの読み込み)は
常にこの MCP サーバーを実行しているマシン自身に対して行われ、
`CLAB_HOST` 経由でリモートホストへは行かない。リモートの containerlab
ホストに対して使う場合は、`save_dir`/トポロジのディレクトリがローカルの
同じパスから参照できるようにしておくこと(リモートのラボディレクトリを
マウント/同期する等)。そうしないと実際のラボと噛み合わない。
`CLAB_HOST` 設定時は、両ツールとも `warnings` にこの注意点を含める。

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
