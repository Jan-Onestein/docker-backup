#!/usr/bin/env python3
"""
docker_backup.py - Python re-implementatie van docker-backup (oorspronkelijk Go)

Maakt backups van Docker containers: metadata (json) + een kopie van alle
volume-mounts, of optioneel een enkel .tar bestand. Kan een backup ook weer
terugzetten (restore).

Vereist: python3 en de `docker` CLI in PATH. Praat via subprocess met de
Docker CLI -- geen extra pip-dependency nodig.

Gebruik:
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
    """Voert een commando uit en geeft stdout terug (of gooit een RuntimeError)."""
    result = subprocess.run(cmd, capture_output=capture, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"Commando mislukt: {' '.join(cmd)}\n{result.stderr.strip()}")
    return result.stdout.strip() if capture else None


def docker_inspect(ref):
    """Geeft de volledige `docker inspect`-output terug als dict."""
    out = run(["docker", "inspect", ref])
    data = json.loads(out)
    if not data:
        raise RuntimeError(f"Kon container/image '{ref}' niet vinden.")
    return data[0]


def sanitize(name):
    """Maakt een string veilig als bestands-/mapnaam."""
    name = name.strip("/")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def mount_folder_name(mount):
    """Submapnaam voor een mount: Docker volumenaam indien beschikbaar,
    anders het gesanitized destination-pad (bind mount)."""
    if mount.get("Name"):
        return sanitize(mount["Name"])
    return sanitize(mount["Destination"])


def get_full_image_name(image):
    """Probeert een volledige image:tag te vinden als er geen tag is opgegeven."""
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
            tf.add(src, arcname=src.lstrip("/"))

    print(f"Created backup: {tar_path}")
    return tar_path


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def create_container_from_info(info, args):
    """Maakt een nieuwe container op basis van de opgeslagen `docker inspect`
    metadata. Poort-mappings, environment, entrypoint, cmd, working-dir en
    labels worden hersteld.

    Let op (zelfde beperking als de originele Go-tool): mounts worden niet
    1-op-1 als bind-mount teruggezet. Alleen volumes die al in de image zelf
    gedefinieerd staan (VOLUME-instructie) worden automatisch door Docker
    aangemaakt -- de data daarvoor wordt na het aanmaken teruggekopieerd.
    Overige bind-mounts moet je zelf opnieuw toevoegen (zie waarschuwingen
    tijdens restore)."""
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
    """Koppelt oude mounts aan nieuwe mounts op basis van Destination."""
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
    raise RuntimeError("Onbekend bestandstype, geef een .json of .tar bestand op")


def restore_from_info(info, backup_dir, args):
    old_mounts = info.get("Mounts", [])
    new_id = create_container_from_info(info, args)
    new_info = docker_inspect(new_id)
    mapping = match_new_mounts(old_mounts, new_info)

    for old in old_mounts:
        new = mapping.get(old["Destination"])
        src_data = backup_dir / mount_folder_name(old)

        if not new:
            print(f"Waarschuwing: geen automatisch aangemaakte mount voor "
                  f"{old['Destination']} -- data in '{src_data}' moet je zelf "
                  f"terugzetten (bv. handmatig bind-mounten).")
            continue
        if not src_data.exists():
            print(f"Waarschuwing: geen backupdata gevonden in {src_data}, sla over.")
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
                    print(f"Waarschuwing: geen automatisch aangemaakte mount voor "
                          f"{old['Destination']} -- data in '{extracted}' moet je "
                          f"zelf terugzetten.")
                    continue
                if not extracted.exists():
                    print(f"Waarschuwing: geen data gevonden voor {old['Destination']} in tar, sla over.")
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

    b = sub.add_parser("backup", help="maak een backup van een container")
    b.add_argument("container", nargs="?", help="container ID of naam")
    b.add_argument("-a", "--all", action="store_true", help="backup alle draaiende containers")
    b.add_argument("-s", "--stopped", action="store_true", help="i.c.m. --all: ook gestopte containers meenemen")
    b.add_argument("-t", "--tar", action="store_true", help="maak een .tar backup i.p.v. json + gekopieerde mounts")
    b.add_argument("-o", "--output", default="./backups", help="root map voor backups (default: ./backups)")
    b.add_argument("-l", "--launch", default=None, help="extern commando na backup; %%tag en %%list worden vervangen")
    b.add_argument("-v", "--verbose", action="store_true", help="print gedetailleerde voortgang")

    r = sub.add_parser("restore", help="herstel een backup")
    r.add_argument("backup_file", help=".json of .tar backup-bestand")
    r.add_argument("-s", "--start", action="store_true", help="start de container na herstel")

    args = parser.parse_args()

    if args.command == "backup":
        if args.all:
            ps_cmd = ["docker", "ps", "-q"]
            if args.stopped:
                ps_cmd.append("-a")
            ids = [i for i in run(ps_cmd).splitlines() if i]
            for cid in ids:
                do_backup(cid, args)
        else:
            if not args.container:
                parser.error("geef een container ID/naam op, of gebruik --all")
            do_backup(args.container, args)

    elif args.command == "restore":
        do_restore(args.backup_file, args)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(f"Fout: {e}", file=sys.stderr)
        sys.exit(1)
