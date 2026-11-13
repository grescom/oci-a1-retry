#!/usr/bin/env python3
"""Retry loop: create an Always Free Ampere A1 instance when capacity appears."""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import oci_a1_lib as lib

STOP_REQUESTED = False
DEADLINE: Optional[float] = None  # time.monotonic() value after which the loop exits cleanly
RESULT_FILE: Optional[Path] = None
MAX_THROTTLE_SECONDS = 600
SUCCESS_WAIT_SECONDS = 900


def _handle_stop(signum, _frame) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    lib.log.info("Received signal %s, will exit after the current attempt.", signum)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Keep trying to launch an Oracle Cloud Always Free Ampere A1 instance "
            "(VM.Standard.A1.Flex) until capacity is available."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=lib.DEFAULT_CONFIG,
        help=f"OCI config file (default: {lib.DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=lib.DEFAULT_SETTINGS,
        help=f"Settings env file (default: {lib.DEFAULT_SETTINGS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the launch request and exit without calling the OCI API.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Try each availability domain once, then exit (do not keep retrying).",
    )
    parser.add_argument(
        "--max-runtime-minutes",
        type=float,
        default=0,
        help="Exit cleanly (status=timeout) after this many minutes. 0 = run forever.",
    )
    parser.add_argument(
        "--result-file",
        type=Path,
        default=None,
        help="Write a JSON result (status=success|fatal|timeout|error, details) to this path.",
    )
    return parser.parse_args(argv)


def time_up() -> bool:
    return DEADLINE is not None and time.monotonic() >= DEADLINE


def write_result(status: str, **fields) -> None:
    if RESULT_FILE is None:
        return
    data = {"status": status, **fields}
    RESULT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def jittered_sleep(base_seconds: float, kind: str) -> float:
    # ±15% jitter, at least 1 second.
    jitter = base_seconds * 0.15 * random.uniform(-1, 1)
    delay = max(1.0, base_seconds + jitter)
    lib.log.info("Sleeping %.1f s before next %s retry.", delay, kind)
    deadline = time.monotonic() + delay
    while time.monotonic() < deadline and not STOP_REQUESTED and not time_up():
        time.sleep(max(0.0, min(1.0, deadline - time.monotonic())))
    return delay


def resolve_ads(settings: lib.Settings, clients: Optional[dict] = None) -> list[str]:
    if settings.availability_domain:
        return [settings.availability_domain]
    if clients is None:
        return []
    ads = lib.list_availability_domains(clients["identity"], settings.compartment_ocid)
    if not ads:
        raise lib.ConfigError("No availability domains returned for this compartment/region.")
    lib.log.info("Auto-detected availability domains: %s", ", ".join(ads))
    return ads


def resolve_image(settings: lib.Settings, clients: Optional[dict] = None) -> str:
    if settings.image_ocid:
        return settings.image_ocid
    if clients is None:
        return ""
    image = lib.pick_latest_ubuntu_image(
        clients["compute"], settings.compartment_ocid, settings.shape
    )
    lib.log.info(
        "Auto-picked image %s (%s, %s)",
        image.id,
        image.display_name,
        image.operating_system_version,
    )
    return image.id


def maybe_exit_if_exists(settings: lib.Settings, clients: dict) -> bool:
    inst = lib.find_existing_instance(
        clients["compute"], settings.compartment_ocid, settings.display_name
    )
    if inst is None:
        return False
    public_ip = lib.get_instance_public_ip(clients["compute"], clients["network"], inst)
    lib.write_success(inst.id, public_ip, settings.display_name)
    write_result(
        "success",
        instance_ocid=inst.id,
        public_ip=public_ip or "",
        state=inst.lifecycle_state,
        display_name=settings.display_name,
        note="already existed",
    )
    lib.log.info(
        "Instance %r already exists: %s (state=%s, public_ip=%s). Exiting successfully.",
        settings.display_name,
        inst.id,
        inst.lifecycle_state,
        public_ip or "n/a",
    )
    lib.notify_telegram(
        settings,
        f"OCI A1: instance {settings.display_name} already exists.\n"
        f"OCID: {inst.id}\n"
        f"State: {inst.lifecycle_state}\n"
        f"Public IP: {public_ip or 'n/a'}",
    )
    return True


def wait_for_running_and_ip(clients: dict, instance_id: str) -> tuple[object, Optional[str]]:
    compute = clients["compute"]
    network = clients["network"]
    deadline = time.monotonic() + SUCCESS_WAIT_SECONDS
    last_state = ""
    inst = None
    while time.monotonic() < deadline and not STOP_REQUESTED:
        inst = compute.get_instance(instance_id).data
        last_state = inst.lifecycle_state or ""
        if last_state == "RUNNING":
            public_ip = lib.get_instance_public_ip(compute, network, inst)
            if public_ip:
                return inst, public_ip
            lib.log.info("Instance is RUNNING, waiting for public IP…")
        elif last_state in {"TERMINATING", "TERMINATED"}:
            raise RuntimeError(f"Instance entered {last_state} while waiting to come up.")
        else:
            lib.log.info("Waiting for instance to become RUNNING (current: %s).", last_state)
        time.sleep(10)
    public_ip = lib.get_instance_public_ip(compute, network, inst) if inst else None
    lib.log.warning(
        "Timed out waiting for RUNNING/public IP (last state=%s, ip=%s).",
        last_state or "unknown",
        public_ip or "n/a",
    )
    return inst, public_ip


def dry_run(settings: lib.Settings) -> int:
    missing = []
    ssh_key = ""
    try:
        ssh_key = settings.load_ssh_key()
    except lib.ConfigError as exc:
        missing.append(str(exc))
        ssh_key = "<SSH public key would be read from " f"{settings.ssh_pubkey_file}>"

    ads = [settings.availability_domain] if settings.availability_domain else [
        "<auto-detect all ADs in the region and rotate>"
    ]
    image_ocid = settings.image_ocid or "<auto-pick latest Canonical Ubuntu 22.04/24.04 aarch64>"
    if not settings.subnet_ocid:
        missing.append("SUBNET_OCID is empty — run check_setup.py (optionally --create-network).")
    if not settings.compartment_ocid:
        missing.append(
            "COMPARTMENT_OCID is empty and tenancy was not read from oci_config."
        )

    payload = lib.launch_details_dict(settings, ads[0], image_ocid, ssh_key)
    print("Dry run — launch request that would be sent to Compute.LaunchInstance:")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print()
    print("Resolved settings:")
    print(f"  region:              {settings.region}")
    print(f"  tenancy:             {settings.tenancy or '(not loaded)'}")
    print(f"  compartment:         {settings.compartment_ocid or '(missing)'}")
    print(f"  display_name:        {settings.display_name}")
    print(f"  shape:               {settings.shape}  ({settings.ocpus} OCPU, {settings.memory_gb} GB)")
    print(f"  boot_volume_gb:      {settings.boot_volume_gb}")
    print(f"  availability_domain: {', '.join(ads)}")
    print(f"  image_ocid:          {image_ocid}")
    print(f"  subnet_ocid:         {settings.subnet_ocid or '(missing — required to launch)'}")
    print(f"  ssh_pubkey_file:     {settings.ssh_pubkey_file}")
    print(f"  retry_seconds:       {settings.retry_seconds}")
    print(f"  telegram:            {'configured' if settings.telegram_bot_token and settings.telegram_chat_id else 'disabled'}")
    print(f"  oci_config:          {settings.config_path}")
    print(f"  settings:            {settings.settings_path}")
    if missing:
        print()
        print("Notes (not sent — this is a dry run):")
        for item in missing:
            print(f"  - {item}")
        return 0
    return 0


def launch_loop(settings: lib.Settings, once: bool = False) -> int:
    if not settings.subnet_ocid:
        raise lib.ConfigError(
            "SUBNET_OCID is empty. Run:  ./venv/bin/python check_setup.py --create-network --write-settings"
        )
    if not settings.compartment_ocid:
        raise lib.ConfigError("COMPARTMENT_OCID is empty and tenancy was not found in oci_config.")
    ssh_key = settings.load_ssh_key()

    config = lib.load_oci_config(settings.config_path)
    clients = lib.make_clients(config)
    if maybe_exit_if_exists(settings, clients):
        return 0

    ads = resolve_ads(settings, clients)
    image_ocid = resolve_image(settings, clients)

    lib.log.info(
        "Starting retry loop: shape=%s (%s OCPU / %s GB), display_name=%r, region=%s, "
        "retry=%ss, ads=%s",
        settings.shape,
        settings.ocpus,
        settings.memory_gb,
        settings.display_name,
        settings.region,
        settings.retry_seconds,
        ", ".join(ads),
    )
    lib.notify_telegram(
        settings,
        f"OCI A1 retry started for {settings.display_name} in {settings.region} "
        f"({settings.ocpus} OCPU / {settings.memory_gb} GB).",
    )

    attempt = 0
    throttle_delay = float(settings.retry_seconds)
    compute = clients["compute"]

    while not STOP_REQUESTED:
        if time_up():
            lib.log.info("Max runtime reached after %s attempts. Exiting cleanly.", attempt)
            write_result("timeout", attempts=attempt)
            return 0
        if maybe_exit_if_exists(settings, clients):
            return 0

        cycle_kind = "capacity"
        tried_this_cycle = 0
        for ad in ads:
            if STOP_REQUESTED or time_up():
                break
            attempt += 1
            tried_this_cycle += 1
            lib.log.info("Attempt #%s in AD %s …", attempt, ad)
            details = lib.build_launch_details(settings, ad, image_ocid, ssh_key)
            try:
                response = compute.launch_instance(details)
                inst = response.data
            except Exception as exc:  # noqa: BLE001
                kind = lib.classify_error(exc)
                detail = lib.format_service_error(exc)
                if kind == "capacity":
                    lib.log.warning("Out of capacity (or transient 5xx) in AD %s: %s", ad, detail)
                    cycle_kind = "capacity"
                    continue
                if kind == "throttle":
                    throttle_delay = min(max(throttle_delay * 2, 30.0), MAX_THROTTLE_SECONDS)
                    lib.log.warning("Rate limited: %s", detail)
                    jittered_sleep(throttle_delay, "throttle")
                    cycle_kind = "throttle"
                    break
                if kind == "network":
                    lib.log.warning("Network error: %s", detail)
                    cycle_kind = "network"
                    break
                if kind == "fatal_limit":
                    lib.log.error(
                        "Limit exceeded — stopping so we do not keep failing. %s "
                        "(check Ampere A1 free-tier quota: 4 OCPU / 24 GB total, and unused instances).",
                        detail,
                    )
                    lib.notify_telegram(settings, f"OCI A1 stopped: LimitExceeded.\n{detail}")
                    write_result("fatal", kind=kind, error=detail)
                    return 2
                if kind == "fatal_auth":
                    lib.log.error("Authentication / authorization error — stopping. %s", detail)
                    lib.notify_telegram(settings, f"OCI A1 stopped: auth error.\n{detail}")
                    write_result("fatal", kind=kind, error=detail)
                    return 2
                if kind == "fatal_config":
                    lib.log.error("Configuration / parameter error — stopping. %s", detail)
                    lib.notify_telegram(settings, f"OCI A1 stopped: bad request.\n{detail}")
                    write_result("fatal", kind=kind, error=detail)
                    return 2
                lib.log.error("Unrecoverable error — stopping. %s", detail)
                lib.notify_telegram(settings, f"OCI A1 stopped: unexpected error.\n{detail}")
                write_result("error", kind=kind, error=detail)
                return 2

            lib.log.info("Launch accepted: %s (state=%s).", inst.id, inst.lifecycle_state)
            try:
                inst, public_ip = wait_for_running_and_ip(clients, inst.id)
            except Exception as exc:  # noqa: BLE001
                lib.log.warning("Waiting for instance failed: %s", exc)
                public_ip = None
            inst_id = inst.id if inst is not None else response.data.id
            lib.write_success(inst_id, public_ip, settings.display_name)
            write_result(
                "success",
                instance_ocid=inst_id,
                public_ip=public_ip or "",
                state=getattr(inst, "lifecycle_state", ""),
                display_name=settings.display_name,
            )
            lib.log.info("SUCCESS. Instance %s, public IP %s. Wrote %s", inst_id, public_ip or "n/a", lib.SUCCESS_FILE)
            lib.notify_telegram(
                settings,
                f"OCI A1 instance is ready.\n"
                f"Name: {settings.display_name}\n"
                f"OCID: {inst_id}\n"
                f"Public IP: {public_ip or 'n/a'}",
            )
            return 0

        if once:
            lib.log.info("--once set and no instance was created this cycle. Exiting.")
            return 1
        if STOP_REQUESTED:
            break
        if time_up():
            continue
        if cycle_kind == "throttle":
            continue
        delay = float(settings.retry_seconds)
        if cycle_kind == "network":
            delay = max(delay, 30.0)
        else:
            # Successful contact that was only capacity: reset throttle backoff.
            throttle_delay = float(settings.retry_seconds)
        jittered_sleep(delay, cycle_kind)

    lib.log.info("Stopped. Attempts made: %s.", attempt)
    write_result("timeout", attempts=attempt, note="stopped by signal")
    return 1


def main(argv: Optional[list[str]] = None) -> int:
    global DEADLINE, RESULT_FILE
    args = parse_args(argv)
    if args.max_runtime_minutes and args.max_runtime_minutes > 0:
        DEADLINE = time.monotonic() + args.max_runtime_minutes * 60
    RESULT_FILE = args.result_file
    lib.setup_logging(None if args.dry_run else lib.DEFAULT_LOG)
    try:
        settings = lib.load_settings(
            config_path=args.config,
            settings_path=args.settings,
            require_files=not args.dry_run,
        )
        # Dry-run is useful even when oci_config / ssh key are not in place yet.
        if args.dry_run:
            if not args.settings.is_file() and not args.config.is_file():
                # Still allow a dry-run from defaults / empty settings.
                settings = lib.load_settings(
                    config_path=args.config if args.config.is_file() else args.config,
                    settings_path=args.settings if args.settings.is_file() else args.settings,
                    require_files=False,
                )
            return dry_run(settings)
        signal.signal(signal.SIGTERM, _handle_stop)
        signal.signal(signal.SIGINT, _handle_stop)
        return launch_loop(settings, once=args.once)
    except lib.ConfigError as exc:
        lib.log.error("%s", exc)
        write_result("fatal", kind="config", error=str(exc))
        return 2
    except Exception as exc:  # noqa: BLE001
        lib.log.exception("Unexpected failure: %s", exc)
        kind = lib.classify_error(exc)
        status = "fatal" if kind in {"fatal_auth", "fatal_limit", "fatal_config"} else "error"
        write_result(status, kind=kind, error=lib.format_service_error(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
