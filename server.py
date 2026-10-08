#!/usr/bin/env python3
"""Containerlab x Nornir/Netmiko ハイブリッド型 MCP サーバー.

Containerlab のライフサイクル管理（deploy / inspect）、既存運用スクリプトの資産
（コマンドエイリアス・コンフィグ保存/復元・トポロジテストエンジン）、そして
Nornir + Netmiko によるマルチベンダー並列オペレーションを 1 つの MCP サーバーに融合する。

設計思想
--------
* stateless: 固定インベントリファイルを持たない。ツール呼び出しのたびに
  ``clab inspect`` で最新ノード状態を取得し、メモリ上で Nornir インベントリを
  file-free に組み立て、その場で ThreadedRunner を初期化して並列実行する。
* 両対応: ``CLAB_HOST`` 未設定ならローカルで ``clab`` を subprocess 実行、
  設定されていれば ``ssh`` 経由でリモート Containerlab ホスト上で実行する。
* subprocess 主軸: deploy / inspect は ``clab`` CLI が主。``CLAB_API_URL`` が
  設定されている場合のみ clab-api-server への httpx 呼び出しにフォールバックする。

環境変数
--------
CLAB_BIN              : Containerlab バイナリ名（既定: ``clab``）
CLAB_HOST            : 設定するとリモートホスト上で ssh 経由 clab 実行
CLAB_SSH_USER        : リモート clab ホストへの ssh ユーザー
CLAB_SUDO            : "1" で clab コマンドに sudo を付与
CLAB_API_URL         : 設定すると deploy/inspect を clab-api-server(httpx)で実行
NORNIR_WORKERS       : 並列スレッド上限（既定: 20）
NETMIKO_READ_TIMEOUT : Netmiko の read_timeout 秒（既定: 60）
NETMIKO_SSH_CONFIG   : Netmiko に渡す ssh_config ファイル（ProxyJump 等）
CLAB_USER_<KIND>     : kind 別のユーザー名上書き（例: CLAB_USER_ARISTA_CEOS）
CLAB_PASS_<KIND>     : kind 別のパスワード上書き

依存
----
mcp[cli], nornir, nornir-netmiko, netmiko, ntc-templates, pyyaml, httpx
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from typing import Any, Callable, Optional
from urllib.parse import quote, urlsplit

from tool_results import (
    OUTPUT_STORE,
    ToolInputError,
    ToolResponse,
    error_detail,
    failure,
    present_output,
    response,
    validate_output_limit,
)

import yaml
from netmiko.exceptions import NetmikoParsingException
from textfsm.parser import TextFSMError

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

# Nornir コアオブジェクト（file-free インベントリ構築のため直接利用する）
from nornir.core import Nornir
from nornir.core.configuration import Config
from nornir.core.inventory import (
    ConnectionOptions,
    Defaults,
    Groups,
    Host,
    Hosts,
    Inventory,
)
from nornir.core.exceptions import NornirSubTaskError
from nornir.core.plugins.connections import ConnectionPluginRegister
from nornir.core.task import Result, Task
from nornir.plugins.runners import ThreadedRunner
from nornir_netmiko.tasks import netmiko_send_command

# InitNornir を経由せず Nornir を直接構築しているため、netmiko 等の接続
# プラグインが自動登録されない。ここで明示的に auto_register しておく。
ConnectionPluginRegister.auto_register()


# =============================================================================
# === Constants (既存運用スクリプトから移植したデータアセット) ===============
# =============================================================================

COMMAND_ALIASES: dict[str, dict[str, str]] = {
    "bgp-summary": {
        "linux": "vtysh -c 'show ip bgp summary'",
        "juniper_crpd": "cli -c 'show bgp summary'",
        "juniper_vjunosrouter": "show bgp summary",
        "juniper_vjunosswitch": "show bgp summary",
        "cisco_xrd": "show bgp summary",
        "cisco_xrv9k": "show bgp summary",
        "arista_ceos": "show ip bgp summary",
    },
    "ip-route": {
        "linux": "ip route",
        "juniper_crpd": "cli -c 'show route'",
        "juniper_vjunosrouter": "show route",
        "juniper_vjunosswitch": "show route",
        "cisco_xrd": "show route",
        "cisco_xrv9k": "show route",
        "arista_ceos": "show ip route",
    },
    "interfaces": {
        "linux": "ip addr",
        "juniper_crpd": "cli -c 'show interfaces terse'",
        "juniper_vjunosrouter": "show interfaces terse",
        "juniper_vjunosswitch": "show interfaces terse",
        "cisco_xrd": "show interfaces description",
        "cisco_xrv9k": "show interfaces description",
        "arista_ceos": "show interfaces status",
    },
}

# kind ごとの「設定を全文取得する」コマンド列（\n 区切り。最終行の出力を設定とみなす）
KIND_COMMAND: dict[str, str] = {
    "cisco_xrd": "terminal length 0\nshow running-config",
    "cisco_xrv9k": "terminal length 0\nshow running-config",
    "cisco_csr1000v": "terminal length 0\nshow running-config",
    "cisco_n9kv": "terminal length 0\nshow running-config",
    "cisco_iol": "terminal length 0\nshow running-config",
    "arista_ceos": "enable\nshow running-config | no-more",
    "arista_veos": "enable\nshow running-config | no-more",
    "juniper_crpd": "show configuration | no-more",
    "juniper_vmx": "show configuration | no-more",
    "juniper_vsrx": "show configuration | no-more",
    "juniper_vjunosrouter": "show configuration | no-more",
    "juniper_vjunosswitch": "show configuration | no-more",
    "juniper_cjunosevolved": "show configuration | no-more",
    "nokia_srlinux": "info from running",
}

# kind=="linux"（FRR 系イメージ）の設定取得コマンド。docker exec 経由で実行するため
# KIND_COMMAND とは別に持つ（Netmiko の enable/prep ステップは不要）。
LINUX_CONFIG_COMMAND = "vtysh -c 'show running-config'"

# kind ごとの既定認証情報 (username, password)
KIND_DEFAULTS: dict[str, tuple[str, str]] = {
    "cisco_xrd": ("clab", "clab@123"),
    "cisco_xrv9k": ("clab", "clab@123"),
    "cisco_csr1000v": ("admin", "admin"),
    "cisco_n9kv": ("admin", "admin"),
    "cisco_iol": ("admin", "admin"),
    "arista_ceos": ("admin", "admin"),
    "arista_veos": ("admin", "admin"),
    "juniper_crpd": ("root", "clab123"),
    "juniper_vmx": ("admin", "admin@123"),
    "juniper_vsrx": ("admin", "admin@123"),
    "juniper_vjunosrouter": ("admin", "admin@123"),
    "juniper_vjunosswitch": ("admin", "admin@123"),
    "juniper_cjunosevolved": ("admin", "admin@123"),
    "nokia_srlinux": ("admin", "NokiaSrl1!"),
}

# clab kind -> Netmiko/Nornir platform (device_type)
CLAB_TO_PLATFORM: dict[str, str] = {
    "juniper_vjunosrouter": "juniper_junos",
    "juniper_vjunosswitch": "juniper_junos",
    "juniper_crpd": "juniper_junos",
    "arista_ceos": "arista_eos",
    "cisco_xrd": "cisco_xr",
    "cisco_xrv9k": "cisco_xr",
    "cisco_n9kv": "cisco_nxos",
}


# =============================================================================
# === Config / Env ============================================================
# =============================================================================

# MCP は stdout を JSON-RPC 専用に使うため、ログは必ず stderr へ出す。
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
logger = logging.getLogger("clab_hybrid")


def _env_int(name: str, default: int) -> int:
    """環境変数を int として読む。不正値は警告を出して既定値へフォールバックする。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "環境変数 %s の値 %r を整数として解釈できません。既定値 %d を使用します",
            name,
            raw,
            default,
        )
        return default


CLAB_BIN = os.environ.get("CLAB_BIN", "clab")
CLAB_HOST = os.environ.get("CLAB_HOST")
CLAB_SSH_USER = os.environ.get("CLAB_SSH_USER")
CLAB_SUDO = os.environ.get("CLAB_SUDO", "0") in ("1", "true", "yes")
CLAB_API_URL = os.environ.get("CLAB_API_URL")
NORNIR_WORKERS = _env_int("NORNIR_WORKERS", 20)
NETMIKO_READ_TIMEOUT = _env_int("NETMIKO_READ_TIMEOUT", 60)
NETMIKO_SSH_CONFIG = os.environ.get("NETMIKO_SSH_CONFIG")

DEFAULT_LINUX_DEVICE_TYPE = "linux"

# MCP サーバーは非対話で動くため、ssh がホストキー確認やパスワード入力の
# プロンプトを出すと入力待ちで永久にハングする。リモート実行はすべて
# 非対話モード（BatchMode）で行い、接続確立にも上限を設ける。
SSH_BATCH_OPTS: list[str] = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]

# CLAB_HOST 実行時はリモート側の coreutils timeout を先に発火させ、rc=124 と
# stderr を回収できるよう、ローカル subprocess の timeout にはこのマージン秒を足す。
REMOTE_TIMEOUT_MARGIN = 10

mcp = FastMCP("clab-hybrid")


# =============================================================================
# === Clab Introspection (subprocess / optional httpx) =========================
# =============================================================================

def _remote_argv(local: list[str], timeout: Optional[int] = None) -> list[str]:
    """ローカル argv を、``CLAB_HOST`` 設定時は ssh 経由のリモート argv にラップする。

    ``clab`` サブプロセスに限らず、docker exec など「containerlab ホスト上で
    実行すべきコマンド」全般で共有する変換ロジック。

    ``timeout`` を指定すると、リモートコマンドを coreutils の ``timeout`` で
    ラップする。ローカル側の ``subprocess.run(timeout=...)`` はローカルの ssh
    プロセスしか強制終了できず、リモート側で起動された実際の clab/docker exec
    プロセスには終了シグナルが伝わらずゾンビ化しうるため、リモート側にも
    自前でタイムアウトを課す。ssh 自体は ``BatchMode=yes`` で非対話にし、
    ホストキー確認やパスワード入力プロンプトによる無限ハングを防ぐ。
    """
    if not CLAB_HOST:
        return local
    target = f"{CLAB_SSH_USER}@{CLAB_HOST}" if CLAB_SSH_USER else CLAB_HOST
    remote = [*local]
    if timeout is not None:
        remote = ["timeout", str(timeout), *remote]
    remote_cmd = " ".join(shlex.quote(part) for part in remote)
    return ["ssh", *SSH_BATCH_OPTS, target, remote_cmd]


def _clab_argv(args: list[str], timeout: Optional[int] = None) -> list[str]:
    """clab 呼び出しの argv を組み立てる（ローカル or ssh リモート）。"""
    return _host_argv([CLAB_BIN, *args], timeout=timeout)


def _host_argv(local: list[str], timeout: Optional[int] = None) -> list[str]:
    """ホストコマンドに非対話 sudo とリモート実行設定を適用する。"""
    if CLAB_SUDO:
        local = ["sudo", "-n", *local]
    return _remote_argv(local, timeout=timeout)


def _run_argv(argv: list[str], timeout: int, label: str) -> subprocess.CompletedProcess:
    """任意の argv を実行し CompletedProcess を返す（バイナリ不在/タイムアウトは RuntimeError）。

    ``stdin=subprocess.DEVNULL`` を指定し、MCP サーバーの stdin（LLM との
    JSON-RPC 通信ストリーム）を子プロセス（ssh 等）が誤って継承・消費しない
    ようにする。継承したままだと ssh のパスワードプロンプト待ちで通信が
    壊れたりハングしたりする。
    """
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
    except FileNotFoundError as exc:  # 対象バイナリ（clab / docker / ssh）が無い
        raise RuntimeError(f"実行バイナリが見つかりません: {argv[0]!r} ({exc})") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"{label} がタイムアウトしました"
            "（CLAB_HOST 使用時はリモート側の timeout ラップにより通常はリモート"
            "プロセスも終了しますが、ネットワーク断等で残存する可能性があります）"
        ) from exc


def _run_clab(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    """clab コマンドを実行し CompletedProcess を返す。失敗時は RuntimeError。"""
    proc = _run_host_command(
        _clab_argv(args, timeout=timeout), timeout, f"clab コマンド: {' '.join(args)}"
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"clab コマンド失敗 (rc={proc.returncode}): {' '.join(args)}\n"
            f"stderr:\n{proc.stderr.strip()}\nstdout:\n{proc.stdout.strip()}"
        )
    return proc


def _run_host_command(
    argv: list[str], timeout: int, label: str
) -> subprocess.CompletedProcess:
    """構築済みホストコマンドに、リモート側 timeout 回収用の猶予を適用する。"""
    local_timeout = timeout + REMOTE_TIMEOUT_MARGIN if CLAB_HOST else timeout
    return _run_argv(argv, local_timeout, label)


def _normalize_inspect_json(
    raw: Any, include_lab_names: bool = False
) -> list[dict[str, Any]]:
    """clab のバージョン差を吸収して container リストへ正規化する。"""
    if isinstance(raw, list):
        if not all(isinstance(item, dict) for item in raw):
            raise RuntimeError("clab inspect のコンテナ情報が不正です")
        return raw
    if isinstance(raw, dict):
        if "containers" in raw and isinstance(raw["containers"], list):
            return _normalize_inspect_json(raw["containers"])
        # 新しめの clab は {labname: [ ... ]} 形式で返す場合がある
        flattened: list[dict[str, Any]] = []
        found_list = False
        for lab, value in raw.items():
            if isinstance(value, list):
                found_list = True
                entries = _normalize_inspect_json(value)
                flattened.extend(
                    {"lab_name": lab, **v} if include_lab_names else v for v in entries
                )
        if flattened or found_list or not raw:
            return flattened
    raise RuntimeError("clab inspect の JSON 構造を解釈できませんでした")


def _strip_cidr(addr: Optional[str]) -> Optional[str]:
    if not addr or addr in ("N/A", "-"):
        return None
    return addr.split("/", 1)[0].strip()


def _short_node_name(full_name: str, lab_name: Optional[str]) -> str:
    """コンテナ名 (clab-<lab>-<node>) からノード短名を推定する。"""
    if lab_name:
        prefix = f"clab-{lab_name}-"
        if full_name.startswith(prefix):
            return full_name[len(prefix):]
    if full_name.startswith("clab-"):
        # ラボ名に '-' を含む可能性があるため最後の要素を採用
        return full_name.split("-")[-1]
    return full_name


def _inspect_nodes(lab_name: str) -> list[dict[str, Any]]:
    """稼働中ラボのノード情報を取得し正規化した dict のリストを返す。

    返却各要素: {name(短名), container(フル名), kind, mgmt_ip, image, state}
    """
    containers = _inspect_containers(lab_name)

    nodes: list[dict[str, Any]] = []
    for c in containers:
        full = c.get("name") or c.get("container") or ""
        if not full:
            continue
        # 別ラボのコンテナが混ざる場合を除外
        c_lab = c.get("lab_name") or c.get("labName")
        if c_lab and lab_name and c_lab != lab_name:
            continue
        nodes.append(
            {
                "name": _short_node_name(full, lab_name),
                "container": full,
                "kind": c.get("kind") or c.get("Kind") or "linux",
                "mgmt_ip": _strip_cidr(
                    c.get("ipv4_address")
                    or c.get("ipv4-address")
                    or c.get("IPv4Address")
                ),
                "image": c.get("image") or c.get("Image"),
                "state": c.get("state") or c.get("State"),
                "labels": c.get("labels") or c.get("Labels"),
            }
        )
    if not nodes:
        raise RuntimeError(f"ラボ '{lab_name}' の稼働ノードが見つかりませんでした")
    return nodes


def _inspect_containers(lab_name: Optional[str] = None) -> list[dict[str, Any]]:
    if CLAB_API_URL:
        return _inspect_via_api(lab_name)
    target = ["--name", lab_name] if lab_name else ["--all"]
    proc = _run_clab(["inspect", *target, "--format", "json"], timeout=120)
    try:
        return _normalize_inspect_json(json.loads(proc.stdout), include_lab_names=True)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"clab inspect の JSON パースに失敗: {exc}") from exc


def _inspect_via_api(lab_name: Optional[str] = None) -> list[dict[str, Any]]:
    """clab-api-server 経由でノード情報を取得する（CLAB_API_URL 設定時のみ）。"""
    import httpx

    assert CLAB_API_URL is not None  # 呼び出し元が CLAB_API_URL 設定時のみ呼ぶ

    url = f"{CLAB_API_URL.rstrip('/')}/api/v1/labs"
    if lab_name:
        url += f"/{quote(lab_name, safe='')}"
    try:
        resp = httpx.get(url, timeout=60.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"clab-api-server への問い合わせに失敗: {exc}") from exc
    return _normalize_inspect_json(resp.json(), include_lab_names=True)


# =============================================================================
# === Topology YAML helpers ===================================================
# =============================================================================

def _load_topo_yaml(topo_path: str) -> dict[str, Any]:
    data = _load_yaml(topo_path)
    if not isinstance(data, dict):
        raise RuntimeError(f"トポロジ YAML の内容が不正です: {topo_path}")
    _topo_nodes_and_links(data)
    return data


def _load_yaml(path: str) -> Any:
    """YAML の読み込み失敗を、呼び出し側が扱える RuntimeError に統一する。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise RuntimeError(f"YAML の読み込みに失敗: {path} ({exc})") from exc


def _find_topo_for_lab(lab_name: str) -> Optional[str]:
    """カレントディレクトリ配下から lab_name に一致する *.clab.yml を探す。

    ``name`` が一致するファイルが見つからない場合は None を返す。カレント
    ディレクトリに複数ラボのトポロジ YAML が存在しうる環境で、無関係な
    候補（例: 最初に見つかった別ラボのファイル）へフォールバックすると、
    ``snapshot_and_save_configs(mode="startup")`` 等で別ラボの
    startup-config を誤って上書きする恐れがあるため、フォールバックはしない。
    """
    candidates = _topology_files(".")
    matches: list[str] = []
    for path in candidates:
        try:
            data = _load_topo_yaml(path)
        except RuntimeError:
            continue
        if data.get("name") == lab_name:
            matches.append(path)
    if len(matches) > 1:
        raise ToolInputError(
            "AMBIGUOUS_TOPOLOGY",
            f"同じラボ名のトポロジが複数あります: {', '.join(matches)}",
        )
    return matches[0] if matches else None


def _topology_files(search_dir: str) -> list[str]:
    if not os.path.isdir(search_dir):
        raise ToolInputError(
            "NOT_FOUND", f"探索ディレクトリが存在しません: {search_dir}"
        )
    files: list[str] = []
    for root, directories, names in os.walk(search_dir):
        directories[:] = sorted(d for d in directories if not d.startswith("."))
        for name in sorted(names):
            if name.endswith((".clab.yml", ".clab.yaml")):
                path = os.path.join(root, name)
                files.append(os.path.relpath(path) if search_dir == "." else path)
    return files


def _execution_context() -> dict[str, Any]:
    return {
        "discovery_backend": "api" if CLAB_API_URL else "ssh" if CLAB_HOST else "local",
        "discovery_host": (urlsplit(CLAB_API_URL).hostname or "API server")
        if CLAB_API_URL
        else CLAB_HOST or "localhost",
        "host_command_backend": "ssh" if CLAB_HOST else "local",
        "execution_host": CLAB_HOST or "localhost",
        "local_working_dir": os.getcwd(),
        "topology_files_location": "mcp_server",
        "sudo": CLAB_SUDO,
    }


def _select_nodes(
    nodes: list[dict[str, Any]],
    node_names: Optional[list[str]] = None,
    node_filter_regex: Optional[str] = None,
    node_labels: Optional[dict[str, str]] = None,
) -> list[dict[str, Any]]:
    if node_names is not None:
        if not node_names or any(not isinstance(n, str) or not n for n in node_names):
            raise ToolInputError(
                "INVALID_ARGUMENT",
                "node_names は空でないノード名のリストで指定してください",
            )
        if node_filter_regex is not None:
            raise ToolInputError(
                "INVALID_ARGUMENT",
                "node_names と node_filter_regex は同時に指定できません",
            )
        missing = sorted(set(node_names) - {n["name"] for n in nodes})
        if missing:
            available = ", ".join(n["name"] for n in nodes)
            raise ToolInputError(
                "NODE_NOT_FOUND",
                f"ノードが見つかりません: {', '.join(missing)}。候補: {available}",
            )
    pattern = None
    if node_filter_regex:
        try:
            pattern = re.compile(node_filter_regex)
        except re.error as exc:
            raise ToolInputError(
                "INVALID_ARGUMENT", f"不正な node_filter_regex: {exc}"
            ) from exc
    if node_labels is not None and (
        not isinstance(node_labels, dict)
        or any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in node_labels.items()
        )
    ):
        raise ToolInputError(
            "INVALID_ARGUMENT", "node_labels は文字列のキーと値で指定してください"
        )
    selected = [
        n
        for n in nodes
        if (node_names is None or n["name"] in node_names)
        and (pattern is None or pattern.search(n["name"]))
        and all(
            (n.get("labels") or {}).get(k) == v for k, v in (node_labels or {}).items()
        )
    ]
    if not selected:
        raise ToolInputError("NO_NODES_SELECTED", "条件に一致するノードがありません")
    return selected


def _labels_from_topology(topo: dict[str, Any], node: dict[str, Any]) -> dict[str, str]:
    topology = topo.get("topology") or {}
    definition = (topology.get("nodes") or {}).get(node["name"]) or {}
    kind = definition.get("kind") or node.get("kind")
    layers = [
        topology.get("defaults") or {},
        (topology.get("kinds") or {}).get(kind) or {},
        (topology.get("groups") or {}).get(definition.get("group")) or {},
        definition,
    ]
    labels: dict[str, str] = {}
    for layer in layers:
        values = layer.get("labels") or {}
        if not isinstance(values, dict):
            raise ToolInputError(
                "INVALID_TOPOLOGY", "トポロジの labels はマッピングで指定してください"
            )
        labels.update({str(k): str(v) for k, v in values.items()})
    return labels


def _command_nodes(
    lab_name: str,
    node_names: Optional[list[str]],
    node_filter_regex: Optional[str],
    node_labels: Optional[dict[str, str]],
) -> tuple[list[dict[str, Any]], list[str]]:
    nodes = _inspect_nodes(lab_name)
    warnings: list[str] = []
    if node_labels and any(n.get("labels") is None for n in nodes):
        missing = [n for n in nodes if n.get("labels") is None]
        try:
            live_labels = _inspect_node_labels(missing)
            nodes = [
                {**n, "labels": live_labels[n["container"]]}
                if n.get("labels") is None
                else n
                for n in nodes
            ]
        except (RuntimeError, ValueError, KeyError) as exc:
            topo_path = _find_topo_for_lab(lab_name)
            if topo_path:
                topo = _load_topo_yaml(topo_path)
                nodes = [
                    {**n, "labels": _labels_from_topology(topo, n)}
                    if n.get("labels") is None
                    else n
                    for n in nodes
                ]
                warnings.append(
                    f"稼働ラベルを取得できず、ローカルトポロジ YAML から補完しました ({exc})。稼働構成との一致を確認してください。"
                )
            else:
                raise ToolInputError(
                    "LABELS_UNAVAILABLE",
                    f"稼働ラベルを取得できず、一致するローカルトポロジ YAML もありません: {exc}",
                ) from exc
    return _select_nodes(nodes, node_names, node_filter_regex, node_labels), warnings


def _inspect_node_labels(nodes: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    containers = [node["container"] for node in nodes]
    template = '{"Name":{{json .Name}},"Labels":{{json .Config.Labels}}}'
    proc = _run_host_command(
        _host_argv(
            ["docker", "inspect", "--format", template, "--", *containers], timeout=30
        ),
        30,
        "稼働ラベルの取得",
    )
    if proc.returncode:
        raise RuntimeError(
            f"docker inspect 失敗 (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    entries = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    labels = {}
    for entry in entries:
        values = entry.get("Labels") or {}
        if not isinstance(values, dict):
            raise RuntimeError("docker inspect のラベル情報が不正です")
        labels[entry["Name"].lstrip("/")] = {str(k): str(v) for k, v in values.items()}
    return labels


def _topo_nodes_and_links(topo: dict[str, Any]) -> tuple[dict[str, Any], list[Any]]:
    topology = topo.get("topology", {}) or {}
    if not isinstance(topology, dict):
        raise RuntimeError("トポロジの topology はマッピングで指定してください")
    nodes = topology.get("nodes", {}) or {}
    links = topology.get("links", []) or []
    if not isinstance(nodes, dict) or any(
        not isinstance(name, str) or not isinstance(node, (dict, type(None)))
        for name, node in nodes.items()
    ):
        raise RuntimeError("トポロジの nodes はノード名とマッピングで指定してください")
    if not isinstance(links, list):
        raise RuntimeError("トポロジの links はリストで指定してください")
    for node in nodes.values():
        if node and node.get("startup-config") is not None and not isinstance(
            node["startup-config"], str
        ):
            raise RuntimeError("startup-config はパス文字列で指定してください")
    return nodes, links


def _safe_join(base_dir: str, *parts: str) -> str:
    """base_dir 配下に限定してパスを結合する。

    トポロジ YAML の ``startup-config`` フィールドやノード名（YAML の dict
    キーであり、外部入力に由来しうる）が ``../`` や絶対パスで base_dir を
    脱出しようとする場合に備えた防御。脱出を検出すると ValueError を送出する。
    """
    base_abs = os.path.realpath(base_dir or ".")
    target_abs = os.path.realpath(os.path.join(base_abs, *parts))
    if os.path.commonpath([base_abs, target_abs]) != base_abs:
        raise ValueError(
            f"許可されたディレクトリ外へのパスです: {os.path.join(*parts)!r} (base={base_dir})"
        )
    return target_abs


def _ensure_parent_dir(path: str) -> None:
    """ファイルの親ディレクトリを作成する（存在しなければ）。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)


def _clab_host_fs_warning() -> list[str]:
    """CLAB_HOST 使用時、snapshot/restore がローカル FS にしか読み書きしない旨の警告行。

    snapshot_and_save_configs / restore_startup_configs はこの MCP サーバーの
    ローカルファイルシステムに対してのみ読み書きする（``deploy_lab`` 等と異なり
    リモートホストへは書き込まない）。CLAB_HOST 設定時にこれを黙って実行すると、
    「ローカルに保存したのにリモートに反映されない」といった不整合に気付き
    にくいため、結果サマリの先頭に明示する。
    """
    if not CLAB_HOST:
        return []
    return [
        f"⚠ CLAB_HOST={CLAB_HOST} が設定されていますが、ファイル読み書きはこの MCP "
        "サーバーのローカルファイルシステムに対して行われました。リモート側と"
        "ディレクトリを同期していない場合、復元/デプロイに反映されません。",
    ]


def _startup_path_for_node(
    topo: dict[str, Any],
    node_name: str,
    default_startup_dir: str,
    base_dir: str = ".",
) -> str:
    """トポロジ定義から node の startup-config パスを解決（未定義なら既定パス）。

    ``base_dir`` 配下に限定して解決する。トポロジ YAML の内容やノード名が
    base_dir を脱出しようとする場合は ValueError を送出する（呼び出し側で
    ノード単位のエラーとして扱うこと）。
    """
    nodes, _ = _topo_nodes_and_links(topo)
    node_def = nodes.get(node_name, {}) or {}
    startup = node_def.get("startup-config")
    if startup:
        return _safe_join(base_dir, startup)
    return _safe_join(base_dir, default_startup_dir, f"{node_name}.conf")


# =============================================================================
# === Inventory Builder (kind -> Nornir Host, file-free) ======================
# =============================================================================

def _netmiko_device_type(kind: str) -> str:
    """clab kind から Netmiko device_type を決定する。"""
    if kind == "linux":
        return DEFAULT_LINUX_DEVICE_TYPE
    if kind in CLAB_TO_PLATFORM:
        return CLAB_TO_PLATFORM[kind]
    # ヒューリスティックなフォールバック
    if kind.startswith("arista"):
        return "arista_eos"
    if kind.startswith("juniper"):
        return "juniper_junos"
    if "xr" in kind:
        return "cisco_xr"
    if "n9kv" in kind or "nxos" in kind:
        return "cisco_nxos"
    if kind.startswith("cisco"):
        return "cisco_ios"
    return DEFAULT_LINUX_DEVICE_TYPE


def _credentials_for(kind: str) -> tuple[str, str]:
    """kind の既定認証情報を返す（環境変数で上書き可能）。"""
    default_user, default_pass = KIND_DEFAULTS.get(kind, ("admin", "admin"))
    env_key = kind.upper()
    user = os.environ.get(f"CLAB_USER_{env_key}", default_user)
    password = os.environ.get(f"CLAB_PASS_{env_key}", default_pass)
    return user, password


def _build_host(node: dict[str, Any]) -> Optional[tuple[str, Host]]:
    """正規化ノード dict から Host を生成する。SSH ノードには mgmt_ip が必要。"""
    name = node["name"]
    kind = node.get("kind", "linux")
    mgmt_ip = node.get("mgmt_ip")
    if not mgmt_ip and kind != "linux":
        return None

    device_type = _netmiko_device_type(kind)
    user, password = _credentials_for(kind)

    extras: dict[str, Any] = {
        "device_type": device_type,
        "fast_cli": False,
        "read_timeout_override": NETMIKO_READ_TIMEOUT,
    }
    if NETMIKO_SSH_CONFIG:
        extras["ssh_config_file"] = NETMIKO_SSH_CONFIG

    host = Host(
        name=name,
        hostname=mgmt_ip,
        username=user,
        password=password,
        platform=device_type,
        data={
            "kind": kind,
            "container": node.get("container"),
            "device_type": device_type,
        },
        connection_options={
            "netmiko": ConnectionOptions(
                hostname=mgmt_ip,
                username=user,
                password=password,
                platform=device_type,
                extras=extras,
            )
        },
    )
    return name, host


def _build_nornir(
    nodes: list[dict[str, Any]], node_filter_regex: Optional[str] = None
) -> Nornir:
    """正規化ノード群からメモリ上で Nornir を初期化する（file-free）。

    ``node_filter_regex`` があればノード短名で絞り込む。
    """
    pattern = None
    if node_filter_regex:
        try:
            pattern = re.compile(node_filter_regex)
        except re.error as exc:
            raise RuntimeError(
                f"不正な node_filter_regex です: {node_filter_regex!r} ({exc})"
            ) from exc

    hosts = Hosts()
    for node in nodes:
        if pattern and not pattern.search(node["name"]):
            continue
        built = _build_host(node)
        if built is None:
            continue
        name, host = built
        hosts[name] = host

    if not hosts:
        raise RuntimeError(
            "対象ノードが 0 件です（フィルタ条件または mgmt IP を確認してください）"
        )

    inventory = Inventory(hosts=hosts, groups=Groups(), defaults=Defaults())
    num_workers = max(1, min(len(hosts), NORNIR_WORKERS))
    runner = ThreadedRunner(num_workers=num_workers)
    return Nornir(inventory=inventory, runner=runner, config=Config())


# =============================================================================
# === Nornir Runtime (command resolution + tasks) =============================
# =============================================================================

def resolve_command(command_or_alias: str, kind: str) -> str:
    """エイリアスを kind 別コマンドに解決する。未定義はリテラルとして返す。"""
    aliases = COMMAND_ALIASES.get(command_or_alias)
    if aliases is None:
        return command_or_alias  # 完全リテラル
    return aliases.get(kind, command_or_alias)  # kind 未定義ならリテラルにフォールバック


def _docker_exec_argv(
    container: str, command: str, timeout: Optional[int] = None
) -> list[str]:
    """linux kind ノードへの docker exec 呼び出し argv を組み立てる。"""
    return _host_argv(["docker", "exec", container, "sh", "-c", command], timeout=timeout)


class _DockerExecError(RuntimeError):
    """docker exec の終了コードを保持し、取得対象外と実行失敗を区別する。"""

    def __init__(self, proc: subprocess.CompletedProcess) -> None:
        self.returncode = proc.returncode
        super().__init__(
            f"docker exec 失敗 (rc={proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )


def _run_docker_exec(container: str, command: str, timeout: int) -> str:
    """linux kind ノードへ docker exec でコマンドを送り、標準出力を返す。

    containerlab の linux/FRR 系イメージは通常 sshd を持たないため、Netmiko(SSH)
    ではなく docker exec でコンテナへ直接コマンドを送り込む（scripts/clab-exec-all,
    scripts/clab-cli と同じ方式）。``CLAB_HOST`` 設定時は ssh 経由でリモートの
    containerlab ホスト上で docker exec を実行する。
    """
    proc = _run_host_command(
        _docker_exec_argv(container, command, timeout=timeout), timeout,
        f"docker exec ({container})",
    )
    if proc.returncode != 0:
        raise _DockerExecError(proc)
    return proc.stdout


def _dispatch_command(
    task: Task, kind: str, command: str, use_textfsm: bool
) -> Any:
    """kind に応じてコマンドを実行し、生の結果（構造化データ or 文字列）を返す。

    kind=="linux"（containerlab の FRR/linux 系イメージ）は sshd を持たないのが
    通例のため docker exec 経由で実行し、それ以外（ネットワーク OS kind）は
    Netmiko(SSH) 経由で実行する。``run_parallel_command``/``run_node_command``
    (``_run_command_task``) と ``run_topology_tests``（``_test_task``）の両方が
    この関数を共有することで、docker exec 分岐の適用漏れを防ぐ。
    """
    if kind == "linux":
        container = task.host.data.get("container") or task.host.name
        return _run_docker_exec(container, command, NETMIKO_READ_TIMEOUT)

    try:
        sub = task.run(
            task=netmiko_send_command,
            command_string=command,
            use_textfsm=use_textfsm,
            read_timeout=NETMIKO_READ_TIMEOUT,
        )
        return sub.result
    except NornirSubTaskError as exc:
        # task.run()（ネストされたサブタスク呼び出し）は原因の型を問わず常に
        # NornirSubTaskError でラップして再送出するため、実際の原因は
        # exc.result[0].exception を見て判別する必要がある。
        # TextFSM テンプレート不一致等の「解析失敗」のみ生テキストで再試行する。
        # 接続断・認証エラー等はここでは捕捉せず、そのまま呼び出し元へ伝播させる
        # （非冪等コマンドを不必要に再実行しないため）。
        cause = exc.result[0].exception
        if use_textfsm and isinstance(
            cause, (NetmikoParsingException, ValueError, TextFSMError)
        ):
            sub = task.run(
                task=netmiko_send_command,
                command_string=command,
                use_textfsm=False,
                read_timeout=NETMIKO_READ_TIMEOUT,
            )
            return sub.result
        raise


def _run_command_task(
    task: Task, command_or_alias: str, use_textfsm: bool
) -> Result:
    """各ホストでエイリアス解決したコマンドを実行する Nornir タスク。"""
    kind = task.host.data.get("kind", "linux")
    command = resolve_command(command_or_alias, kind)
    output = _dispatch_command(task, kind, command, use_textfsm)
    return Result(host=task.host, result={"command": command, "output": output})


def _collect_config_task(task: Task) -> Result:
    """ノードの設定全文を取得する Nornir タスク。

    kind=="linux"（FRR 系イメージ）は sshd を持たないため docker exec 経由で
    vtysh を叩いて取得する。vtysh を持たないプレーンな linux コンテナ（L2
    スイッチ役等）ではコマンドが失敗するため、その場合は KIND_COMMAND 未定義
    kind と同様に取得対象外としてスキップする。それ以外の kind は
    KIND_COMMAND（未定義なら取得対象外）を用いて Netmiko 経由で取得する。
    """
    kind = task.host.data.get("kind", "linux")

    if kind == "linux":
        container = task.host.data.get("container") or task.host.name
        try:
            config_text = _run_docker_exec(
                container, LINUX_CONFIG_COMMAND, NETMIKO_READ_TIMEOUT
            )
        except _DockerExecError as exc:
            if exc.returncode != 127:
                raise
            # vtysh が無い（FRR 以外の）linux コンテナは取得対象外扱いにする
            return Result(
                host=task.host,
                result="",
                failed=False,
                changed=False,
                severity_level=logging.WARNING,
            )
        return Result(host=task.host, result=config_text)

    spec = KIND_COMMAND.get(kind)
    if not spec:
        # KIND_COMMAND 未定義 kind は取得対象外
        return Result(
            host=task.host,
            result="",
            failed=False,
            changed=False,
            severity_level=logging.WARNING,
        )

    conn = task.host.get_connection("netmiko", task.nornir.config)
    lines = spec.split("\n")
    for prep in lines[:-1]:
        prep = prep.strip()
        if prep == "enable":
            try:
                conn.enable()
            except Exception as exc:  # noqa: BLE001 - enable 不要/失敗は続行
                logger.debug(
                    "enable() failed or not required for %s: %s", task.host.name, exc
                )
        elif prep:
            conn.send_command(prep, read_timeout=NETMIKO_READ_TIMEOUT)

    config_text = conn.send_command(lines[-1], read_timeout=NETMIKO_READ_TIMEOUT)
    return Result(host=task.host, result=config_text)


def _unwrap_exception(exc: BaseException) -> BaseException:
    """NornirSubTaskError のラップを剥がし、実際の原因例外を取り出す。

    ``task.run()``（ネストされたサブタスク呼び出し）は原因の型を問わず常に
    NornirSubTaskError で包んで再送出するため、ユーザー向けのエラー表示では
    ラップを剥がした実際の例外を見せないと診断できない。
    """
    while isinstance(exc, NornirSubTaskError) and exc.result and exc.result[0].exception:
        exc = exc.result[0].exception
    return exc


def _format_results(agg: Any) -> dict[str, dict[str, Any]]:
    """AggregatedResult を LLM 向けの正規化 dict に整形する。"""
    formatted: dict[str, dict[str, Any]] = {}
    for host_name, multi in agg.items():
        top = multi[0]
        if top.failed:
            exc = _unwrap_exception(top.exception) if top.exception else None
            formatted[host_name] = {
                "failed": True,
                "error": f"{type(exc).__name__}: {exc}" if exc else "unknown error",
            }
        else:
            formatted[host_name] = {"failed": False, "result": top.result}
    return formatted


def _run_nornir(
    nr: Nornir, task: Callable[..., Result], **kwargs: Any
) -> dict[str, dict[str, Any]]:
    """Nornir タスクを実行し、接続を確実にクローズして整形結果を返す。"""
    try:
        agg = nr.run(task=task, **kwargs)
        return _format_results(agg)
    finally:
        try:
            nr.close_connections()
        except Exception as exc:  # noqa: BLE001
            logger.warning("close_connections failed: %s", exc)


def _execute_selected_nodes(
    nodes: list[dict[str, Any]],
    task: Callable[..., Result],
    **kwargs: Any,
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    eligible = []
    for node in nodes:
        if node.get("kind", "linux") != "linux" and not node.get("mgmt_ip"):
            results[node["name"]] = {
                "status": "skipped",
                "reason": "SSH接続に必要な管理IPがありません",
            }
        else:
            eligible.append(node)
    if eligible:
        raw = _run_nornir(_build_nornir(eligible), task, **kwargs)
        for name, result in raw.items():
            results[name] = (
                {"status": "error", "error": error_detail(result["error"], name)}
                if result["failed"]
                else {"status": "success", "result": result["result"]}
            )
    return results


def _command_response(
    tool: str,
    lab_name: str,
    command: str,
    nodes: list[dict[str, Any]],
    results: dict[str, dict[str, Any]],
    max_output_chars: int,
    warnings: list[str],
) -> ToolResponse:
    by_name = {node["name"]: node for node in nodes}
    truncated = False
    for name, result in results.items():
        node = by_name[name]
        kind = node.get("kind", "linux")
        result.update(
            kind=kind,
            connection={
                "method": "docker_exec" if kind == "linux" else "ssh",
                "destination": node.get("container")
                if kind == "linux"
                else node.get("mgmt_ip"),
            },
        )
        if result["status"] == "success":
            raw = result.pop("result")
            result["command"] = (
                raw.get("command", command) if isinstance(raw, dict) else command
            )
            output = raw.get("output") if isinstance(raw, dict) else raw
            result["output"], result["output_info"] = present_output(
                output, max_output_chars
            )
            truncated |= result["output_info"]["truncated"]
    succeeded = sum(r["status"] == "success" for r in results.values())
    failed = sum(r["status"] == "error" for r in results.values())
    skipped = sum(r["status"] == "skipped" for r in results.values())
    errors = [r["error"] for r in results.values() if r["status"] == "error"]
    if truncated:
        warnings.append(
            "一部の出力を省略しました。output_info を確認してください。JSONの場合はテキスト断片です。"
        )
    return response(
        tool,
        f"{len(results)}台中{succeeded}台で成功、{failed}台で失敗、{skipped}台をスキップしました。",
        data={
            "lab": lab_name,
            "command": command,
            "results": results,
            "selected_nodes": list(by_name),
            "context": _execution_context(),
        },
        succeeded=succeeded,
        failed=failed,
        skipped=skipped,
        errors=errors,
        warnings=warnings,
        next_steps=[
            "output_id と next_offset を read_command_output に渡すと続きを取得できます。"
        ]
        if truncated
        else [],
    )


def _success_output(tool: str, summary: str, output: Any, **data: Any) -> ToolResponse:
    value, info = present_output(output, 5000)
    return response(
        tool,
        summary,
        data={
            **data,
            "output": value,
            "output_info": info,
            "context": _execution_context(),
        },
        succeeded=1,
        warnings=["出力を省略しました。output_info を確認してください。"]
        if info["truncated"]
        else [],
    )


# =============================================================================
# === Test Engine (test.yml 再帰探索 + PASS/FAIL 判定) ========================
# =============================================================================

def _discover_test_files(path: str) -> list[str]:
    """パスから test.yml を再帰的に収集する。"""
    if os.path.isfile(path):
        return [path]
    if os.path.isdir(path):
        found = glob.glob(os.path.join(path, "**", "test.yml"), recursive=True)
        found += glob.glob(os.path.join(path, "**", "test.yaml"), recursive=True)
        return sorted(set(found))
    raise RuntimeError(f"テストパスが存在しません: {path}")


def _load_test_cases(test_file: str) -> tuple[Optional[str], list[dict[str, Any]]]:
    """test.yml をロードし (lab_name, cases) を返す。"""
    data = _load_yaml(test_file)
    cases: Any
    if data is None:
        data = {}
    if isinstance(data, list):
        lab_name, cases = None, data
    elif isinstance(data, dict):
        lab_name = data.get("lab") or data.get("lab_name")
        cases = data.get("tests", data.get("cases", []))
    else:
        raise RuntimeError(f"テスト YAML はマッピングまたはリストで指定してください: {test_file}")
    if lab_name is not None and not isinstance(lab_name, str):
        raise RuntimeError(f"lab は文字列で指定してください: {test_file}")
    if cases is None:
        cases = []
    if not isinstance(cases, list) or any(not isinstance(case, dict) for case in cases):
        raise RuntimeError(f"tests はテストケースのマッピングのリストで指定してください: {test_file}")
    return lab_name, cases


_MAX_REGEX_INPUT_LEN = 100_000  # regex アサーション評価対象の上限文字数（ReDoS対策）


def _evaluate_assertion(output: str, assertion: dict[str, Any]) -> tuple[bool, str]:
    """contains / regex / exit_code 判定を行う。"""
    if "contains" in assertion:
        needle = str(assertion["contains"])
        ok = needle in output
        return ok, f"contains {needle!r}: {'OK' if ok else 'NG'}"

    if "regex" in assertion:
        pat = str(assertion["regex"])
        # ReDoS 対策: 破局的バックトラッキングの被害を限定するため評価対象を切り詰める。
        target = output[:_MAX_REGEX_INPUT_LEN]
        ok = re.search(pat, target) is not None
        return ok, f"regex {pat!r}: {'OK' if ok else 'NG'}"

    if "exit_code" in assertion:
        expected = int(assertion["exit_code"])
        match = re.search(r"(?:^|\n)__RC__=(\d+)\s*\Z", output)
        if not match:
            # __RC__ マーカーの付与は linux kind のみ（_run_test_case 参照）。
            # マーカーが無い場合に actual=0 とみなすと、コマンド自体が失敗
            # している NOS ノードでも exit_code: 0 が誤って PASS してしまう
            # ため、判定不能として明示的に FAIL とする。
            return False, (
                "exit_code 判定不能: 出力に __RC__ マーカーがありません"
                "（exit_code アサーションは linux kind のみサポートしています）"
            )
        actual = int(match.group(1))
        ok = actual == expected
        return ok, f"exit_code expected={expected} actual={actual}: {'OK' if ok else 'NG'}"

    return False, "アサーション条件がありません (contains/regex/exit_code)"


def _test_failure(name: str, node: str, detail: str) -> dict[str, Any]:
    return {"test": name, "node": node, "passed": False, "detail": detail}


def _run_test_case(lab_name: str, case: dict[str, Any]) -> list[dict[str, Any]]:
    """1 テストケースを対象ノード群で並列実行し結果リストを返す。"""
    name = case.get("name", "unnamed")
    command_or_alias = case.get("command") or case.get("alias")
    node_filter = case.get("nodes", case.get("node"))
    assertion = case.get("assert")
    if assertion is None:
        assertion = {}

    if not command_or_alias:
        return [_test_failure(name, "-", "command 未指定")]
    if not isinstance(command_or_alias, str) or not isinstance(assertion, dict):
        return [
            _test_failure(
                name, "-", "command は文字列、assert はマッピングで指定してください"
            )
        ]
    if node_filter is not None and not isinstance(node_filter, (str, list)):
        return [
            _test_failure(
                name, "-", "nodes はノード名リストまたは正規表現で指定してください"
            )
        ]

    try:
        nodes, warnings = _command_nodes(
            lab_name,
            node_filter if isinstance(node_filter, list) else None,
            node_filter if isinstance(node_filter, str) else None,
            case.get("node_labels"),
        )
    except (RuntimeError, ValueError) as exc:
        return [_test_failure(name, "-", str(exc))]

    # exit_code 判定がある場合は linux コマンドに RC 収集を付与
    wants_exit_code = "exit_code" in assertion

    def _test_task(task: Task) -> Result:
        kind = task.host.data.get("kind", "linux")
        command = resolve_command(command_or_alias, kind)
        if wants_exit_code:
            if kind != "linux":
                raise RuntimeError(
                    "exit_code アサーションは linux kind のみサポートしています"
                )
            # 別シェルに閉じ込め、exit や末尾コメントでも RC 収集を実行する。
            command = f"sh -c {shlex.quote(command)}; printf '\\n__RC__=%s\\n' \"$?\""
        output = _dispatch_command(task, kind, command, use_textfsm=False)
        return Result(host=task.host, result=output)

    try:
        results = _execute_selected_nodes(nodes, _test_task)
    except RuntimeError as exc:
        return [_test_failure(name, str(node_filter or "*"), str(exc))]

    outcomes: list[dict[str, Any]] = []
    for host_name, res in results.items():
        if res["status"] != "success":
            detail = (
                res["error"]["message"] if res["status"] == "error" else res["reason"]
            )
            outcomes.append(_test_failure(name, host_name, detail))
            continue
        ok, detail = _evaluate_assertion(str(res["result"]), assertion)
        outcomes.append(
            {
                "test": name,
                "node": host_name,
                "passed": ok,
                "detail": detail,
                "warnings": warnings,
            }
        )
    return outcomes


# =============================================================================
# === MCP Tools ===============================================================
# =============================================================================

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def list_labs() -> ToolResponse:
    """稼働ホストのラボ一覧を取得する。ラボ名が分からないとき最初に使う。

    ラボ名、ノード数、状態、ホスト側トポロジパスを返す。ローカルの未デプロイ
    トポロジは list_topologies で探索する。毎回最新の inspect 結果を取得する。
    """
    try:
        containers = _inspect_containers()
        labs: dict[str, dict[str, Any]] = {}
        errors: list[dict[str, Any]] = []
        for container in containers:
            lab = container.get("lab_name") or container.get("labName")
            if not lab:
                errors.append(
                    error_detail(
                        ToolInputError(
                            "LAB_NAME_MISSING",
                            f"ラボ名を判別できません: {container.get('name', '(unknown)')}",
                        )
                    )
                )
                continue
            entry = labs.setdefault(
                lab,
                {
                    "lab_name": lab,
                    "execution_host": _execution_context()["discovery_host"],
                    "topology_path": container.get("absLabPath")
                    or container.get("labPath"),
                    "topology_path_location": "execution_host",
                    "nodes": [],
                },
            )
            entry["nodes"].append(
                {
                    "name": _short_node_name(
                        container.get("name") or container.get("container") or "", lab
                    ),
                    "kind": container.get("kind") or container.get("Kind"),
                    "state": container.get("state")
                    or container.get("State")
                    or "unknown",
                }
            )
        for entry in labs.values():
            entry["node_count"] = len(entry["nodes"])
            states = {node["state"] for node in entry["nodes"]}
            entry["state"] = next(iter(states)) if len(states) == 1 else "mixed"
        entries = [labs[name] for name in sorted(labs)]
        return response(
            "list_labs",
            f"{len(entries)}件のラボが見つかりました。",
            data={"labs": entries, "context": _execution_context()},
            succeeded=len(entries),
            failed=len(errors),
            errors=errors,
            next_steps=["inspect_lab_topology に lab_name を渡して構成を確認できます。"]
            if entries
            else [],
        )
    except Exception as exc:  # noqa: BLE001
        return failure("list_labs", exc, data={"context": _execution_context()})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def list_topologies(search_dir: str = ".") -> ToolResponse:
    """MCPサーバー側で *.clab.yml / *.clab.yaml を再帰探索する。

    ラボ名、絶対パス、ノード名、ノード数、リンク数を返す。リモートホストの
    ファイル探索ではない。同じラボ名の複数ファイルも省略せず列挙する。
    """
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    try:
        for path in _topology_files(search_dir):
            try:
                topo = _load_topo_yaml(path)
                nodes, links = _topo_nodes_and_links(topo)
                if not isinstance(topo.get("name"), str) or not topo["name"]:
                    raise ToolInputError(
                        "INVALID_TOPOLOGY", "name にラボ名を指定してください"
                    )
                entries.append(
                    {
                        "lab_name": topo["name"],
                        "path": os.path.abspath(path),
                        "node_names": sorted(nodes),
                        "node_count": len(nodes),
                        "link_count": len(links),
                        "location": "mcp_server",
                    }
                )
            except (RuntimeError, ValueError) as exc:
                errors.append({**error_detail(exc), "path": os.path.abspath(path)})
        return response(
            "list_topologies",
            f"{len(entries)}件のトポロジが見つかりました。",
            data={
                "topologies": entries,
                "search_dir": os.path.abspath(search_dir),
                "context": _execution_context(),
            },
            succeeded=len(entries),
            failed=len(errors),
            errors=errors,
        )
    except Exception as exc:  # noqa: BLE001
        return failure("list_topologies", exc)


def _connection_check_task(task: Task) -> Result:
    if task.host.data.get("kind", "linux") == "linux":
        _run_docker_exec(task.host.data.get("container") or task.host.name, "true", 15)
    else:
        task.host.get_connection("netmiko", task.nornir.config)
    return Result(host=task.host, result="接続・認証に成功しました")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def diagnose_environment(
    lab_name: Optional[str] = None,
    check_node_connections: bool = False,
) -> ToolResponse:
    """読み取り操作でホスト接続、clab、Docker、探索場所、ラボ検出を診断する。

    check_node_connections=True と lab_name を指定すると、Linuxは docker exec
    true、それ以外は管理IPへのNetmiko接続・認証を確認する。設定変更は行わない。
    APIモードでもCLI専用ツールのためホストのclab/Dockerは別々に診断する。
    """
    if check_node_connections and not lab_name:
        return failure(
            "diagnose_environment",
            ToolInputError(
                "INVALID_ARGUMENT", "ノード接続診断には lab_name を指定してください"
            ),
        )
    checks: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    def check(name: str, action: Callable[[], Any]) -> bool:
        try:
            result = action()
            checks.append({"name": name, "status": "success", "detail": result})
            return True
        except Exception as exc:  # noqa: BLE001
            error = error_detail(exc)
            checks.append({"name": name, "status": "error", "error": error})
            errors.append({**error, "check": name})
            return False

    def host_probe(argv: list[str]) -> str:
        proc = _run_host_command(argv, 15, "環境診断")
        if proc.returncode:
            raise RuntimeError(
                f"環境診断失敗 (rc={proc.returncode}): {proc.stderr.strip()}"
            )
        return proc.stdout.strip() or "接続に成功しました"

    host_ok = True
    if CLAB_HOST:
        host_ok = check(
            "host_ssh", lambda: host_probe(_remote_argv(["true"], timeout=15))
        )
    else:
        checks.append(
            {"name": "host_ssh", "status": "skipped", "detail": "ローカル実行です"}
        )
    if host_ok:
        check("clab_version", lambda: _run_clab(["version"], timeout=15).stdout.strip())
        check(
            "docker_access",
            lambda: host_probe(
                _host_argv(
                    ["docker", "info", "--format", "{{.ServerVersion}}"], timeout=15
                )
            ),
        )
    else:
        for name in ("clab_version", "docker_access"):
            checks.append(
                {
                    "name": name,
                    "status": "skipped",
                    "detail": "ホストSSH接続を先に修復してください",
                }
            )
    check(
        "topology_search",
        lambda: {
            "search_dir": os.getcwd(),
            "file_count": len(_topology_files(".")),
            "location": "mcp_server",
        },
    )
    if host_ok or CLAB_API_URL:
        check("lab_discovery", lambda: {"container_count": len(_inspect_containers())})
    else:
        checks.append(
            {
                "name": "lab_discovery",
                "status": "skipped",
                "detail": "ホスト接続に失敗しました",
            }
        )
    if lab_name and (host_ok or CLAB_API_URL):

        def lab_check() -> dict[str, Any]:
            nodes = _inspect_nodes(lab_name)
            detail: dict[str, Any] = {"lab_name": lab_name, "node_count": len(nodes)}
            if check_node_connections:
                detail["results"] = _execute_selected_nodes(
                    nodes, _connection_check_task
                )
            return detail

        check("lab_nodes", lab_check)
        if checks[-1]["status"] == "success" and check_node_connections:
            for node, result in checks[-1]["detail"]["results"].items():
                if result["status"] == "error":
                    errors.append(result["error"])
                    checks.append(
                        {
                            "name": f"node:{node}",
                            "status": "error",
                            "error": result["error"],
                        }
                    )
                else:
                    checks.append(
                        {
                            "name": f"node:{node}",
                            "status": result["status"],
                            "detail": result.get("result") or result.get("reason"),
                        }
                    )
    elif lab_name:
        checks.append(
            {"name": "lab_nodes", "status": "skipped", "detail": "ホスト接続に失敗しました"}
        )
    succeeded = sum(c["status"] == "success" for c in checks)
    skipped = sum(c["status"] == "skipped" for c in checks)
    unchecked_nodes = any(
        c["name"].startswith("node:") and c["status"] == "skipped" for c in checks
    )
    return response(
        "diagnose_environment",
        f"診断: 成功{succeeded}件、失敗{len(errors)}件、未実施{skipped}件。",
        data={"checks": checks, "context": _execution_context()},
        errors=errors,
        succeeded=succeeded,
        failed=len(errors),
        skipped=skipped,
        status="partial"
        if (errors and succeeded) or unchecked_nodes
        else "error"
        if errors
        else "success",
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def read_command_output(
    output_id: str, offset: int = 0, max_output_chars: int = 5000
) -> ToolResponse:
    """省略されたコマンド出力の続きを取得する。元のコマンドは再実行しない。

    output_id と next_offset を前の結果から渡す。出力はこのサーバープロセス内で
    最大1時間・100件保持する。文字オフセットであり、JSONのページはテキスト断片。
    """
    try:
        data = OUTPUT_STORE.read(output_id, offset, max_output_chars)
        return response(
            "read_command_output",
            f"出力の{offset}文字目から取得しました。",
            data=data,
            succeeded=1,
        )
    except Exception as exc:  # noqa: BLE001
        return failure("read_command_output", exc)


@mcp.tool()
def deploy_lab(topo_yaml_path: str, reconfigure: bool = False) -> ToolResponse:
    """Containerlab トポロジ YAML から新規ラボをデプロイする。

    ``CLAB_HOST`` が設定されていれば ssh 経由でリモートホスト上に、未設定なら
    ローカルで ``clab deploy`` を実行する。``CLAB_API_URL`` 設定時は
    clab-api-server(httpx) 経由でデプロイする。

    Args:
        topo_yaml_path: デプロイする *.clab.yml トポロジファイルのパス。
        reconfigure: True の場合 ``clab deploy --reconfigure`` を付与し、
            既存の設定成果物を再生成して上書きする（``CLAB_API_URL`` 使用時は無視）。

    Returns:
        共通結果形式。data.output に実行出力、errors に原因と確認方法を含む。
    """
    try:
        if CLAB_API_URL:
            import httpx

            topo = _load_topo_yaml(topo_yaml_path)
            url = f"{CLAB_API_URL.rstrip('/')}/api/v1/labs"
            resp = httpx.post(url, json={"topology": topo}, timeout=600.0)
            resp.raise_for_status()
            return _success_output(
                "deploy_lab",
                "API デプロイ成功",
                resp.text,
                topology_path=topo_yaml_path,
            )

        if not CLAB_HOST and not os.path.isfile(topo_yaml_path):
            return failure(
                "deploy_lab",
                ToolInputError(
                    "NOT_FOUND", f"トポロジファイルが見つかりません: {topo_yaml_path}"
                ),
            )

        args = ["deploy", "-t", topo_yaml_path]
        if reconfigure:
            args.append("--reconfigure")
        proc = _run_clab(args, timeout=600)
        return _success_output(
            "deploy_lab",
            "デプロイ成功",
            proc.stdout.strip(),
            topology_path=topo_yaml_path,
        )
    except Exception as exc:  # noqa: BLE001
        return failure("deploy_lab", exc)


@mcp.tool()
def apply_lab(topo_yaml_path: str, dry_run: bool = False) -> ToolResponse:
    """トポロジ YAML と稼働中ラボの差分だけを反映する（containerlab 0.77+ の ``apply``）。

    ラボが存在しなければ新規デプロイし、既に稼働中ならノード/リンクの追加・削除など
    サポートされる変更のみを検出して反映する（全体再作成の ``deploy --reconfigure``
    や ``redeploy`` と異なり、変更の無いノードは再起動・再作成されない）。
    ``CLAB_HOST`` が設定されていれば ssh 経由でリモートホスト上で実行する。
    containerlab 0.77 未満には ``apply`` サブコマンド自体が存在しない点に注意。
    ``deploy_lab``/``destroy_lab`` と異なり ``CLAB_API_URL``（clab-api-server
    経由実行）には対応していない。設定されていても常に subprocess/ssh 経由で
    ``clab`` を実行する。

    Args:
        topo_yaml_path: 適用する *.clab.yml トポロジファイルのパス。
        dry_run: True の場合 ``--dry-run`` を付与し、実際には適用せず変更内容のみ表示する。

    Returns:
        共通結果形式。data.output に実行出力、errors に原因と確認方法を含む。
    """
    try:
        if not CLAB_HOST and not os.path.isfile(topo_yaml_path):
            return failure(
                "apply_lab",
                ToolInputError(
                    "NOT_FOUND", f"トポロジファイルが見つかりません: {topo_yaml_path}"
                ),
            )

        args = ["apply", "-t", topo_yaml_path]
        if dry_run:
            args.append("--dry-run")
        proc = _run_clab(args, timeout=600)
        return _success_output(
            "apply_lab",
            "変更内容を確認しました（未適用）" if dry_run else "適用成功",
            proc.stdout.strip(),
            topology_path=topo_yaml_path,
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        return failure("apply_lab", exc)


@mcp.tool()
def destroy_lab(topo_yaml_path: str, cleanup: bool = False) -> ToolResponse:
    """Containerlab トポロジ YAML からラボを破棄する。

    ``CLAB_HOST`` が設定されていれば ssh 経由でリモートホスト上に、未設定なら
    ローカルで ``clab destroy`` を実行する。``CLAB_API_URL`` 設定時は
    clab-api-server(httpx) 経由で破棄する。

    Args:
        topo_yaml_path: 破棄する *.clab.yml トポロジファイルのパス。
        cleanup: True の場合 ``clab destroy --cleanup`` を付与し、ラボディレクトリ
            (生成された設定・証明書等)ごと完全に削除する（``CLAB_API_URL`` 使用時は無視）。

    Returns:
        共通結果形式。data.output に実行出力、errors に原因と確認方法を含む。
    """
    try:
        if CLAB_API_URL:
            import httpx

            topo = _load_topo_yaml(topo_yaml_path)
            lab_name = topo.get("name")
            if not lab_name:
                return failure(
                    "destroy_lab",
                    ToolInputError(
                        "INVALID_TOPOLOGY",
                        "トポロジ YAML に name フィールドがありません",
                    ),
                )
            url = f"{CLAB_API_URL.rstrip('/')}/api/v1/labs/{quote(lab_name, safe='')}"
            resp = httpx.delete(url, timeout=600.0)
            resp.raise_for_status()
            return _success_output(
                "destroy_lab", "API 破棄成功", resp.text, topology_path=topo_yaml_path
            )

        if not CLAB_HOST and not os.path.isfile(topo_yaml_path):
            return failure(
                "destroy_lab",
                ToolInputError(
                    "NOT_FOUND", f"トポロジファイルが見つかりません: {topo_yaml_path}"
                ),
            )

        args = ["destroy", "-t", topo_yaml_path]
        if cleanup:
            args.append("--cleanup")
        proc = _run_clab(args, timeout=600)
        return _success_output(
            "destroy_lab", "破棄成功", proc.stdout.strip(), topology_path=topo_yaml_path
        )
    except Exception as exc:  # noqa: BLE001
        return failure("destroy_lab", exc)


@mcp.tool()
def redeploy_lab(topo_yaml_path: str, cleanup: bool = False) -> ToolResponse:
    """ラボを破棄してから同じトポロジ YAML で再デプロイする（``clab redeploy``）。

    ``destroy_lab`` に続けて ``deploy_lab`` を呼ぶのと違い、containerlab 自身の
    ``redeploy`` サブコマンドを1回の呼び出しで実行する。``CLAB_HOST`` が設定されて
    いれば ssh 経由でリモートホスト上で実行する。``deploy_lab``/``destroy_lab``
    と異なり ``CLAB_API_URL``（clab-api-server 経由実行）には対応していない。
    設定されていても常に subprocess/ssh 経由で ``clab`` を実行する。

    Args:
        topo_yaml_path: 対象の *.clab.yml トポロジファイルのパス。
        cleanup: True の場合 ``--cleanup`` を付与し、破棄時にラボディレクトリ
            (生成された設定・証明書等)ごと削除してから再デプロイする。

    Returns:
        共通結果形式。data.output に実行出力、errors に原因と確認方法を含む。
    """
    try:
        if not CLAB_HOST and not os.path.isfile(topo_yaml_path):
            return failure(
                "redeploy_lab",
                ToolInputError(
                    "NOT_FOUND", f"トポロジファイルが見つかりません: {topo_yaml_path}"
                ),
            )

        args = ["redeploy", "-t", topo_yaml_path]
        if cleanup:
            args.append("--cleanup")
        proc = _run_clab(args, timeout=600)
        return _success_output(
            "redeploy_lab",
            "再デプロイ成功",
            proc.stdout.strip(),
            topology_path=topo_yaml_path,
        )
    except Exception as exc:  # noqa: BLE001
        return failure("redeploy_lab", exc)


@mcp.tool()
def restart_lab_nodes(
    lab_name: str, node_names: Optional[list[str]] = None
) -> ToolResponse:
    """稼働中ラボのノードを再起動する（``clab restart``、seamless dataplane）。

    ``node_names`` を省略するとラボ内の全ノードを再起動する。破棄・再作成は
    行わないため、他ノードのデータプレーンには影響しない（containerlab 側の
    「seamless dataplane」restart）。

    Args:
        lab_name: 対象ラボ名（clab トポロジの name フィールド）。
        node_names: 再起動するノード短名のリスト（省略時は全ノード）。

    Returns:
        共通結果形式。data.output に実行出力、errors に原因と確認方法を含む。
    """
    try:
        if node_names is not None:
            _select_nodes(_inspect_nodes(lab_name), node_names=node_names)
        args = ["restart", "--name", lab_name]
        if node_names:
            args += ["-n", ",".join(node_names)]
        proc = _run_clab(args, timeout=300)
        return _success_output(
            "restart_lab_nodes",
            "再起動成功",
            proc.stdout.strip(),
            lab=lab_name,
            node_names=node_names,
        )
    except Exception as exc:  # noqa: BLE001
        return failure("restart_lab_nodes", exc)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def inspect_lab_topology(lab_name: str) -> ToolResponse:
    """稼働中ラボのノード、管理IP、kind、接続方式、リンクを取得する。

    リンクはMCPサーバー側で一致するトポロジYAMLを探して補完する。
    トポロジが無くても稼働ノードを返し、リンク不足をwarningsに明示する。
    """
    try:
        nodes = _inspect_nodes(lab_name)
        warnings: list[str] = []
        links: list[Any] = []
        topo_path = None
        try:
            topo_path = _find_topo_for_lab(lab_name)
            if topo_path:
                _, links = _topo_nodes_and_links(_load_topo_yaml(topo_path))
            else:
                warnings.append(
                    "一致するローカルトポロジ YAML が見つからずリンク情報は空です"
                )
        except (RuntimeError, ValueError) as exc:
            warnings.append(f"リンク情報の取得に失敗: {exc}")
        for node in nodes:
            node["connection_method"] = (
                "docker_exec" if node["kind"] == "linux" else "ssh"
            )
        return response(
            "inspect_lab_topology",
            f"{len(nodes)}台のノードと{len(links)}件のリンクを取得しました。",
            data={
                "lab": lab_name,
                "topo_path": topo_path,
                "nodes": nodes,
                "links": links,
                "context": _execution_context(),
            },
            warnings=warnings,
            succeeded=len(nodes),
        )
    except Exception as exc:  # noqa: BLE001
        return failure("inspect_lab_topology", exc, data={"lab": lab_name})


@mcp.tool()
def run_parallel_command(
    lab_name: str,
    command_or_alias: str,
    node_filter_regex: Optional[str] = None,
    node_names: Optional[list[str]] = None,
    node_labels: Optional[dict[str, str]] = None,
    max_output_chars: int = 5000,
) -> ToolResponse:
    """指定ラボのノードへ並列コマンドを実行する。

    node_names は短名の完全一致リスト（r1 は r10 に一致しない）。正規表現と
    同時指定は不可。node_labels はラベルの完全一致で、名前指定と組み合わせ可能。
    省略時は全ノード。存在しない名前があれば実行前にエラーを返す。
    コマンドはkind別エイリアスまたはリテラル。出力はノードごとに最大
    max_output_chars文字。省略分は read_command_output で再実行せず取得できる。
    """
    try:
        validate_output_limit(max_output_chars)
        nodes, warnings = _command_nodes(
            lab_name, node_names, node_filter_regex, node_labels
        )
        results = _execute_selected_nodes(
            nodes,
            _run_command_task,
            command_or_alias=command_or_alias,
            use_textfsm=True,
        )
        return _command_response(
            "run_parallel_command",
            lab_name,
            command_or_alias,
            nodes,
            results,
            max_output_chars,
            warnings,
        )
    except Exception as exc:  # noqa: BLE001
        return failure("run_parallel_command", exc, data={"lab": lab_name})


@mcp.tool()
def run_node_command(
    lab_name: str,
    node_name: str,
    command: Optional[str] = None,
    max_output_chars: int = 5000,
) -> ToolResponse:
    """短名が完全一致する1ノードだけでコマンドを実行する。

    command省略時はinterfaces。接続方式・宛先・解決したコマンド・出力・
    エラーの原因を共通結果形式で返す。省略された出力はread_command_outputで取得。
    """
    try:
        validate_output_limit(max_output_chars)
        nodes, warnings = _command_nodes(lab_name, [node_name], None, None)
        command_or_alias = command or "interfaces"
        results = _execute_selected_nodes(
            nodes,
            _run_command_task,
            command_or_alias=command_or_alias,
            use_textfsm=True,
        )
        return _command_response(
            "run_node_command",
            lab_name,
            command_or_alias,
            nodes,
            results,
            max_output_chars,
            warnings,
        )
    except Exception as exc:  # noqa: BLE001
        return failure(
            "run_node_command", exc, data={"lab": lab_name, "node": node_name}
        )


@mcp.tool()
def snapshot_and_save_configs(
    lab_name: str,
    mode: str = "snapshot",
    save_dir: str = "save",
    default_startup_dir: str = "startup-configs",
    node_names: Optional[list[str]] = None,
    node_labels: Optional[dict[str, str]] = None,
) -> ToolResponse:
    """ノードの設定を並列回収してローカルファイルに保存する。

    mode=snapshotは一意なスナップショット、mode=startupはstartup-configへの
    上書き。node_namesは完全一致、node_labelsはラベル一致で絞り込む。
    Linux/FRRはdocker exec、他kindはSSH。非対応kindやvtysh不在はスキップ。
    ファイルI/OはCLAB_HOSTにかかわらずMCPサーバー側。稼働機器には反映しない。
    """
    tool = "snapshot_and_save_configs"
    try:
        if mode not in ("snapshot", "startup"):
            raise ToolInputError(
                "INVALID_ARGUMENT", "mode は snapshot または startup を指定してください"
            )
        topo = None
        base_dir = "."
        if mode == "startup":
            topo_path = _find_topo_for_lab(lab_name)
            if not topo_path:
                raise ToolInputError(
                    "NOT_FOUND", "startup モードにはトポロジ YAML が必要です"
                )
            topo = _load_topo_yaml(topo_path)
            base_dir = os.path.dirname(os.path.abspath(topo_path))
        nodes, warnings = _command_nodes(lab_name, node_names, None, node_labels)
        results = _execute_selected_nodes(nodes, _collect_config_task)
        target_dir = ""
        if mode == "snapshot":
            os.makedirs(save_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            target_dir = tempfile.mkdtemp(prefix=f"save-{timestamp}-", dir=save_dir)
        kinds = {n["name"]: n.get("kind", "linux") for n in nodes}
        saved = []
        for name, result in results.items():
            if result["status"] != "success":
                continue
            config = str(result.pop("result")).strip()
            if not config:
                result.update(
                    status="skipped",
                    reason=(
                        "vtyshが利用できないか、設定出力が空です"
                        if kinds[name] == "linux"
                        else "設定取得が未対応のkind、または設定出力が空です"
                    ),
                )
                continue
            try:
                dest = (
                    _safe_join(target_dir, f"{name}.conf")
                    if mode == "snapshot"
                    else _startup_path_for_node(
                        topo or {}, name, default_startup_dir, base_dir
                    )
                )
                _ensure_parent_dir(dest)
                with open(dest, "w", encoding="utf-8") as fh:
                    fh.write(config + "\n")
                result["path"] = dest
                saved.append(dest)
            except (OSError, ValueError, RuntimeError) as exc:
                result.update(status="error", error=error_detail(exc, name))
        succeeded = sum(r["status"] == "success" for r in results.values())
        failed = sum(r["status"] == "error" for r in results.values())
        skipped = sum(r["status"] == "skipped" for r in results.values())
        return response(
            tool,
            f"保存成功: {succeeded} 件、失敗: {failed} 件、スキップ: {skipped} 件。",
            data={
                "lab": lab_name,
                "mode": mode,
                "saved": saved,
                "results": results,
                "snapshot_dir": os.path.abspath(target_dir) if target_dir else None,
                "file_location": "mcp_server",
            },
            succeeded=succeeded,
            failed=failed,
            skipped=skipped,
            errors=[r["error"] for r in results.values() if r["status"] == "error"],
            warnings=warnings + _clab_host_fs_warning(),
        )
    except Exception as exc:  # noqa: BLE001
        return failure(tool, exc)


@mcp.tool()
def restore_startup_configs(
    topo_path: str,
    snapshot_name: str = "latest",
    save_dir: str = "save",
) -> ToolResponse:
    """スナップショットからstartup-configを復元し、ノードごとの結果を返す。

    ファイルの読み書きはMCPサーバー側。稼働中機器への反映は行わない。
    snapshot_nameはlatestまたはsave_dir配下のディレクトリ名。
    """
    tool = "restore_startup_configs"
    try:
        topo = _load_topo_yaml(topo_path)
        snapshot_dir = _resolve_snapshot_dir(save_dir, snapshot_name)
        base_dir = os.path.dirname(os.path.abspath(topo_path))
        nodes, _ = _topo_nodes_and_links(topo)
        results: dict[str, dict[str, Any]] = {}
        for name in nodes:
            try:
                src = _safe_join(snapshot_dir, f"{name}.conf")
                dest = _startup_path_for_node(topo, name, "startup-configs", base_dir)
                if not os.path.isfile(src):
                    results[name] = {
                        "status": "skipped",
                        "reason": "スナップショットに設定がありません",
                    }
                    continue
                _ensure_parent_dir(dest)
                shutil.copyfile(src, dest)
                results[name] = {
                    "status": "success",
                    "source": src,
                    "destination": dest,
                }
            except (OSError, ValueError, RuntimeError) as exc:
                results[name] = {"status": "error", "error": error_detail(exc, name)}
        succeeded = sum(r["status"] == "success" for r in results.values())
        failed = sum(r["status"] == "error" for r in results.values())
        skipped = sum(r["status"] == "skipped" for r in results.values())
        return response(
            tool,
            f"復元成功: {succeeded} 件、失敗: {failed} 件、スキップ: {skipped} 件。",
            data={
                "snapshot_dir": snapshot_dir,
                "results": results,
                "file_location": "mcp_server",
                "applied_to_running_nodes": False,
            },
            succeeded=succeeded,
            failed=failed,
            skipped=skipped,
            errors=[r["error"] for r in results.values() if r["status"] == "error"],
            warnings=_clab_host_fs_warning(),
        )
    except Exception as exc:  # noqa: BLE001
        return failure(tool, exc)


def _resolve_snapshot_dir(save_dir: str, snapshot_name: str) -> str:
    """latest と明示名の両方で、復元元を save_dir 配下のディレクトリに限定する。"""
    if snapshot_name == "latest":
        candidates = sorted(
            path for path in glob.glob(os.path.join(glob.escape(save_dir), "save-*"))
            if os.path.isdir(path)
        )
        if not candidates:
            raise RuntimeError(f"スナップショットが見つかりません ({save_dir})")
        snapshot_name = os.path.basename(candidates[-1])
    snapshot_dir = _safe_join(save_dir, snapshot_name)
    if not os.path.isdir(snapshot_dir):
        raise RuntimeError(f"スナップショットが存在しません: {snapshot_dir}")
    return snapshot_dir


@mcp.tool()
def run_topology_tests(test_file_or_dir: str) -> ToolResponse:
    """test.yml/test.yamlを探索して実行し、共通形式でPASS/FAILを返す。

    nodesは完全一致の短名リスト、または従来の正規表現文字列。node_labelsは
    ラベルによる絞り込み。assertはcontains/regex/exit_code。exit_codeはLinuxのみ。
    ファイル読み込みや対象指定の失敗も失敗件数に含む。
    """
    tool = "run_topology_tests"
    try:
        files = _discover_test_files(test_file_or_dir)
    except RuntimeError as exc:
        return failure(tool, exc)
    if not files:
        return response(
            tool,
            "テストファイルが見つかりませんでした。",
            data={"verdict": "NO TESTS", "outcomes": []},
            status="skipped",
        )
    outcomes: list[dict[str, Any]] = []
    for path in files:
        try:
            lab_name, cases = _load_test_cases(path)
            if not lab_name:
                raise ToolInputError("INVALID_TEST_FILE", "lab / lab_name が未指定です")
        except Exception as exc:  # noqa: BLE001
            outcomes.append({**_test_failure(path, "-", str(exc)), "file": path})
            continue
        for case in cases:
            try:
                results = _run_test_case(lab_name, case)
            except Exception as exc:  # noqa: BLE001
                results = [_test_failure(case.get("name", "unnamed"), "-", str(exc))]
            outcomes.extend({**result, "file": path} for result in results)
    succeeded = sum(o["passed"] for o in outcomes)
    failed = len(outcomes) - succeeded
    verdict = "FAILURES" if failed else "ALL PASS" if outcomes else "NO TESTS"
    errors = []
    for outcome in outcomes:
        if not outcome["passed"]:
            error = error_detail(
                ToolInputError("TEST_FAILED", outcome["detail"]), outcome["node"]
            )
            error.update(
                test=outcome["test"],
                file=outcome["file"],
                next_step="テスト定義と対象ノードの状態を確認してください。",
            )
            errors.append(error)
    return response(
        tool,
        f"{verdict}: 合計{len(outcomes)}件、PASS {succeeded}件、FAIL {failed}件。",
        data={"verdict": verdict, "outcomes": outcomes, "test_files": files},
        succeeded=succeeded,
        failed=failed,
        errors=errors,
        warnings=list(
            dict.fromkeys(w for o in outcomes for w in o.get("warnings", []))
        ),
        status="skipped" if not outcomes else None,
    )


def _find_wireshark() -> Optional[str]:
    """Wireshark 実行ファイルを OS 非依存に探索する。見つからなければ None。

    PATH 上の ``wireshark``/``Wireshark`` を優先し、無ければ主要 OS の既定
    インストール先を確認する。macOS 専用の絶対パスをハードコードしていた
    旧実装は Windows/Linux クライアントから呼ぶと必ず失敗していたため、
    プラットフォーム別の候補リストに分離した。
    """
    found = shutil.which("wireshark") or shutil.which("Wireshark")
    if found:
        return found
    candidates: list[str] = []
    if sys.platform == "darwin":
        candidates.append("/Applications/Wireshark.app/Contents/MacOS/Wireshark")
    elif sys.platform.startswith("win"):
        candidates += [
            r"C:\Program Files\Wireshark\Wireshark.exe",
            r"C:\Program Files (x86)\Wireshark\Wireshark.exe",
        ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def _stop_capture_process(proc: subprocess.Popen) -> None:
    """キャプチャプロセスを終了し、終了待ちでプロセスを回収する。"""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    else:
        proc.wait()


@mcp.tool()
def trigger_packet_capture(
    remote_host: str, container_name: str, interface_name: str
) -> ToolResponse:
    """リモートホスト上のコンテナIFを tshark でキャプチャし、ローカル Wireshark に流す。

    リモートホストで ``ip netns exec <container> tshark`` をバックグラウンド起動し、
    その pcap ストリームを ssh 経由でローカルの Wireshark にパイプする。

    注意: ``remote_host`` への ssh は、他ツールが使う ``NETMIKO_SSH_CONFIG``
    (mgmt IP 宛の ProxyJump 設定) を経由せず、素の ``ssh <remote_host>``
    （ユーザーの通常の ``~/.ssh/config``）で接続する。踏み台が必要な環境では
    ``~/.ssh/config`` 側に ``remote_host`` 宛のエイリアス/ProxyJump を定義して
    おくこと。``sudo`` は ``CLAB_SUDO`` 設定時のみ非対話（``-n``）で付与する。

    Args:
        remote_host: Containerlab が稼働するリモートホスト（ssh 到達可能名。
            ``~/.ssh/config`` で到達可能なエイリアスを指定できる）。
        container_name: キャプチャ対象コンテナ名（netns 名）。
        interface_name: キャプチャ対象のインターフェース名（例: eth1）。

    Returns:
        共通結果形式。data に起動したコマンドと PID を含む。
    """
    # 入力バリデーション（コマンドインジェクション防止のため厳格に）
    token = re.compile(r"^[A-Za-z0-9_.:@\-]+$")
    for label, value in (
        ("remote_host", remote_host),
        ("container_name", container_name),
        ("interface_name", interface_name),
    ):
        if not value or value.startswith("-") or not token.fullmatch(value):
            return failure(
                "trigger_packet_capture",
                ToolInputError("INVALID_ARGUMENT", f"不正な {label}: {value!r}"),
            )

    wireshark = _find_wireshark()
    if not wireshark:
        return failure(
            "trigger_packet_capture",
            ToolInputError(
                "NOT_FOUND",
                "Wireshark が見つかりません。インストールするか PATH に追加してください",
            ),
        )

    sudo_prefix = "sudo -n " if CLAB_SUDO else ""
    remote_capture = (
        f"{sudo_prefix}ip netns exec {shlex.quote(container_name)} "
        f"tshark -i {shlex.quote(interface_name)} -U -w -"
    )
    # -n: ssh 自身の stdin を継承しない（MCP サーバーの JSON-RPC ストリーム
    #     を誤って消費しないため）。BatchMode によりホストキー/パスワード
    #     プロンプトでもハングしない。ServerAlive* により、ネットワーク瞬断
    #     時に ssh 自身が自律的に終了し、Wireshark 側は EOF でキャプチャを
    #     止める（リモート側の後始末は下記の監視スレッドが担う）。
    ssh_cmd = [
        "ssh",
        "-n",
        *SSH_BATCH_OPTS,
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=4",
        remote_host,
        remote_capture,
    ]

    ssh_proc: Optional[subprocess.Popen] = None
    try:
        ssh_proc = subprocess.Popen(
            ssh_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        ws_proc = subprocess.Popen(
            [wireshark, "-k", "-i", "-"],
            stdin=ssh_proc.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if ssh_proc.stdout:
            ssh_proc.stdout.close()  # SIGPIPE を Wireshark 側へ伝播させる

        capture_proc = ssh_proc

        def _wait_and_cleanup() -> None:
            """Wireshark 終了後、ssh（延いてはリモート tshark）を確実に終了させる。

            ユーザーが Wireshark を閉じても SIGPIPE がリモート tshark まで
            正しく伝播しない場合があるため、ssh がまだ生きていれば明示的に
            terminate/kill する。ssh の切断により sshd がリモートプロセスへ
            SIGHUP を送るため、リモート側の tshark ゾンビ化も防げる。
            """
            try:
                ws_proc.wait()
            finally:
                _stop_capture_process(capture_proc)

        threading.Thread(target=_wait_and_cleanup, daemon=True).start()
    except Exception as exc:  # noqa: BLE001
        if ssh_proc is not None:
            if ssh_proc.stdout:
                ssh_proc.stdout.close()
            _stop_capture_process(ssh_proc)
        return failure("trigger_packet_capture", exc)

    return response(
        "trigger_packet_capture",
        "キャプチャプロセスを起動しました。",
        data={
            "remote_host": remote_host,
            "container": container_name,
            "interface": interface_name,
            "ssh_pid": ssh_proc.pid,
            "wireshark_pid": ws_proc.pid,
            "command": shlex.join(ssh_cmd),
        },
        succeeded=1,
    )


# =============================================================================
# === main ====================================================================
# =============================================================================

if __name__ == "__main__":
    try:
        mcp.run()
    except KeyboardInterrupt:
        print("shutting down clab-hybrid MCP server", file=sys.stderr)
