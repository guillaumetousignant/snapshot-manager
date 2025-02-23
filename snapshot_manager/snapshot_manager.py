#!/bin/python

import argparse
import logging
import subprocess
import sys
import tomllib
from importlib import metadata
from pathlib import Path
from typing import Optional

import requests


class Mount:
    origin: str = ""
    destination: str = ""
    children: list[str] = []

    def __init__(self, origin: str, destination: str, children: list[str]):
        self.origin = origin
        self.destination = destination
        self.children = children


class App:
    name: str = ""
    containers: list[str] = []
    mounts: list[Mount] = []

    def __init__(self, name: str, containers: list[str], mounts: list[Mount]):
        self.name = name
        self.containers = containers
        self.mounts = mounts


class Configuration:
    retention: int = 0
    backup_containers: list[str] = []
    monitor_url: Optional[str] = ""
    apps: list[App] = []

    def __init__(
        self,
        retention: int,
        backup_containers: list[str],
        monitor_url: Optional[str],
        apps: list[App],
    ):
        self.retention = retention
        self.backup_containers = backup_containers
        self.monitor_url = monitor_url
        self.apps = apps


def remove_mount_last_snapshot(mount: Mount, app_name: str, retention: int):
    result = subprocess.run(
        ["zfs", "list", "-r", "-t", "snapshot", f"{mount.origin}@backup-{retention}"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        logging.debug(
            f"Removing previous {app_name} {mount.origin} snapshot with age {retention}"
        )
        result = subprocess.run(
            ["zfs", "destroy", "-r", f"{mount.origin}@backup-{retention}"]
        )
        if result.returncode != 0:
            logging.critical(
                f"Failed to destroy {app_name} {mount.origin} snapshot with age {retention}"
            )
            raise RuntimeError(
                f"Failed to destroy {app_name} {mount.origin} snapshot with age {retention}"
            )


def rename_mount_snapshots(mount: Mount, app_name: str, retention: int):
    for i in range(retention - 1, 0, -1):
        result = subprocess.run(
            ["zfs", "list", "-r", "-t", "snapshot", f"{mount.origin}@backup-{i}"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            logging.debug(
                f"Renaming previous {app_name} {mount.origin} snapshot with age {i}"
            )
            result = subprocess.run(
                [
                    "zfs",
                    "rename",
                    "-r",
                    f"{mount.origin}@backup-{i}",
                    f"{mount.origin}@backup-{i + 1}",
                ]
            )
            if result.returncode != 0:
                logging.critical(
                    f"Failed to rename {app_name} {mount.origin} snapshot with age {i}"
                )
                raise RuntimeError(
                    f"Failed to rename {app_name} {mount.origin} snapshot with age {i}"
                )


def rename_mount_latest_snapshot(mount: Mount, app_name: str):
    result = subprocess.run(
        ["zfs", "list", "-r", "-t", "snapshot", f"{mount.origin}@backup"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        logging.debug(f"Renaming previous {app_name} {mount.origin} snapshot")
        result = subprocess.run(
            [
                "zfs",
                "rename",
                "-r",
                f"{mount.origin}@backup",
                f"{mount.origin}@backup-{1}",
            ]
        )
        if result.returncode != 0:
            logging.critical(f"Failed to rename {app_name} {mount.origin} snapshot")
            raise RuntimeError(f"Failed to rename {app_name} {mount.origin} snapshot")


def remove_last_snapshot(app: App, retention: int):
    logging.info(f"Removing snapshot past retention for app {app.name}")
    for mount in app.mounts:
        remove_mount_last_snapshot(mount, app.name, retention)


def archive_snapshots(app: App, retention: int):
    for mount in app.mounts:
        remove_mount_last_snapshot(mount, app.name, retention)
        rename_mount_snapshots(mount, app.name, retention)
        rename_mount_latest_snapshot(mount, app.name)


def stop_containers(app: App):
    logging.debug(f"Stopping {app.name} container(s)")
    result = subprocess.run(
        ["docker", "stop"] + app.containers[::-1],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logging.critical(f"Failed to stop {app.name} container(s)")
        raise RuntimeError(f"Failed to stop {app.name} container(s)")


def snapshot_mounts(app: App):
    for mount in app.mounts:
        logging.debug(f"Performing {app.name} {mount.origin} snapshot")
        result = subprocess.run(["zfs", "snapshot", "-r", f"{mount.origin}@backup"])
        if result.returncode != 0:
            logging.critical(f"Failed to create {app.name} {mount.origin} snapshot")
            raise RuntimeError(f"Failed to create {app.name} {mount.origin} snapshot")


def start_containers(app: App):
    logging.debug(f"Starting {app.name} container(s)")
    result = subprocess.run(
        ["docker", "start"] + app.containers,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logging.critical(f"Failed to start {app.name} container(s)")
        raise RuntimeError(f"Failed to start {app.name} container(s)")


def do_snapshot(app: App, retention: int):
    logging.info(f"Snapshotting app {app.name}")

    archive_snapshots(app, retention)

    if app.containers:
        stop_containers(app)

    snapshot_mounts(app)

    if app.containers:
        start_containers(app)


def unmount_mount(mount: Mount, app_name: str):
    result = subprocess.run(["mountpoint", "-q", f"{mount.destination}"])
    if result.returncode == 0:
        for child in mount.children:
            result = subprocess.run(
                ["mountpoint", "-q", f"{mount.destination}/{child}"]
            )
            if result.returncode == 0:
                logging.debug(
                    f"Unmounting previous {app_name} {mount.destination}/{child} snapshot"
                )
                result = subprocess.run(["umount", f"{mount.destination}/{child}"])
                if result.returncode != 0:
                    logging.critical(
                        f"Failed to unmount {app_name} {mount.destination}/{child} snapshot"
                    )
                    raise RuntimeError(
                        f"Failed to unmount {app_name} {mount.destination}/{child} snapshot"
                    )

        logging.debug(f"Unmounting previous {app_name} {mount.destination} snapshot")
        result = subprocess.run(["umount", mount.destination])
        if result.returncode != 0:
            logging.critical(
                f"Failed to unmount {app_name} {mount.destination} snapshot"
            )
            raise RuntimeError(
                f"Failed to unmount {app_name} {mount.destination} snapshot"
            )


def mount_mount(mount: Mount, app_name: str):
    result = subprocess.run(["mkdir", "-p", mount.destination])
    if result.returncode != 0:
        logging.critical(
            f"Failed to create {app_name} {mount.destination} top-level directory"
        )
        raise RuntimeError(
            f"Failed to create {app_name} {mount.destination} top-level directory"
        )

    logging.debug(f"Mounting {app_name} {mount.destination} snapshot")
    result = subprocess.run(
        ["mount", "-t", "zfs", f"{mount.origin}@backup", mount.destination]
    )
    if result.returncode != 0:
        logging.critical(
            f"Failed to mount {app_name} snapshot {mount.origin}@backup at {mount.destination}"
        )
        raise RuntimeError(
            f"Failed to mount {app_name} snapshot {mount.origin}@backup at {mount.destination}"
        )

    for child in mount.children:
        logging.debug(f"Mounting {app_name} {mount.destination}/{child} snapshot")
        result = subprocess.run(
            [
                "mount",
                "-t",
                "zfs",
                f"{mount.origin}/{child}@backup",
                f"{mount.destination}/{child}",
            ]
        )
        if result.returncode != 0:
            logging.critical(
                f"Failed to mount {app_name} snapshot {mount.origin}/{child}@backup at {mount.destination}/{child}"
            )
            raise RuntimeError(
                f"Failed to mount {app_name} snapshot {mount.origin}/{child}@backup at {mount.destination}/{child}"
            )


def do_mount(app: App):
    logging.info(f"Mounting snapshot for app {app.name}")
    for mount in app.mounts:
        unmount_mount(mount, app.name)
        mount_mount(mount, app.name)


def stop_backup_containers(backup_containers: list[str]):
    logging.info("Stopping backup container(s)")
    result = subprocess.run(
        ["docker", "stop"] + backup_containers[::-1],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logging.critical("Failed to stop backup container(s)")
        raise RuntimeError("Failed to stop backup container(s)")


def start_backup_containers(backup_containers: list[str]):
    logging.info("Starting backup container(s)")
    result = subprocess.run(
        ["docker", "start"] + backup_containers,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logging.critical("Failed to start backup container(s)")
        raise RuntimeError("Failed to start backup container(s)")


def send_notification(monitor_url: str, status: str, message: str):
    logging.debug(
        f'Sending notification with status "{status}" and message "{message}"'
    )
    try:
        response = requests.get(
            monitor_url,
            params={"status": status, "msg": message},
            timeout=20,
        )
    except Exception as e:
        logging.error(
            f'Failed to send notification with status "{status}" and message "{message}", exception "{e}"'
        )
        return

    try:
        response_data = response.json()
        if not response_data["ok"]:
            logging.error(
                f'Failed to send notification with status "{status}" and message "{message}", received response "{response_data}"'
            )
    except requests.JSONDecodeError:
        logging.error(
            f'Failed to send notification with status "{status}" and message "{message}", could not destructure response "{response}"'
        )


def snapshot_manager(
    retention: int,
    backup_containers: list[str],
    monitor_url: Optional[str],
    apps: list[App],
):
    logging.info("Start backup snapshot")
    if monitor_url is not None:
        send_notification(monitor_url, "up", "start")

    for app in apps:
        do_snapshot(app, retention)

    if backup_containers:
        stop_backup_containers(backup_containers)

    for app in apps:
        do_mount(app)

    if backup_containers:
        start_backup_containers(backup_containers)

    for app in apps:
        remove_last_snapshot(app, retention)

    if monitor_url is not None:
        send_notification(monitor_url, "up", "finish")
    logging.info("Finished backup snapshot")


DEFAULT_RETENTION = 14


def read_config(
    config_path: Path,
    input_retention: Optional[int] = None,
    input_backup_containers: Optional[list[str]] = None,
    input_monitor_url: Optional[str] = None,
) -> Configuration:
    logging.info(f'Reading configuration file "{config_path}"')

    if not config_path.is_file():
        logging.critical(f'Configuration file "{config_path}" does not exist')
        raise RuntimeError(f'Configuration file "{config_path}" does not exist')

    with open(config_path, "rb") as f:
        configuration = tomllib.load(f)

    config_retention = configuration["config"].get("retention")
    retention = (
        input_retention
        if input_retention is not None
        else config_retention
        if config_retention is not None
        else DEFAULT_RETENTION
    )
    logging.debug(f"Using retention: {retention}")

    config_backup_containers = configuration["config"].get("backup-containers")
    backup_containers = (
        input_backup_containers
        if input_backup_containers is not None
        else config_backup_containers
        if config_backup_containers is not None
        else []
    )
    logging.debug(f'Using backup containers: "{", ".join(backup_containers)}"')

    monitor_url = (
        input_monitor_url
        if input_monitor_url is not None
        else configuration["config"].get("monitor-url")
    )
    logging.debug(
        f"""Using monitor url: {f'"{monitor_url}"' if monitor_url is not None else "no monitor"}"""
    )

    apps: list[App] = []
    for app in configuration.get("apps", []):
        name = app["name"]
        containers = app["containers"]

        mounts: list[Mount] = []
        for mount in app.get("mounts", []):
            origin = mount["origin"]
            destination = mount["destination"]
            children = mount.get("children", [])

            mounts.append(Mount(origin, destination, children))

        apps.append(App(name, containers, mounts))

    logging.debug(f'Using apps: "{", ".join([app.name for app in apps])}"')

    return Configuration(retention, backup_containers, monitor_url, apps)


def snapshot_manager_main(args: argparse.Namespace):
    config = read_config(args.config)

    snapshot_manager(
        config.retention, config.backup_containers, config.monitor_url, config.apps
    )


def get_version() -> str:
    try:
        return metadata.version("snapshot-manager")
    except Exception:
        try:
            with open("pyproject.toml", "rb") as f:
                pyproject = tomllib.load(f)
            return pyproject["project"]["version"]
        except Exception:
            return "develop"


DEFAULT_CONFIG_PATH = "config.toml"


def main():
    parser = argparse.ArgumentParser(
        prog="Snapshot Manager",
        description="Makes periodic snapshots of app storage and mounts them for backup.",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to a configuration file to use",
    )
    parser.add_argument(
        "-r",
        "--retention",
        type=int,
        help="Number of snapshots to retain",
    )
    parser.add_argument(
        "-b",
        "--backup-containers",
        type=str,
        nargs="*",
        help="Backup containers to stop before updating mounts and start after",
    )
    parser.add_argument(
        "-m",
        "--monitor-url",
        type=str,
        help="Uptime Kuma-compatible URL to get tu update status",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        dest="verbosity",
        default=0,
        help="verbose output (repeat to increase verbosity)",
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {get_version()}"
    )
    parser.set_defaults(func=snapshot_manager_main)

    args = parser.parse_args(sys.argv[1:])

    match args.verbosity:
        case 0:
            log_level = logging.WARNING
        case 1:
            log_level = logging.INFO
        case 2:
            log_level = logging.DEBUG
        case _:
            raise RuntimeError(
                f"Unsupported log level {args.verbosity}. 0 for WARNING, 1 for INFO, 2 for DEBUG"
            )

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s:%(levelname)s:%(name)s:%(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    try:
        args.func(args)
    except KeyboardInterrupt:
        logging.info("Keyboard interrupt received")
        if args.monitor_url is not None:
            send_notification(args.monitor_url, "down", "interrupted")
    except Exception as e:
        logging.exception("Unhandled exception")
        if args.monitor_url is not None:
            send_notification(args.monitor_url, "down", "exception")
        raise e


if __name__ == "__main__":
    main()
