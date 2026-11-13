#!/usr/bin/env python3
"""Validate OCI credentials and discover ADs, subnets, and a compatible Ubuntu image."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Optional

import oci
import oci_a1_lib as lib

VCN_NAME = "a1-free-vcn"
IGW_NAME = "a1-free-igw"
SUBNET_NAME = "a1-free-public-subnet"
VCN_CIDR = "10.0.0.0/16"
SUBNET_CIDR = "10.0.0.0/24"
DNS_VCN = "a1free"
DNS_SUBNET = "public"


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate OCI API access, list availability domains / subnets / Ubuntu ARM images, "
            "and optionally create a public VCN + subnet."
        )
    )
    parser.add_argument("--config", type=Path, default=lib.DEFAULT_CONFIG)
    parser.add_argument("--settings", type=Path, default=lib.DEFAULT_SETTINGS)
    parser.add_argument(
        "--create-network",
        action="store_true",
        help="If no usable public subnet exists, create a VCN, internet gateway, route and public subnet.",
    )
    parser.add_argument(
        "--write-settings",
        action="store_true",
        help="Write discovered COMPARTMENT_OCID, SUBNET_OCID, AVAILABILITY_DOMAIN and IMAGE_OCID into settings.env.",
    )
    parser.add_argument(
        "--generate-ssh-key",
        action="store_true",
        help=f"Generate {lib.ROOT / 'id_ed25519'} if no SSH public key is available.",
    )
    return parser.parse_args(argv)


def generate_ssh_key(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    private = path if path.suffix != ".pub" else Path(str(path)[:-4]) if str(path).endswith(".pub") else path
    pub = Path(str(private) + ".pub")
    if pub.is_file():
        return pub
    if private.exists():
        raise lib.ConfigError(f"Private key {private} exists but public key {pub} is missing.")
    result = subprocess.run(
        [
            "ssh-keygen",
            "-t",
            "ed25519",
            "-N",
            "",
            "-f",
            str(private),
            "-C",
            "oci-a1-stockholm",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise lib.ConfigError(f"ssh-keygen failed: {result.stderr.strip() or result.stdout.strip()}")
    print(f"Generated SSH key pair: {private}  and  {pub}")
    print("Keep the private key safe. You will use it to SSH into the instance.")
    return pub


def wait_for(client, getter, resource_id: str, state: str = "AVAILABLE", timeout: int = 180):
    response = getter(resource_id)
    return oci.wait_until(client, response, "lifecycle_state", state, max_wait_seconds=timeout).data


def find_by_name(items, name: str):
    for item in items:
        if item.display_name == name and (item.lifecycle_state or "").upper() not in {"TERMINATING", "TERMINATED"}:
            return item
    return None


def create_public_network(network_client, compartment_id: str):
    existing = list(lib.iter_public_subnets(network_client, compartment_id))
    for vcn, subnet in existing:
        if subnet.display_name == SUBNET_NAME and lib.is_public_subnet(subnet):
            print(f"Reusing existing public subnet {subnet.display_name} ({subnet.id})")
            return subnet

    vcns = oci.pagination.list_call_get_all_results(network_client.list_vcns, compartment_id).data
    vcn = find_by_name(vcns, VCN_NAME)
    if vcn is None:
        print(f"Creating VCN {VCN_NAME} ({VCN_CIDR}) …")
        vcn = network_client.create_vcn(
            oci.core.models.CreateVcnDetails(
                cidr_block=VCN_CIDR,
                compartment_id=compartment_id,
                display_name=VCN_NAME,
                dns_label=DNS_VCN,
            )
        ).data
        vcn = wait_for(network_client, network_client.get_vcn, vcn.id)
        print(f"  VCN: {vcn.id}")
    else:
        print(f"Reusing VCN {VCN_NAME} ({vcn.id})")

    gateways = oci.pagination.list_call_get_all_results(
        network_client.list_internet_gateways, compartment_id, vcn_id=vcn.id
    ).data
    igw = find_by_name(gateways, IGW_NAME) or next((g for g in gateways if g.lifecycle_state == "AVAILABLE"), None)
    if igw is None:
        print(f"Creating internet gateway {IGW_NAME} …")
        igw = network_client.create_internet_gateway(
            oci.core.models.CreateInternetGatewayDetails(
                compartment_id=compartment_id,
                vcn_id=vcn.id,
                display_name=IGW_NAME,
                is_enabled=True,
            )
        ).data
        igw = wait_for(network_client, network_client.get_internet_gateway, igw.id)
        print(f"  IGW: {igw.id}")
    else:
        print(f"Reusing internet gateway {igw.display_name} ({igw.id})")

    rt_id = vcn.default_route_table_id
    rt = network_client.get_route_table(rt_id).data
    has_default_route = any(
        (rule.destination == "0.0.0.0/0" and rule.network_entity_id == igw.id) for rule in (rt.route_rules or [])
    )
    if not has_default_route:
        rules = list(rt.route_rules or [])
        rules.append(
            oci.core.models.RouteRule(
                destination="0.0.0.0/0",
                destination_type="CIDR_BLOCK",
                network_entity_id=igw.id,
            )
        )
        print("Adding 0.0.0.0/0 → Internet Gateway route …")
        network_client.update_route_table(
            rt_id, oci.core.models.UpdateRouteTableDetails(route_rules=rules)
        )

    # Ensure SSH is allowed on the default security list.
    sl_id = vcn.default_security_list_id
    sl = network_client.get_security_list(sl_id).data
    ingress = list(sl.ingress_security_rules or [])
    has_ssh = any(
        rule.protocol in {"6", "tcp", "TCP"}
        and getattr(getattr(rule, "tcp_options", None), "destination_port_range", None)
        and rule.tcp_options.destination_port_range.min == 22
        and rule.tcp_options.destination_port_range.max == 22
        for rule in ingress
        if getattr(rule, "tcp_options", None)
    )
    # Also accept a rule with no tcp_options (all TCP) — rare. If no SSH, add it.
    if not has_ssh:
        # Simpler check: any dest port 22
        has_ssh = _has_tcp_port(ingress, 22)
    if not has_ssh:
        print("Adding SSH (TCP/22) ingress rule on the default security list …")
        ingress.append(
            oci.core.models.IngressSecurityRule(
                protocol="6",
                source="0.0.0.0/0",
                source_type="CIDR_BLOCK",
                is_stateless=False,
                tcp_options=oci.core.models.TcpOptions(
                    destination_port_range=oci.core.models.PortRange(min=22, max=22)
                ),
                description="SSH for Ampere A1",
            )
        )
        network_client.update_security_list(
            sl_id,
            oci.core.models.UpdateSecurityListDetails(
                ingress_security_rules=ingress,
                egress_security_rules=sl.egress_security_rules,
            ),
        )

    subnets = oci.pagination.list_call_get_all_results(
        network_client.list_subnets, compartment_id, vcn_id=vcn.id
    ).data
    subnet = find_by_name(subnets, SUBNET_NAME)
    if subnet is None:
        print(f"Creating public subnet {SUBNET_NAME} ({SUBNET_CIDR}) …")
        subnet = network_client.create_subnet(
            oci.core.models.CreateSubnetDetails(
                compartment_id=compartment_id,
                vcn_id=vcn.id,
                cidr_block=SUBNET_CIDR,
                display_name=SUBNET_NAME,
                dns_label=DNS_SUBNET,
                prohibit_public_ip_on_vnic=False,
                route_table_id=rt_id,
                security_list_ids=[sl_id],
            )
        ).data
        subnet = wait_for(network_client, network_client.get_subnet, subnet.id)
        print(f"  Subnet: {subnet.id}")
    else:
        print(f"Reusing subnet {SUBNET_NAME} ({subnet.id})")
    return subnet


def _has_tcp_port(rules, port: int) -> bool:
    for rule in rules:
        if str(rule.protocol) not in {"6"}:
            continue
        opts = getattr(rule, "tcp_options", None)
        if opts is None or opts.destination_port_range is None:
            # No port restriction = all TCP ports.
            return True
        rng = opts.destination_port_range
        if rng.min is not None and rng.max is not None and rng.min <= port <= rng.max:
            return True
    return False


def print_section(title: str) -> None:
    print()
    print(f"== {title} ==")


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    lib.setup_logging(None)
    print(f"Config:   {args.config}")
    print(f"Settings: {args.settings}")

    try:
        settings = lib.load_settings(args.config, args.settings, require_files=True)
    except lib.ConfigError as exc:
        print(f"ERROR: {exc}")
        return 2

    # SSH key
    print_section("SSH key")
    ssh_path = settings.ssh_pubkey_file
    if not ssh_path.is_file():
        if args.generate_ssh_key:
            try:
                ssh_path = generate_ssh_key(lib.ROOT / "id_ed25519")
                settings.ssh_pubkey_file = ssh_path
            except lib.ConfigError as exc:
                print(f"ERROR: {exc}")
                return 2
        else:
            print(f"No SSH public key at {ssh_path}")
            print("Create one with:  ./venv/bin/python check_setup.py --generate-ssh-key")
            ssh_path = None
    if ssh_path and ssh_path.is_file():
        try:
            key = lib.read_ssh_pubkey(ssh_path)
            comment = key.split()[-1] if len(key.split()) >= 3 else ""
            algo = key.split()[0]
            print(f"Using {ssh_path}  ({algo} {comment})".rstrip())
        except lib.ConfigError as exc:
            print(f"ERROR: {exc}")
            return 2

    try:
        config = lib.load_oci_config(settings.config_path)
    except lib.ConfigError as exc:
        print(f"ERROR: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: failed to load OCI config: {exc}")
        return 2

    print_section("API authentication")
    print(f"region:   {config.get('region')}")
    print(f"user:     {config.get('user')}")
    print(f"tenancy:  {config.get('tenancy')}")
    print(f"key_file: {config.get('key_file')}")

    clients = lib.make_clients(config)
    identity = clients["identity"]
    compute = clients["compute"]
    network = clients["network"]

    try:
        tenancy = identity.get_tenancy(config["tenancy"]).data
        print(f"Auth OK. Tenancy name: {tenancy.name}")
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: authentication failed: {lib.format_service_error(exc)}")
        print("Check user OCID, fingerprint, tenancy, region, and that the private key matches the API key.")
        return 2

    try:
        user = identity.get_user(config["user"]).data
        print(f"User: {user.name}  ({user.email or 'no email'})")
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not read the API user: {lib.format_service_error(exc)}")

    compartment_id = settings.compartment_ocid or config["tenancy"]
    print(f"Compartment: {compartment_id}")

    print_section("Availability domains")
    try:
        ads = lib.list_availability_domains(identity, compartment_id)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: listing ADs failed: {lib.format_service_error(exc)}")
        return 2
    if not ads:
        print("No availability domains found.")
        return 2
    for name in ads:
        print(f"  - {name}")

    print_section("VCNs and subnets")
    try:
        pairs = list(lib.iter_public_subnets(network, compartment_id))
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: listing subnets failed: {lib.format_service_error(exc)}")
        return 2

    if not pairs:
        print("No VCNs/subnets in this compartment.")
    else:
        for vcn, subnet in pairs:
            kind = "public" if lib.is_public_subnet(subnet) else "private"
            vcn_name = vcn.display_name if vcn else "?"
            state = subnet.lifecycle_state or ""
            print(
                f"  - [{kind:7}] {subnet.display_name}  {subnet.cidr_block}  "
                f"vcn={vcn_name}  state={state}"
            )
            print(f"             {subnet.id}")

    public_subnets = [s for _, s in pairs if lib.is_public_subnet(s) and (s.lifecycle_state or "") == "AVAILABLE"]
    chosen_subnet = None
    if settings.subnet_ocid:
        chosen_subnet = next((s for _, s in pairs if s.id == settings.subnet_ocid), None)
        if chosen_subnet is None:
            print(f"WARNING: SUBNET_OCID from settings was not found in this compartment: {settings.subnet_ocid}")
        else:
            print(f"Using SUBNET_OCID from settings: {chosen_subnet.id}")
    elif len(public_subnets) == 1:
        chosen_subnet = public_subnets[0]
        print(f"Exactly one public subnet found; it can be used automatically: {chosen_subnet.id}")
    elif len(public_subnets) > 1:
        print(f"{len(public_subnets)} public subnets found. Set SUBNET_OCID in settings.env to pick one.")
    elif args.create_network:
        print("No public subnet found — creating one.")
        try:
            chosen_subnet = create_public_network(network, compartment_id)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: failed to create network: {lib.format_service_error(exc)}")
            return 2
    else:
        print("No public subnet found. Re-run with --create-network to create a VCN + public subnet.")

    print_section(f"Images compatible with {settings.shape}")
    try:
        images = lib.list_compatible_ubuntu_images(compute, compartment_id, settings.shape)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: listing images failed: {lib.format_service_error(exc)}")
        return 2
    if not images:
        print("No Canonical Ubuntu 22.04/24.04 aarch64 images found for this shape.")
        return 2
    for image in images[:8]:
        created = ""
        if image.time_created:
            created = image.time_created.astimezone().strftime("%Y-%m-%d")
        print(f"  - {image.operating_system} {image.operating_system_version}  {image.display_name}  ({created})")
        print(f"    {image.id}")
    if len(images) > 8:
        print(f"  … and {len(images) - 8} older image(s)")
    chosen_image = images[0]
    if settings.image_ocid:
        match = next((img for img in images if img.id == settings.image_ocid), None)
        if match:
            chosen_image = match
            print(f"Using IMAGE_OCID from settings: {chosen_image.id}")
        else:
            print(f"Note: IMAGE_OCID from settings was not in the filtered Ubuntu list; it will still be used as-is.")
            chosen_image_id = settings.image_ocid
            chosen_image = type("Img", (), {"id": chosen_image_id, "display_name": settings.image_ocid})()
    else:
        print(f"Latest suitable image: {chosen_image.display_name}")

    existing = lib.find_existing_instance(compute, compartment_id, settings.display_name)
    print_section("Existing instance")
    if existing:
        ip = lib.get_instance_public_ip(compute, network, existing)
        print(f"Found {settings.display_name}: {existing.id}")
        print(f"  state={existing.lifecycle_state}  public_ip={ip or 'n/a'}")
        print("The launch script will exit successfully without creating a duplicate.")
    else:
        print(f"No existing instance named {settings.display_name!r} in this compartment.")

    print_section("Suggested settings")
    print(f"  COMPARTMENT_OCID={compartment_id}")
    if ads:
        print(f"  AVAILABILITY_DOMAIN={ads[0]}    # leave empty to rotate through all ADs")
    if chosen_subnet is not None:
        print(f"  SUBNET_OCID={chosen_subnet.id}")
    print(f"  IMAGE_OCID={chosen_image.id}")
    print(f"  DISPLAY_NAME={settings.display_name}")
    print(f"  OCPUS={settings.ocpus}")
    print(f"  MEMORY_GB={settings.memory_gb}")
    if ssh_path:
        print(f"  SSH_PUBKEY_FILE={ssh_path}")

    if args.write_settings:
        updates = {
            "COMPARTMENT_OCID": compartment_id,
            "IMAGE_OCID": chosen_image.id,
        }
        if chosen_subnet is not None:
            updates["SUBNET_OCID"] = chosen_subnet.id
        if ssh_path:
            updates["SSH_PUBKEY_FILE"] = str(ssh_path)
        # Leave AVAILABILITY_DOMAIN empty so the launcher can rotate if more than one AD exists.
        if len(ads) == 1 and not settings.availability_domain:
            updates["AVAILABILITY_DOMAIN"] = ads[0]
        elif settings.availability_domain:
            updates["AVAILABILITY_DOMAIN"] = settings.availability_domain
        lib.update_env_file(settings.settings_path, updates)
        print()
        print(f"Updated {settings.settings_path} with the values above.")

    print()
    if chosen_subnet is None:
        print("Setup is incomplete: no subnet yet. Use --create-network --write-settings.")
        return 1
    print("Setup looks good. Start the retry loop with:  ./start.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
