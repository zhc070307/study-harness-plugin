from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_cache_path, user_config_path, user_data_path, user_log_path

APP_NAME = "LocalTranscribe"


def data_root() -> Path:
    override = os.environ.get("LOCALTRANSCRIBE_DATA_DIR")
    root = Path(override).expanduser() if override else user_data_path(APP_NAME, appauthor=False)
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def cache_root() -> Path:
    override = os.environ.get("LOCALTRANSCRIBE_CACHE_DIR")
    root = Path(override).expanduser() if override else user_cache_path(APP_NAME, appauthor=False)
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def config_root() -> Path:
    override = os.environ.get("LOCALTRANSCRIBE_CONFIG_DIR")
    root = Path(override).expanduser() if override else user_config_path(APP_NAME, appauthor=False)
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def tasks_root() -> Path:
    path = data_root() / "tasks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def models_root() -> Path:
    path = data_root() / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def documents_root() -> Path:
    path = data_root() / "Documents"
    path.mkdir(parents=True, exist_ok=True)
    return path


def task_cache_root() -> Path:
    path = cache_root() / "tasks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_root() -> Path:
    override = os.environ.get("LOCALTRANSCRIBE_LOG_DIR")
    path = Path(override).expanduser() if override else user_log_path(APP_NAME, appauthor=False)
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()
