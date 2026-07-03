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
import threading
from datetime import datetime
from typing import Any, Callable, Optional

import yaml
from netmiko.exceptions import NetmikoParsingException

from mcp.server.fastmcp import FastMCP

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
}

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

CLAB_BIN = os.environ.get("CLAB_BIN", "clab")
CLAB_HOST = os.environ.get("CLAB_HOST")
CLAB_SSH_USER = os.environ.get("CLAB_SSH_USER")
CLAB_SUDO = os.environ.get("CLAB_SUDO", "0") in ("1", "true", "yes")
CLAB_API_URL = os.environ.get("CLAB_API_URL")
NORNIR_WORKERS = int(os.environ.get("NORNIR_WORKERS", "20"))
NETMIKO_READ_TIMEOUT = int(os.environ.get("NETMIKO_READ_TIMEOUT", "60"))
NETMIKO_SSH_CONFIG = os.environ.get("NETMIKO_SSH_CONFIG")

DEFAULT_LINUX_DEVICE_TYPE = "linux"

# MCP は stdout を JSON-RPC 専用に使うため、ログは必ず stderr へ出す。
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
logger = logging.getLogger("clab_hybrid")

mcp = FastMCP("clab-hybrid")


# =============================================================================
# === Clab Introspection (subprocess / optional httpx) =========================
# =============================================================================

def _clab_argv(args: list[str]) -> list[str]:
    """clab 呼び出しの argv を組み立てる（ローカル or ssh リモート）。"""
    local = []
    if CLAB_SUDO:
        local.append("sudo")
    local += [CLAB_BIN, *args]

    if not CLAB_HOST:
        return local

    target = f"{CLAB_SSH_USER}@{CLAB_HOST}" if CLAB_SSH_USER else CLAB_HOST
    remote_cmd = " ".join(shlex.quote(part) for part in local)
    return ["ssh", target, remote_cmd]


def _run_clab(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    """clab コマンドを実行し CompletedProcess を返す。失敗時は RuntimeError。"""
    argv = _clab_argv(args)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:  # clab / ssh バイナリが無い
        raise RuntimeError(
            f"実行バイナリが見つかりません: {argv[0]!r} ({exc})"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"clab コマンドがタイムアウトしました: {' '.join(args)}") from exc

    if proc.returncode != 0:
        raise RuntimeError(
            f"clab コマンド失敗 (rc={proc.returncode}): {' '.join(args)}\n"
            f"stderr:\n{proc.stderr.strip()}\nstdout:\n{proc.stdout.strip()}"
        )
    return proc


def _normalize_inspect_json(raw: Any) -> list[dict[str, Any]]:
    """clab のバージョン差を吸収して container リストへ正規化する。"""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        if "containers" in raw and isinstance(raw["containers"], list):
            return raw["containers"]
        # 新しめの clab は {labname: [ ... ]} 形式で返す場合がある
        flattened: list[dict[str, Any]] = []
        for value in raw.values():
            if isinstance(value, list):
                flattened.extend(v for v in value if isinstance(v, dict))
        if flattened:
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
    if CLAB_API_URL:
        containers = _inspect_via_api(lab_name)
    else:
        proc = _run_clab(["inspect", "--name", lab_name, "--format", "json"], timeout=120)
        try:
            containers = _normalize_inspect_json(json.loads(proc.stdout))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"clab inspect の JSON パースに失敗: {exc}") from exc

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
                    c.get("ipv4_address") or c.get("ipv4-address") or c.get("IPv4Address")
                ),
                "image": c.get("image") or c.get("Image"),
                "state": c.get("state") or c.get("State"),
            }
        )
    if not nodes:
        raise RuntimeError(f"ラボ '{lab_name}' の稼働ノードが見つかりませんでした")
    return nodes


def _inspect_via_api(lab_name: str) -> list[dict[str, Any]]:
    """clab-api-server 経由でノード情報を取得する（CLAB_API_URL 設定時のみ）。"""
    import httpx

    assert CLAB_API_URL is not None  # 呼び出し元が CLAB_API_URL 設定時のみ呼ぶ

    url = f"{CLAB_API_URL.rstrip('/')}/api/v1/labs/{lab_name}"
    try:
        resp = httpx.get(url, timeout=60.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"clab-api-server への問い合わせに失敗: {exc}") from exc
    return _normalize_inspect_json(resp.json())


# =============================================================================
# === Topology YAML helpers ===================================================
# =============================================================================

def _load_topo_yaml(topo_path: str) -> dict[str, Any]:
    if not os.path.isfile(topo_path):
        raise RuntimeError(f"トポロジファイルが見つかりません: {topo_path}")
    with open(topo_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise RuntimeError(f"トポロジ YAML の内容が不正です: {topo_path}")
    return data


def _find_topo_for_lab(lab_name: str) -> Optional[str]:
    """カレントディレクトリ配下から lab_name に一致する *.clab.yml を探す。"""
    candidates = glob.glob("**/*.clab.yml", recursive=True) + glob.glob(
        "**/*.clab.yaml", recursive=True
    )
    for path in candidates:
        try:
            data = _load_topo_yaml(path)
        except RuntimeError:
            continue
        if data.get("name") == lab_name:
            return path
    return candidates[0] if candidates else None


def _topo_nodes_and_links(topo: dict[str, Any]) -> tuple[dict[str, Any], list[Any]]:
    topology = topo.get("topology", {}) or {}
    nodes = topology.get("nodes", {}) or {}
    links = topology.get("links", []) or []
    return nodes, links


def _safe_join(base_dir: str, *parts: str) -> str:
    """base_dir 配下に限定してパスを結合する。

    トポロジ YAML の ``startup-config`` フィールドやノード名（YAML の dict
    キーであり、外部入力に由来しうる）が ``../`` や絶対パスで base_dir を
    脱出しようとする場合に備えた防御。脱出を検出すると ValueError を送出する。
    """
    base_abs = os.path.realpath(base_dir or ".")
    target_abs = os.path.realpath(os.path.join(base_abs, *parts))
    if target_abs != base_abs and not target_abs.startswith(base_abs + os.sep):
        raise ValueError(
            f"許可されたディレクトリ外へのパスです: {os.path.join(*parts)!r} (base={base_dir})"
        )
    return target_abs


def _ensure_parent_dir(path: str) -> None:
    """ファイルの親ディレクトリを作成する（存在しなければ）。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)


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
    """正規化ノード dict から Nornir Host を生成する。mgmt_ip 無しは None。"""
    name = node["name"]
    kind = node.get("kind", "linux")
    mgmt_ip = node.get("mgmt_ip")
    if not mgmt_ip:
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


def _run_command_task(
    task: Task, command_or_alias: str, use_textfsm: bool
) -> Result:
    """各ホストでエイリアス解決したコマンドを Netmiko 実行する Nornir タスク。"""
    kind = task.host.data.get("kind", "linux")
    command = resolve_command(command_or_alias, kind)
    try:
        sub = task.run(
            task=netmiko_send_command,
            command_string=command,
            use_textfsm=use_textfsm,
            read_timeout=NETMIKO_READ_TIMEOUT,
        )
        output = sub.result
    except (NetmikoParsingException, ValueError):
        # TextFSM テンプレート不一致等の「解析失敗」のみ生テキストで再試行する。
        # 接続断・認証エラー等はここでは捕捉せず、そのまま呼び出し元へ伝播させる
        # （非冪等コマンドを不必要に再実行しないため）。
        if use_textfsm:
            sub = task.run(
                task=netmiko_send_command,
                command_string=command,
                use_textfsm=False,
                read_timeout=NETMIKO_READ_TIMEOUT,
            )
            output = sub.result
        else:
            raise
    return Result(host=task.host, result={"command": command, "output": output})


def _collect_config_task(task: Task) -> Result:
    """KIND_COMMAND を用いてノードの設定全文を取得する Nornir タスク。"""
    kind = task.host.data.get("kind", "linux")
    spec = KIND_COMMAND.get(kind)
    conn = task.host.get_connection("netmiko", task.nornir.config)

    if not spec:
        # KIND_COMMAND 未定義 kind（linux 等）は取得対象外
        return Result(
            host=task.host,
            result="",
            failed=False,
            changed=False,
            severity_level=logging.WARNING,
        )

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


def _format_results(agg: Any) -> dict[str, dict[str, Any]]:
    """AggregatedResult を LLM 向けの正規化 dict に整形する。"""
    formatted: dict[str, dict[str, Any]] = {}
    for host_name, multi in agg.items():
        top = multi[0]
        if top.failed:
            exc = top.exception
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
    with open(test_file, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if isinstance(data, list):
        return None, data
    lab_name = data.get("lab") or data.get("lab_name")
    cases = data.get("tests") or data.get("cases") or []
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
        match = re.search(r"__RC__=(\d+)", output)
        actual = int(match.group(1)) if match else 0
        ok = actual == expected
        return ok, f"exit_code expected={expected} actual={actual}: {'OK' if ok else 'NG'}"

    return False, "アサーション条件がありません (contains/regex/exit_code)"


def _run_test_case(
    lab_name: str, case: dict[str, Any]
) -> list[dict[str, Any]]:
    """1 テストケースを対象ノード群で並列実行し結果リストを返す。"""
    name = case.get("name", "unnamed")
    command_or_alias = case.get("command") or case.get("alias")
    node_filter = case.get("nodes") or case.get("node")
    assertion = case.get("assert") or {}

    if not command_or_alias:
        return [{"test": name, "node": "-", "passed": False, "detail": "command 未指定"}]

    try:
        nodes = _inspect_nodes(lab_name)
    except RuntimeError as exc:
        return [{"test": name, "node": "-", "passed": False, "detail": str(exc)}]

    # exit_code 判定がある場合は linux コマンドに RC 収集を付与
    wants_exit_code = "exit_code" in assertion

    def _test_task(task: Task) -> Result:
        kind = task.host.data.get("kind", "linux")
        command = resolve_command(command_or_alias, kind)
        if wants_exit_code and kind == "linux":
            command = f"{command}; echo __RC__=$?"
        sub = task.run(
            task=netmiko_send_command,
            command_string=command,
            use_textfsm=False,
            read_timeout=NETMIKO_READ_TIMEOUT,
        )
        return Result(host=task.host, result=sub.result)

    try:
        nr = _build_nornir(nodes, node_filter_regex=node_filter)
    except RuntimeError as exc:
        return [{"test": name, "node": node_filter or "*", "passed": False, "detail": str(exc)}]

    results = _run_nornir(nr, _test_task)

    outcomes: list[dict[str, Any]] = []
    for host_name, res in results.items():
        if res["failed"]:
            outcomes.append(
                {"test": name, "node": host_name, "passed": False, "detail": res["error"]}
            )
            continue
        ok, detail = _evaluate_assertion(str(res["result"]), assertion)
        outcomes.append(
            {"test": name, "node": host_name, "passed": ok, "detail": detail}
        )
    return outcomes


# =============================================================================
# === MCP Tools ===============================================================
# =============================================================================

@mcp.tool()
def deploy_lab(topo_yaml_path: str) -> str:
    """Containerlab トポロジ YAML から新規ラボをデプロイする。

    ``CLAB_HOST`` が設定されていれば ssh 経由でリモートホスト上に、未設定なら
    ローカルで ``clab deploy`` を実行する。``CLAB_API_URL`` 設定時は
    clab-api-server(httpx) 経由でデプロイする。

    Args:
        topo_yaml_path: デプロイする *.clab.yml トポロジファイルのパス。

    Returns:
        clab の実行結果（標準出力）またはエラーサマリ文字列。
    """
    try:
        if CLAB_API_URL:
            import httpx

            topo = _load_topo_yaml(topo_yaml_path)
            url = f"{CLAB_API_URL.rstrip('/')}/api/v1/labs"
            resp = httpx.post(url, json={"topology": topo}, timeout=600.0)
            resp.raise_for_status()
            return f"[deploy_lab] API デプロイ成功:\n{resp.text}"

        if not CLAB_HOST and not os.path.isfile(topo_yaml_path):
            return f"[deploy_lab] エラー: トポロジファイルが見つかりません: {topo_yaml_path}"

        proc = _run_clab(["deploy", "-t", topo_yaml_path], timeout=600)
        return f"[deploy_lab] デプロイ成功:\n{proc.stdout.strip()}"
    except Exception as exc:  # noqa: BLE001
        return f"[deploy_lab] エラー: {exc}"


@mcp.tool()
def inspect_lab_topology(lab_name: str) -> dict[str, Any]:
    """稼働中ラボの全ノード（管理IP・kind）とリンク情報を取得して返す。

    ノード情報は ``clab inspect`` から、リンク情報はカレントディレクトリ配下で
    一致するトポロジ YAML を探索して補完する（best-effort）。

    Args:
        lab_name: 対象ラボ名（clab トポロジの name フィールド）。

    Returns:
        {"lab": ..., "nodes": [...], "links": [...], "warnings": [...]} 形式の dict。
    """
    warnings: list[str] = []
    try:
        nodes = _inspect_nodes(lab_name)
    except Exception as exc:  # noqa: BLE001
        return {"lab": lab_name, "nodes": [], "links": [], "error": str(exc)}

    links: list[Any] = []
    topo_path = _find_topo_for_lab(lab_name)
    if topo_path:
        try:
            topo = _load_topo_yaml(topo_path)
            _, raw_links = _topo_nodes_and_links(topo)
            links = raw_links
        except RuntimeError as exc:
            warnings.append(f"リンク情報の取得に失敗: {exc}")
    else:
        warnings.append("一致するトポロジ YAML が見つからずリンク情報は空です")

    return {
        "lab": lab_name,
        "topo_path": topo_path,
        "nodes": nodes,
        "links": links,
        "warnings": warnings,
    }


@mcp.tool()
def run_parallel_command(
    lab_name: str, command_or_alias: str, node_filter_regex: Optional[str] = None
) -> str:
    """指定ラボの全（または絞り込んだ）ノードに対しコマンドを完全並列実行する。

    ``command_or_alias`` が COMMAND_ALIASES（bgp-summary / ip-route / interfaces）に
    該当する場合、各ノードの kind に応じたコマンドへ自動変換する。エイリアスに
    kind 定義が無い、または完全リテラルの場合はそのまま実行する。Netmiko 経由で
    ``use_textfsm=True`` を適用し、可能なら構造化データを返す。

    Args:
        lab_name: 対象ラボ名。
        command_or_alias: エイリアス名（bgp-summary 等）または実行コマンド文字列。
        node_filter_regex: ノード短名を絞り込む正規表現（省略時は全ノード）。

    Returns:
        ノードごとの実行結果を含む JSON 文字列。
    """
    try:
        nodes = _inspect_nodes(lab_name)
        nr = _build_nornir(nodes, node_filter_regex=node_filter_regex)
        results = _run_nornir(
            nr, _run_command_task, command_or_alias=command_or_alias, use_textfsm=True
        )
        return json.dumps(
            {"lab": lab_name, "command": command_or_alias, "results": results},
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("run_parallel_command failed")
        return f"[run_parallel_command] エラー: {exc}"


@mcp.tool()
def snapshot_and_save_configs(
    lab_name: str,
    mode: str = "snapshot",
    save_dir: str = "save",
    default_startup_dir: str = "startup-configs",
) -> str:
    """稼働中ノードから設定を全台並列回収して保存する。

    ``mode="snapshot"`` の場合、``save/save-<TIMESTAMP>/<node>.conf`` に保存する。
    ``mode="startup"`` の場合、トポロジ定義の startup-config パス（未定義なら
    ``<default_startup_dir>/<node>.conf``）へ直接上書き書き込みする。

    Args:
        lab_name: 対象ラボ名。
        mode: "snapshot"（既定）または "startup"。
        save_dir: スナップショットのルートディレクトリ。
        default_startup_dir: startup-config 未定義ノードの既定保存先。

    Returns:
        保存結果のサマリ文字列。
    """
    if mode not in ("snapshot", "startup"):
        return f"[snapshot_and_save_configs] エラー: 不正な mode '{mode}' (snapshot|startup)"

    try:
        nodes = _inspect_nodes(lab_name)
        nr = _build_nornir(nodes)
        results = _run_nornir(nr, _collect_config_task)
    except Exception as exc:  # noqa: BLE001
        return f"[snapshot_and_save_configs] エラー: {exc}"

    topo = None
    base_dir = "."
    if mode == "startup":
        topo_path = _find_topo_for_lab(lab_name)
        if not topo_path:
            return "[snapshot_and_save_configs] エラー: startup モードにはトポロジ YAML が必要です"
        topo = _load_topo_yaml(topo_path)
        base_dir = os.path.dirname(os.path.abspath(topo_path)) or "."

    saved: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    target_dir = ""
    if mode == "snapshot":
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target_dir = os.path.join(save_dir, f"save-{timestamp}")
        os.makedirs(target_dir, exist_ok=True)

    for host_name, res in results.items():
        if res["failed"]:
            errors.append(f"{host_name}: {res['error']}")
            continue
        config_text = str(res["result"]).strip()
        if not config_text:
            skipped.append(host_name)  # KIND_COMMAND 未定義 kind 等
            continue

        try:
            if mode == "snapshot":
                dest = _safe_join(target_dir, f"{host_name}.conf")
            else:
                assert topo is not None  # mode == "startup" のときのみこの分岐に入る
                dest = _startup_path_for_node(topo, host_name, default_startup_dir, base_dir)
        except ValueError as exc:
            errors.append(f"{host_name}: {exc}")
            continue

        try:
            _ensure_parent_dir(dest)
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(config_text + "\n")
            saved.append(dest)
        except OSError as exc:
            errors.append(f"{host_name}: 書き込み失敗 {exc}")

    summary = [
        f"[snapshot_and_save_configs] mode={mode}",
        f"保存成功: {len(saved)} 件",
    ]
    summary += [f"  - {p}" for p in saved]
    if skipped:
        summary.append(f"スキップ(設定取得対象外): {', '.join(skipped)}")
    if errors:
        summary.append(f"エラー: {len(errors)} 件")
        summary += [f"  - {e}" for e in errors]
    return "\n".join(summary)


@mcp.tool()
def restore_startup_configs(
    topo_path: str, snapshot_name: str = "latest", save_dir: str = "save"
) -> str:
    """保存済みスナップショットから各ノードの startup-config パスへ設定を書き戻す。

    Args:
        topo_path: 対象トポロジ *.clab.yml のパス。
        snapshot_name: 復元元スナップショット名（"latest" で最新を自動選択）。
        save_dir: スナップショットのルートディレクトリ。

    Returns:
        復元結果のサマリ文字列。
    """
    try:
        topo = _load_topo_yaml(topo_path)
    except RuntimeError as exc:
        return f"[restore_startup_configs] エラー: {exc}"

    base_dir = os.path.dirname(os.path.abspath(topo_path)) or "."

    # スナップショットディレクトリを解決（save_dir 配下に限定する）
    if snapshot_name == "latest":
        candidates = sorted(glob.glob(os.path.join(save_dir, "save-*")))
        if not candidates:
            return f"[restore_startup_configs] エラー: スナップショットが見つかりません ({save_dir})"
        snapshot_dir = candidates[-1]
    else:
        try:
            snapshot_dir = _safe_join(save_dir, snapshot_name)
        except ValueError as exc:
            return f"[restore_startup_configs] エラー: {exc}"
        if not os.path.isdir(snapshot_dir):
            return f"[restore_startup_configs] エラー: スナップショットが存在しません: {snapshot_dir}"

    nodes, _ = _topo_nodes_and_links(topo)
    restored: list[str] = []
    missing: list[str] = []
    errors: list[str] = []

    for node_name in nodes:
        try:
            src = _safe_join(snapshot_dir, f"{node_name}.conf")
            dest = _startup_path_for_node(topo, node_name, "startup-configs", base_dir)
        except ValueError as exc:
            errors.append(f"{node_name}: {exc}")
            continue
        if not os.path.isfile(src):
            missing.append(node_name)
            continue
        try:
            _ensure_parent_dir(dest)
            shutil.copyfile(src, dest)
            restored.append(f"{node_name}: {src} -> {dest}")
        except OSError as exc:
            errors.append(f"{node_name}: {exc}")

    summary = [
        f"[restore_startup_configs] スナップショット: {snapshot_dir}",
        f"復元成功: {len(restored)} 件",
    ]
    summary += [f"  - {r}" for r in restored]
    if missing:
        summary.append(f"スナップショットに設定が無いノード: {', '.join(missing)}")
    if errors:
        summary.append(f"エラー: {len(errors)} 件")
        summary += [f"  - {e}" for e in errors]
    return "\n".join(summary)


@mcp.tool()
def run_topology_tests(test_file_or_dir: str) -> str:
    """test.yml を再帰探索し、Nornir 並列実行で PASS/FAIL 判定を行う。

    各テストケースは以下の構造を想定する::

        lab: <lab_name>
        tests:
          - name: "BGP established on r1"
            nodes: "r1"              # ノード短名 or 正規表現
            command: "bgp-summary"   # エイリアス or リテラル
            assert:
              contains: "Established"   # または regex / exit_code

    Args:
        test_file_or_dir: test.yml のパス、またはそれを含むディレクトリ。

    Returns:
        PASS/FAIL サマリと各ケースのログを含むレポート文字列。
    """
    try:
        test_files = _discover_test_files(test_file_or_dir)
    except RuntimeError as exc:
        return f"[run_topology_tests] エラー: {exc}"

    if not test_files:
        return f"[run_topology_tests] test.yml が見つかりませんでした: {test_file_or_dir}"

    all_outcomes: list[dict[str, Any]] = []
    report_lines: list[str] = []

    for test_file in test_files:
        report_lines.append(f"=== {test_file} ===")
        try:
            lab_name, cases = _load_test_cases(test_file)
        except Exception as exc:  # noqa: BLE001
            report_lines.append(f"  ロード失敗: {exc}")
            continue
        if not lab_name:
            report_lines.append("  スキップ: lab / lab_name が未指定です")
            continue

        for case in cases:
            try:
                outcomes = _run_test_case(lab_name, case)
            except Exception as exc:  # noqa: BLE001 - 1ケースの異常でバッチ全体を落とさない
                case_name = case.get("name", "unnamed")
                logger.exception("test case %r raised unexpectedly", case_name)
                outcomes = [
                    {
                        "test": case_name,
                        "node": "-",
                        "passed": False,
                        "detail": f"想定外エラー: {exc}",
                    }
                ]
            all_outcomes.extend(outcomes)
            for o in outcomes:
                status = "PASS" if o["passed"] else "FAIL"
                report_lines.append(
                    f"  [{status}] {o['test']} @ {o['node']} :: {o['detail']}"
                )

    total = len(all_outcomes)
    passed = sum(1 for o in all_outcomes if o["passed"])
    failed = total - passed

    if failed == 0 and total > 0:
        verdict = "ALL PASS ✅"
    elif failed:
        verdict = "FAILURES ❌"
    else:
        verdict = "NO TESTS"

    header = [
        "===== Topology Test Summary =====",
        f"合計: {total}  PASS: {passed}  FAIL: {failed}",
        f"結果: {verdict}",
        "",
    ]
    return "\n".join(header + report_lines)


@mcp.tool()
def trigger_packet_capture(
    remote_host: str, container_name: str, interface_name: str
) -> str:
    """リモートホスト上のコンテナIFを tshark でキャプチャし、ローカル Wireshark に流す。

    リモートホストで ``ip netns exec <container> tshark`` をバックグラウンド起動し、
    その pcap ストリームを ssh 経由でローカル Mac の Wireshark にパイプする。

    Args:
        remote_host: Containerlab が稼働するリモートホスト（ssh 到達可能名）。
        container_name: キャプチャ対象コンテナ名（netns 名）。
        interface_name: キャプチャ対象のインターフェース名（例: eth1）。

    Returns:
        起動したコマンドと PID を含むサマリ文字列。
    """
    # 入力バリデーション（コマンドインジェクション防止のため厳格に）
    token = re.compile(r"^[A-Za-z0-9_.:@\-]+$")
    for label, value in (
        ("remote_host", remote_host),
        ("container_name", container_name),
        ("interface_name", interface_name),
    ):
        if not value or not token.match(value):
            return f"[trigger_packet_capture] エラー: 不正な {label}: {value!r}"

    wireshark = shutil.which("wireshark") or (
        "/Applications/Wireshark.app/Contents/MacOS/Wireshark"
    )

    remote_capture = (
        f"sudo ip netns exec {shlex.quote(container_name)} "
        f"tshark -i {shlex.quote(interface_name)} -U -w -"
    )
    ssh_cmd = ["ssh", remote_host, remote_capture]

    try:
        ssh_proc = subprocess.Popen(
            ssh_cmd,
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
        # fire-and-forget な子プロセスなのでここではブロックせず、終了を
        # バックグラウンドスレッドで待ち受けてゾンビ化を防ぐ。
        threading.Thread(
            target=lambda: (ssh_proc.wait(), ws_proc.wait()),
            daemon=True,
        ).start()
    except FileNotFoundError as exc:
        return f"[trigger_packet_capture] エラー: 実行バイナリが見つかりません: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"[trigger_packet_capture] エラー: {exc}"

    return (
        "[trigger_packet_capture] キャプチャを開始しました\n"
        f"  remote: {remote_host} netns={container_name} if={interface_name}\n"
        f"  ssh pid={ssh_proc.pid}  wireshark pid={ws_proc.pid}\n"
        f"  command: {' '.join(ssh_cmd)} | {wireshark} -k -i -"
    )


# =============================================================================
# === main ====================================================================
# =============================================================================

if __name__ == "__main__":
    try:
        mcp.run()
    except KeyboardInterrupt:
        print("shutting down clab-hybrid MCP server", file=sys.stderr)
