#!/usr/bin/env python3
"""Shared helpers for the Always Free Ampere A1 retry tools."""

from __future__ import annotations

import logging
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "oci_config"
DEFAULT_SETTINGS = ROOT / "settings.env"
DEFAULT_LOG = ROOT / "attempts.log"
SUCCESS_FILE = ROOT / "SUCCESS.txt"
SHAPE_DEFAULT = "VM.Standard.A1.Flex"
PREFERRED_OS = "Canonical Ubuntu"
PREFERRED_VERSIONS = ("24.04", "22.04")
EXISTING_STATES = {
    "PROVISIONING",
    "CREATING_IMAGE",
    "STARTING",
    "RUNNING",
    "STOPPING",
    "STOPPED",
}

log = logging.getLogger("oci_a1")


class ConfigError(Exception):
    """User-facing configuration problem (missing file, bad value, etc.)."""


class TzFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: Optional[str] = None) -> str:
        dt = datetime.fromtimestamp(record.created).astimezone()
        if datefmt:
            return dt.strftime(datefmt)
        tz = dt.tzname() or dt.strftime("%z")
        return f"{dt.strftime('%Y-%m-%d %H:%M:%S')} {tz}"


def setup_logging(log_file: Optional[Path] = DEFAULT_LOG, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(level)
    formatter = TzFormatter("%(asctime)s [%(levelname)s] %(message)s")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


def load_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ConfigError(f"Settings file not found: {path}")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Insert or replace KEY=value lines, preserving comments and order."""
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    else:
        lines = []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        compare = stripped[len("export ") :].strip() if stripped.startswith("export ") else stripped
        if compare and not compare.startswith("#") and "=" in compare:
            key = compare.split("=", 1)[0].strip()
            if key in remaining:
                prefix = "export " if stripped.startswith("export ") else ""
                out.append(f"{prefix}{key}={remaining.pop(key)}\n")
                continue
        out.append(line if line.endswith("\n") else line + "\n")
    if remaining:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        if out and out[-1].strip():
            out.append("\n")
        for key, value in remaining.items():
            out.append(f"{key}={value}\n")
    path.write_text("".join(out), encoding="utf-8")


def _as_int(name: str, value: str, default: int, minimum: int = 1) -> int:
    raw = (value or "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc
    if parsed < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {parsed}")
    return parsed


def find_default_ssh_pubkey() -> Optional[Path]:
    candidates = [
        ROOT / "id_ed25519.pub",
        ROOT / "id_rsa.pub",
        Path.home() / ".ssh" / "id_ed25519.pub",
        Path.home() / ".ssh" / "id_rsa.pub",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def read_ssh_pubkey(path: Path) -> str:
    if not path.is_file():
        raise ConfigError(f"SSH public key not found: {path}")
    text = path.read_text(encoding="utf-8").strip()
    # Take the first non-comment line (ignore cert comments etc.)
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            if not (line.startswith("ssh-") or line.startswith("ecdsa-")):
                raise ConfigError(f"File does not look like an SSH public key: {path}")
            return line
    raise ConfigError(f"No SSH public key found in {path}")


@dataclass
class Settings:
    config_path: Path
    settings_path: Path
    compartment_ocid: str
    subnet_ocid: str
    availability_domain: str
    image_ocid: str
    ocpus: int = 4
    memory_gb: int = 24
    boot_volume_gb: int = 50
    shape: str = SHAPE_DEFAULT
    display_name: str = "a1-stockholm-free"
    ssh_pubkey_file: Path = field(default_factory=lambda: ROOT / "id_ed25519.pub")
    retry_seconds: int = 60
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    region: str = "eu-stockholm-1"
    tenancy: str = ""
    user: str = ""

    def load_ssh_key(self) -> str:
        return read_ssh_pubkey(self.ssh_pubkey_file)


def load_settings(
    config_path: Path = DEFAULT_CONFIG,
    settings_path: Path = DEFAULT_SETTINGS,
    require_files: bool = True,
) -> Settings:
    if require_files and not config_path.is_file():
        raise ConfigError(
            f"OCI config not found: {config_path}\n"
            f"Copy {ROOT / 'oci_config.example'} to {config_path} and fill in your API key details."
        )
    env = {}
    if settings_path.is_file():
        env = load_env_file(settings_path)
    elif require_files:
        raise ConfigError(
            f"Settings file not found: {settings_path}\n"
            f"Copy {ROOT / 'settings.env.example'} to {settings_path} and fill in the OCIDs."
        )

    tenancy = ""
    user = ""
    region = env.get("REGION", "eu-stockholm-1").strip() or "eu-stockholm-1"
    if config_path.is_file():
        parsed = parse_oci_config(config_path)
        tenancy = parsed.get("tenancy", "")
        user = parsed.get("user", "")
        region = parsed.get("region", region) or region

    ssh_raw = (env.get("SSH_PUBKEY_FILE") or "").strip()
    if ssh_raw:
        ssh_path = Path(ssh_raw).expanduser()
    else:
        found = find_default_ssh_pubkey()
        ssh_path = found if found else ROOT / "id_ed25519.pub"

    compartment = (env.get("COMPARTMENT_OCID") or "").strip() or tenancy
    return Settings(
        config_path=config_path,
        settings_path=settings_path,
        compartment_ocid=compartment,
        subnet_ocid=(env.get("SUBNET_OCID") or "").strip(),
        availability_domain=(env.get("AVAILABILITY_DOMAIN") or "").strip(),
        image_ocid=(env.get("IMAGE_OCID") or "").strip(),
        ocpus=_as_int("OCPUS", env.get("OCPUS", ""), 4),
        memory_gb=_as_int("MEMORY_GB", env.get("MEMORY_GB", ""), 24),
        boot_volume_gb=_as_int("BOOT_VOLUME_GB", env.get("BOOT_VOLUME_GB", ""), 50),
        shape=(env.get("SHAPE") or "").strip() or SHAPE_DEFAULT,
        display_name=(env.get("DISPLAY_NAME") or "").strip() or "a1-stockholm-free",
        ssh_pubkey_file=ssh_path,
        retry_seconds=_as_int("RETRY_SECONDS", env.get("RETRY_SECONDS", ""), 60),
        telegram_bot_token=(env.get("TELEGRAM_BOT_TOKEN") or "").strip(),
        telegram_chat_id=(env.get("TELEGRAM_CHAT_ID") or "").strip(),
        region=region,
        tenancy=tenancy,
        user=user,
    )


def parse_oci_config(path: Path, profile: str = "DEFAULT") -> dict[str, str]:
    if not path.is_file():
        raise ConfigError(f"OCI config not found: {path}")
    current = None
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            continue
        if current != profile or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def load_oci_config(path: Path, profile: str = "DEFAULT") -> dict[str, Any]:
    import oci

    if not path.is_file():
        raise ConfigError(f"OCI config not found: {path}")
    parsed = parse_oci_config(path, profile)
    for required in ("user", "fingerprint", "tenancy", "region", "key_file"):
        if not parsed.get(required):
            raise ConfigError(f"oci_config is missing required key {required!r}")
    key_file = Path(parsed["key_file"]).expanduser()
    if not key_file.is_absolute():
        key_file = (path.parent / key_file).resolve()
        parsed["key_file"] = str(key_file)
    if not key_file.is_file():
        raise ConfigError(f"API private key not found: {key_file}")
    mode = stat.S_IMODE(key_file.stat().st_mode)
    if mode & 0o077:
        log.warning(
            "API private key %s is group/world-readable (mode %03o). Run: chmod 600 %s",
            key_file,
            mode,
            key_file,
        )
    # Rewrite key_file to absolute so the SDK can find it regardless of cwd.
    text = path.read_text(encoding="utf-8")
    # Use from_file against a temporary in-memory approach: set key_file via signer.
    config = oci.config.from_file(str(path), profile)
    config["key_file"] = str(key_file)
    oci.config.validate_config(config)
    return config


def make_clients(config: dict[str, Any]) -> dict[str, Any]:
    import oci

    return {
        "identity": oci.identity.IdentityClient(config),
        "compute": oci.core.ComputeClient(config),
        "network": oci.core.VirtualNetworkClient(config),
    }


def classify_error(exc: BaseException) -> str:
    """
    Return one of: capacity, throttle, network, fatal_limit, fatal_auth, fatal_config, fatal_other.
    """
    try:
        import oci
        from oci.exceptions import (
            ClientError,
            ConnectTimeout,
            RequestException,
            ServiceError,
        )
    except ImportError:
        oci = None  # type: ignore
        ServiceError = tuple()  # type: ignore
        ConnectTimeout = tuple()  # type: ignore
        RequestException = tuple()  # type: ignore
        ClientError = tuple()  # type: ignore

    if oci is not None and isinstance(exc, ServiceError):
        status = int(getattr(exc, "status", 0) or 0)
        code = (getattr(exc, "code", None) or "").strip()
        message = (getattr(exc, "message", None) or str(exc) or "").strip()
        lower = message.lower()
        code_l = code.lower()

        if status == 429 or code_l in {"toomanyrequests", "throttling", "throttle"}:
            return "throttle"
        if code_l in {"limitexceeded", "quotaexceeded"} or "limit exceeded" in lower:
            return "fatal_limit"
        if status in {401, 403} or code_l in {
            "notauthenticated",
            "notauthorizedornotfound",
            "signinfailed",
        }:
            return "fatal_auth"
        if status == 400 and code_l in {
            "invalidparameter",
            "missingparameter",
            "invalidparameterformat",
            "invalidshape",
            "invalidimage",
        }:
            return "fatal_config"
        if (
            "out of host capacity" in lower
            or "out of capacity" in lower
            or "insufficient host capacity" in lower
            or code_l in {"outofhostcapacity", "internalerror"}
            or status in {500, 502, 503, 504}
        ):
            return "capacity"
        return "fatal_other"

    network_types: tuple[type, ...] = (
        ConnectionError,
        TimeoutError,
        OSError,
    )
    if oci is not None:
        network_types = network_types + (ConnectTimeout, RequestException)
    try:
        import requests

        network_types = network_types + (
            requests.exceptions.RequestException,
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        )
    except Exception:
        pass

    if oci is not None and isinstance(exc, ClientError):
        return "fatal_config"
    if isinstance(exc, network_types):
        return "network"
    return "fatal_other"


def format_service_error(exc: BaseException) -> str:
    status = getattr(exc, "status", None)
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None) or str(exc)
    parts = []
    if status is not None:
        parts.append(f"status={status}")
    if code:
        parts.append(f"code={code}")
    parts.append(message.strip())
    request_id = getattr(exc, "request_id", None) or getattr(exc, "opc_request_id", None)
    if request_id:
        parts.append(f"opc-request-id={request_id}")
    return " ".join(parts)


def list_availability_domains(identity_client: Any, compartment_id: str) -> list[str]:
    import oci

    ads = oci.pagination.list_call_get_all_results(
        identity_client.list_availability_domains, compartment_id
    ).data
    return [ad.name for ad in ads]


def list_compatible_ubuntu_images(
    compute_client: Any, compartment_id: str, shape: str = SHAPE_DEFAULT
) -> list[Any]:
    import oci

    images = oci.pagination.list_call_get_all_results(
        compute_client.list_images,
        compartment_id,
        operating_system=PREFERRED_OS,
        shape=shape,
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    ranked: list[tuple[int, Any]] = []
    for image in images:
        version = (getattr(image, "operating_system_version", None) or "")
        display = (getattr(image, "display_name", None) or "")
        # Prefer 24.04, then 22.04; skip other Ubuntu versions.
        rank = None
        for index, prefix in enumerate(PREFERRED_VERSIONS):
            if version.startswith(prefix) or f"-{prefix}" in display:
                rank = index
                break
        if rank is None:
            continue
        # Prefer aarch64 / ARM images when the name mentions architecture.
        name_l = display.lower()
        if "x86_64" in name_l or "amd64" in name_l:
            continue
        ranked.append((rank, image))
    ranked.sort(key=lambda item: (item[0], -(item[1].time_created.timestamp() if item[1].time_created else 0)))
    return [image for _, image in ranked]


def pick_latest_ubuntu_image(
    compute_client: Any, compartment_id: str, shape: str = SHAPE_DEFAULT
) -> Any:
    images = list_compatible_ubuntu_images(compute_client, compartment_id, shape)
    if not images:
        raise ConfigError(
            f"No Canonical Ubuntu 22.04/24.04 aarch64 image found that is compatible with {shape}."
        )
    return images[0]


def find_existing_instance(
    compute_client: Any, compartment_id: str, display_name: str
) -> Optional[Any]:
    import oci

    instances = oci.pagination.list_call_get_all_results(
        compute_client.list_instances,
        compartment_id,
        display_name=display_name,
    ).data
    for inst in instances:
        state = (inst.lifecycle_state or "").upper()
        if state in EXISTING_STATES:
            return inst
    return None


def get_instance_public_ip(
    compute_client: Any, network_client: Any, instance: Any
) -> Optional[str]:
    import oci

    attachments = oci.pagination.list_call_get_all_results(
        compute_client.list_vnic_attachments,
        instance.compartment_id,
        instance_id=instance.id,
    ).data
    for attachment in attachments:
        if not attachment.vnic_id:
            continue
        vnic = network_client.get_vnic(attachment.vnic_id).data
        if vnic.public_ip:
            return vnic.public_ip
    return None


def write_success(instance_id: str, public_ip: Optional[str], display_name: str) -> None:
    ip_line = public_ip or "(no public IP yet — check the subnet / VNIC in the console)"
    now = datetime.now().astimezone()
    tz = now.tzname() or now.strftime("%z")
    text = (
        f"display_name={display_name}\n"
        f"instance_ocid={instance_id}\n"
        f"public_ip={ip_line}\n"
        f"created_at={now.strftime('%Y-%m-%d %H:%M:%S')} {tz}\n"
    )
    SUCCESS_FILE.write_text(text, encoding="utf-8")


def notify_telegram(settings: Settings, text: str) -> None:
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not token or not chat_id:
        return
    import json
    import urllib.error
    import urllib.request

    try:
        payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as response:
            if response.status >= 300:
                body = response.read(200).decode("utf-8", errors="replace")
                log.warning("Telegram notify failed: HTTP %s %s", response.status, body)
    except urllib.error.HTTPError as exc:
        body = exc.read(200).decode("utf-8", errors="replace") if exc.fp else ""
        log.warning("Telegram notify failed: HTTP %s %s", exc.code, body)
    except Exception as exc:  # noqa: BLE001
        log.warning("Telegram notify failed: %s", exc)


def launch_details_dict(settings: Settings, availability_domain: str, image_ocid: str, ssh_key: str) -> dict[str, Any]:
    return {
        "availability_domain": availability_domain,
        "compartment_id": settings.compartment_ocid,
        "display_name": settings.display_name,
        "shape": settings.shape,
        "shape_config": {
            "ocpus": settings.ocpus,
            "memory_in_gbs": settings.memory_gb,
        },
        "source_details": {
            "source_type": "image",
            "image_id": image_ocid,
            "boot_volume_size_in_gbs": settings.boot_volume_gb,
        },
        "create_vnic_details": {
            "subnet_id": settings.subnet_ocid,
            "assign_public_ip": True,
        },
        "metadata": {
            "ssh_authorized_keys": ssh_key,
        },
    }


def build_launch_details(settings: Settings, availability_domain: str, image_ocid: str, ssh_key: str):
    import oci

    return oci.core.models.LaunchInstanceDetails(
        availability_domain=availability_domain,
        compartment_id=settings.compartment_ocid,
        display_name=settings.display_name,
        shape=settings.shape,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=float(settings.ocpus),
            memory_in_gbs=float(settings.memory_gb),
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=image_ocid,
            boot_volume_size_in_gbs=settings.boot_volume_gb,
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=settings.subnet_ocid,
            assign_public_ip=True,
        ),
        metadata={"ssh_authorized_keys": ssh_key},
    )


_OCID_RE = re.compile(r"^ocid1\.[a-z0-9_-]+\.")


def looks_like_ocid(value: str) -> bool:
    return bool(value and _OCID_RE.match(value))


def iter_public_subnets(network_client: Any, compartment_id: str) -> Iterable[tuple[Any, Any]]:
    """Yield (vcn, subnet) pairs in the compartment."""
    import oci

    vcns = oci.pagination.list_call_get_all_results(
        network_client.list_vcns, compartment_id
    ).data
    vcn_by_id = {vcn.id: vcn for vcn in vcns}
    subnets = oci.pagination.list_call_get_all_results(
        network_client.list_subnets, compartment_id
    ).data
    for subnet in subnets:
        yield vcn_by_id.get(subnet.vcn_id), subnet


def is_public_subnet(subnet: Any) -> bool:
    # prohibit_public_ip_on_vnic True means private subnet.
    return not bool(getattr(subnet, "prohibit_public_ip_on_vnic", False))
