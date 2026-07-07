#!/usr/bin/env python3
"""
docker_backup.py - Python re-implementation of docker-backup (originally Go)

Creates backups of Docker containers: metadata (json) + a copy of all
volume mounts, or optionally a single .tar file. Can also restore a
backup.

Requires: python3 and the `docker` CLI in PATH. Talks to the Docker CLI
via subprocess -- no extra pip dependency needed.

Usage:
    ./docker_backup.py backup <container-id>
    ./docker_backup.py backup --all [--stopped]
    ./docker_backup.py backup <container-id> --tar
    ./docker_backup.py restore <backup>.json [--start]
    ./docker_backup.py restore <backup>.tar [--start]
"""

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd, capture=True, check=True):
    """Runs a command and returns stdout (or raises a RuntimeError)."""
    result = subprocess.run(cmd, capture_output=capture, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{result.stderr.strip()}")
    return result.stdout.strip() if capture else None


def docker_inspect(ref):
    """Returns the full `docker inspect` output as a dict."""
    out = run(["docker", "inspect", ref])
    data = json.loads(out)
    if not data:
        raise RuntimeError(f"Could not find container/image '{ref}'.")
    return data[0]


def sanitize(name):
    """Makes a string safe to use as a file/directory name."""
    name = name.strip("/")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def mount_folder_name(mount):
    """Subdirectory name for a mount: Docker volume name if available,
    otherwise the sanitized destination path (bind mount)."""
    if mount.get("Name"):
        return sanitize(mount["Name"])
    return sanitize(mount["Destination"])


def get_full_image_name(image):
    """Tries to find a full image:tag if no tag was specified."""
    if ":" in image:
        return image
    try:
        info = docker_inspect(image)
        tags = info.get("RepoTags") or []
        if tags:
            return tags[0]
    except RuntimeError:
        pass
    return image


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def backup_root_for(container_name, output):
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    return Path(output) / timestamp / sanitize(container_name)


def do_backup(container_id, args):
    info = docker_inspect(container_id)
    name = info["Name"]
    image = get_full_image_name(info["Config"]["Image"])
    mounts = info.get("Mounts", [])

    print(f"Creating backup of {name.lstrip('/')} ({image}, {container_id[:12]})")

    filename = sanitize(f"{image}-{container_id}")
    backup_root = backup_root_for(name, args.output)

    if args.tar:
        return backup_tar(info, filename, backup_root, args)

    backup_root.mkdir(parents=True, exist_ok=True)

    json_path = backup_root / f"{filename}.backup.json"
    json_path.write_text(json.dumps(info, indent=2))

    collected_paths = []
    for m in mounts:
        src = m["Source"]
        if args.verbose:
            print(f"Mount ({m.get('Type')}) {src} -> {m['Destination']}")

        # Some containers bind-mount special files instead of directories,
        # e.g. /var/run/docker.sock (common for Portainer, Watchtower, Diun,
        # Traefik, autoheal, etc.). shutil.copytree() requires a directory,
        # so skip anything that isn't one instead of crashing the whole run.
        if not os.path.isdir(src):
            print(f"Warning: mount '{src}' is not a directory (socket/device?), skipping it.")
            continue

        for root, _dirs, files in os.walk(src):
            for f in files:
                collected_paths.append(os.path.join(root, f))
        dest_dir = backup_root / mount_folder_name(m)
        shutil.copytree(src, dest_dir, dirs_exist_ok=True)

    files_path = backup_root / f"{filename}.backup.files"
    with open(files_path, "w") as fl:
        fl.write(str(json_path.resolve()) + "\n")
        for p in collected_paths:
            fl.write(p + "\n")

    print(f"Created backup: {json_path}")

    if args.launch:
        cmd = args.launch.replace("%tag", filename).replace("%list", str(files_path))
        print("Launching external command and waiting for it to finish:")
        print(cmd)
        subprocess.run(cmd, shell=True, check=True)

    return json_path


def backup_tar(info, filename, backup_root, args):
    backup_root.mkdir(parents=True, exist_ok=True)
    tar_path = backup_root / f"{filename}.tar"

    with tarfile.open(tar_path, "w") as tf:
        data = json.dumps(info, indent=2).encode()
        ti = tarfile.TarInfo(name="container.json")
        ti.size = len(data)
        tf.addfile(ti, fileobj=io.BytesIO(data))

        for m in info.get("Mounts", []):
            src = m["Source"]
            if args.verbose:
                print(f"Mount ({m.get('Type')}) {src} -> {m['Destination']}")

            # Same reasoning as in do_backup(): skip sockets/devices/missing
            # paths instead of letting tarfile blow up on a non-regular file.
            if not os.path.exists(src):
                print(f"Warning: mount '{src}' does not exist, skipping it.")
                continue

            tf.add(src, arcname=src.lstrip("/"))

    print(f"Created backup: {tar_path}")
    return tar_path


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def create_container_from_info(info, args):
    """Creates a new container based on the stored `docker inspect`
    metadata. Port mappings, environment, entrypoint, cmd, working dir and
    labels are restored.

    Note (same limitation as the original Go tool): mounts are not
    restored 1-to-1 as bind mounts. Only volumes already defined in the
    image itself (VOLUME instruction) are automatically created by
    Docker -- the data for those is copied back afterwards. Other bind
    mounts need to be added back manually (see warnings during restore)."""
    config = info["Config"]
    host_config = info.get("HostConfig", {})
    name = info["Name"].lstrip("/")
    image = config["Image"]

    try:
        docker_inspect(image)
    except RuntimeError:
        print(f"Pulling image: {image}")
        run(["docker", "pull", image], capture=False)

    cmd = ["docker", "create", "--name", name]

    for env in config.get("Env") or []:
        cmd += ["-e", env]

    for key, val in (config.get("Labels") or {}).items():
        cmd += ["--label", f"{key}={val}"]

    for container_port, bindings in (host_config.get("PortBindings") or {}).items():
        proto = container_port.split("/")[-1] if "/" in container_port else "tcp"
        port_only = container_port.split("/")[0]
        for b in bindings or [{}]:
            host_port = b.get("HostPort", "")
            host_ip = b.get("HostIp", "")
            spec = f"{host_port}:{port_only}/{proto}"
            if host_ip:
                spec = f"{host_ip}:{spec}"
            cmd += ["-p", spec]

    if config.get("WorkingDir"):
        cmd += ["-w", config["WorkingDir"]]

    if config.get("Entrypoint"):
        cmd += ["--entrypoint", " ".join(config["Entrypoint"])]

    cmd.append(image)

    if config.get("Cmd"):
        cmd += config["Cmd"]

    print("Restoring container:", name)
    new_id = run(cmd)
    print(f"Created container with ID: {new_id}")
    return new_id


def match_new_mounts(old_mounts, new_info):
    """Maps old mounts to new mounts based on Destination."""
    new_mounts = new_info.get("Mounts", [])
    mapping = {}
    for old in old_mounts:
        for new in new_mounts:
            if old["Destination"] == new["Destination"]:
                mapping[old["Destination"]] = new
                break
    return mapping


def do_restore(backup_file, args):
    path = Path(backup_file)
    if path.suffix == ".json":
        info = json.loads(path.read_text())
        return restore_from_info(info, path.parent, args)
    if path.suffix == ".tar":
        return restore_tar(path, args)
    raise RuntimeError("Unknown file type, please provide a .json or .tar file")


def restore_from_info(info, backup_dir, args):
    old_mounts = info.get("Mounts", [])
    new_id = create_container_from_info(info, args)
    new_info = docker_inspect(new_id)
    mapping = match_new_mounts(old_mounts, new_info)

    for old in old_mounts:
        new = mapping.get(old["Destination"])
        src_data = backup_dir / mount_folder_name(old)

        if not new:
            print(f"Warning: no automatically created mount for "
                  f"{old['Destination']} -- data in '{src_data}' must be "
                  f"restored manually (e.g. bind-mount it yourself).")
            continue

        if not src_data.exists():
            print(f"Warning: no backup data found in {src_data}, skipping.")
            continue

        print(f"Restoring: {src_data} -> {new['Source']}")
        shutil.copytree(src_data, new["Source"], dirs_exist_ok=True)

    if args.start:
        start_container(new_id)

    return new_id


def restore_tar(tar_path, args):
    with tarfile.open(tar_path, "r") as tf:
        info = json.loads(tf.extractfile("container.json").read())
        old_mounts = info.get("Mounts", [])
        new_id = create_container_from_info(info, args)
        new_info = docker_inspect(new_id)
        mapping = match_new_mounts(old_mounts, new_info)

        with tempfile.TemporaryDirectory() as tmp:
            tf.extractall(tmp)

            for old in old_mounts:
                new = mapping.get(old["Destination"])
                extracted = Path(tmp) / old["Source"].lstrip("/")

                if not new:
                    print(f"Warning: no automatically created mount for "
                          f"{old['Destination']} -- data in '{extracted}' "
                          f"must be restored manually.")
                    continue

                if not extracted.exists():
                    print(f"Warning: no data found for {old['Destination']} in tar, skipping.")
                    continue

                print(f"Restoring: {extracted} -> {new['Source']}")
                shutil.copytree(extracted, new["Source"], dirs_exist_ok=True)

    if args.start:
        start_container(new_id)

    return new_id


def start_container(container_id):
    print(f"Starting container: {container_id[:12]}")
    run(["docker", "start", container_id], capture=False)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Backup & restore Docker containers")
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("backup", help="create a backup of a container")
    b.add_argument("container", nargs="?", help="container ID or name")
    b.add_argument("-a", "--all", action="store_true", help="back up all running containers")
    b.add_argument("-s", "--stopped", action="store_true", help="with --all: also include stopped containers")
    b.add_argument("-t", "--tar", action="store_true", help="create a .tar backup instead of json + copied mounts")
    b.add_argument("-o", "--output", default="./backups", help="root directory for backups (default: ./backups)")
    b.add_argument("-l", "--launch", default=None, help="external command after backup; %%tag and %%list are substituted")
    b.add_argument("-v", "--verbose", action="store_true", help="print detailed progress")

    r = sub.add_parser("restore", help="restore a backup")
    r.add_argument("backup_file", help=".json or .tar backup file")
    r.add_argument("-s", "--start", action="store_true", help="start the container after restoring")

    args = parser.parse_args()

    if args.command == "backup":
        if args.all:
            ps_cmd = ["docker", "ps", "-q"]
            if args.stopped:
                ps_cmd.append("-a")
            ids = [i for i in run(ps_cmd).splitlines() if i]
            for cid in ids:
                # Isolate failures per container so one problematic container
                # (e.g. one with a docker.sock bind mount, permission issues,
                # or any other unexpected error) doesn't abort the whole
                # --all run.
                try:
                    do_backup(cid, args)
                except Exception as e:
                    print(f"Error backing up container {cid}: {e}", file=sys.stderr)
                    continue
        else:
            if not args.container:
                parser.error("provide a container ID/name, or use --all")
            do_backup(args.container, args)
    elif args.command == "restore":
        do_restore(args.backup_file, args)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)