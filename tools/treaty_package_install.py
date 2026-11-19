"""Install a compiled EYES decision package into a HOI4 mod folder.

The compiler (`treaty_decision_package.compile_decision`) produces files and
returns them as ``PREPARED_NOT_INSTALLED``. This module is the only place that
writes them to disk, and it refuses to touch anything outside the requested
mod folder.

Safety rules enforced here:

* an explicit ``confirm=True`` is required for every real write;
* the destination must look like a mod (``descriptor.mod`` beside it);
* HOI4 must not be running: the game loads mod files at startup only;
* only the package's own declared files are written, and a file that exists
  without being recorded in a previous EYES manifest is never overwritten;
* every write is backed up first and restored if any later write fails.

This module never writes into the game installation, never edits saves and
never sends anything to a running process.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from tempfile import mkdtemp
from uuid import uuid4

from bridge_instance import bridge_instance_lock
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 1
MANIFEST_DIR = ".eyes_install"
MANIFEST_NAME = "manifest.json"
INSTALL_ROOTS_ENV = "EYES_INSTALL_ROOTS"
MAX_FILES = 64
MAX_FILE_BYTES = 2 * 1024 * 1024
ALLOWED_PREFIXES = (
    "events/",
    "common/decisions/",
    "common/on_actions/",
    "localisation/english/",
    "localisation/russian/",
    "localisation/french/",
    "common/scripted_effects/",
)


class InstallError(ValueError):
    """A refused or failed installation; the message is a stable error code."""


_UNSET = object()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hoI4_running() -> bool | None:
    """Return True/False when the OS answers, None when it cannot be checked.

    ``None`` is not permission: the installer refuses to guess.
    """
    if os.name != "nt":
        return None
    try:
        output = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq hoi4.exe", "/NH"],
            capture_output=True, text=True, timeout=15, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (output.stdout or "").casefold()
    if "hoi4.exe" in text:
        return True
    if output.returncode == 0:
        return False
    return None


def _safe_relative(name: Any) -> Path:
    if not isinstance(name, str) or not name or len(name) > 260:
        raise InstallError("invalid_file_name")
    if "\\" in name:
        raise InstallError("invalid_file_name")
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise InstallError("unsafe_file_name")
    if not any(name.startswith(prefix) for prefix in ALLOWED_PREFIXES):
        raise InstallError("file_outside_allowed_tree")
    return relative


def validate_package(package: Any) -> dict[str, Any]:
    """Accept only a compiled, not-yet-applied EYES package."""
    if not isinstance(package, Mapping):
        raise InstallError("invalid_package")
    if package.get("status") != "PREPARED_NOT_INSTALLED":
        raise InstallError("package_not_prepared")
    if package.get("game_effect_applied") is not False:
        raise InstallError("package_claims_applied_effect")
    files = package.get("files")
    if not isinstance(files, Mapping) or not 1 <= len(files) <= MAX_FILES:
        raise InstallError("invalid_package_files")
    normalized: dict[str, str] = {}
    for name, content in files.items():
        relative = _safe_relative(name)
        if not isinstance(content, str):
            raise InstallError("invalid_file_content")
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise InstallError("file_too_large")
        normalized[relative.as_posix()] = content
    identity = {
        "campaign_id": package.get("campaign_id"),
        "agreement_id": package.get("agreement_id"),
        "version": package.get("version"),
        "execution_request_id": package.get("execution_request_id"),
    }
    if not all(isinstance(value, str) and value for key, value in identity.items()
               if key != "version"):
        raise InstallError("package_identity_missing")
    if type(identity["version"]) is not int or identity["version"] < 1:
        raise InstallError("package_identity_missing")
    slot=package.get('review_slot')
    if slot is not None:
        if (not isinstance(slot,dict) or set(slot)!={'seed','pair','from_day','through_day'}
                or not isinstance(slot['seed'],str) or not re.fullmatch(r'[a-f0-9]{32}',slot['seed'])
                or not isinstance(slot['pair'],list) or len(slot['pair'])!=2
                or any(not isinstance(tag,str) or not re.fullmatch(r'[A-Z][A-Z0-9]{2}',tag) for tag in slot['pair'])
                or slot['pair'][0]>=slot['pair'][1]
                or any(type(slot[k]) is not int for k in ('from_day','through_day'))
                or not 0<=slot['from_day']<=slot['through_day']<=4_000_000):
            raise InstallError('invalid_treaty_review_slot')
    return {"identity": identity, "files": normalized, "result_marker": package.get("result_marker"),
            "review_slot":slot}


def package_digest(files: Mapping[str, str]) -> str:
    payload = json.dumps(dict(sorted(files.items())), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _manifest_path(mod_root: Path) -> Path:
    return mod_root / MANIFEST_DIR / MANIFEST_NAME


def read_manifest(mod_root: Path) -> dict[str, Any]:
    path = _manifest_path(mod_root)
    if not path.is_file():
        return {"schema_version": SCHEMA_VERSION, "installed": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise InstallError("manifest_unreadable") from error
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise InstallError("manifest_schema_invalid")
    if not isinstance(data.get("installed"), list):
        raise InstallError("manifest_schema_invalid")
    return data


def _check_mod_root(mod_root: Any) -> Path:
    root = Path(mod_root).resolve()
    if not root.is_dir():
        raise InstallError("mod_root_missing")
    if not (root / "descriptor.mod").is_file():
        raise InstallError("not_a_mod_folder")
    manifest_dir = root / MANIFEST_DIR
    if not manifest_dir.resolve().is_relative_to(root) or not _manifest_path(root).resolve().is_relative_to(root):
        raise InstallError("unsafe_manifest_path")
    return root


def _owned_files(manifest: Mapping[str, Any]) -> set[str]:
    owned: set[str] = set()
    for entry in manifest.get("installed", []):
        if isinstance(entry, dict) and isinstance(entry.get("files"), dict):
            owned.update(entry["files"])
    return owned


def allowed_install_roots(default_root: Any = None) -> list[Path]:
    """Roots this process may write into; ``EYES_INSTALL_ROOTS`` decides when set.

    The default is ``app/prepared``: a staging tree inside the project, never
    the installed game and never the live mod folder.
    """
    raw = os.getenv(INSTALL_ROOTS_ENV, "")
    roots = [Path(item).expanduser().resolve() for item in raw.split(os.pathsep) if item.strip()]
    if not roots:
        base = Path(default_root) if default_root is not None else Path(__file__).resolve().parents[1] / "prepared"
        roots = [base.resolve()]
    return roots


def ensure_allowed_root(mod_root: Any, *, default_root: Any = None) -> Path:
    root = Path(mod_root).resolve()
    for allowed in allowed_install_roots(default_root):
        if root == allowed or root.is_relative_to(allowed):
            return root
    raise InstallError("mod_root_not_allowed")


def install(package: Any, *, mod_root: Any, confirm: bool, game_running: Any = _UNSET,
            dry_run: bool = False, allow_unknown_game_state: bool = False,
            allowed_roots: Any = None) -> dict[str, Any]:
    """Write a compiled package into one mod folder. Return a JSON-safe report."""
    prepared = validate_package(package)
    files = prepared["files"]
    digest = package_digest(files)
    root = _check_mod_root(mod_root) if not dry_run else Path(mod_root).resolve()
    if dry_run:
        return {"installed": False, "dry_run": True, "package_digest": digest,
                "files": sorted(files), "mod_root": str(root),
                "note": "No file was written; the destination was not validated."}
    if allowed_roots is not None and not any(
            root == Path(allowed).resolve() or root.is_relative_to(Path(allowed).resolve())
            for allowed in allowed_roots):
        raise InstallError("mod_root_not_allowed")
    if confirm is not True:
        raise InstallError("explicit_confirmation_required")
    # None is a deliberate "unknown", not "not supplied": only the sentinel
    # triggers detection, so a caller can never accidentally skip the check.
    running = hoI4_running() if game_running is _UNSET else game_running
    if running is None and not allow_unknown_game_state:
        raise InstallError("game_state_unknown")
    if running is True:
        raise InstallError("hoi4_running")

    with bridge_instance_lock(_manifest_path(root).parent / "package.lock") as acquired:
        if not acquired:
            raise InstallError("package_operation_in_progress")
        return _install_prepared(prepared, root, digest)


def _write_manifest(root, manifest):
    path = _manifest_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{MANIFEST_NAME}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _install_prepared(prepared, root, digest):
    files = prepared["files"]
    for name in files:
        if not (root / name).resolve().is_relative_to(root):
            raise InstallError("unsafe_file_destination")

    manifest = read_manifest(root)
    identity = prepared["identity"]
    for entry in manifest["installed"]:
        old_identity = entry.get("identity", {})
        same_version = all(old_identity.get(key) == identity[key]
                           for key in ("campaign_id", "agreement_id", "version"))
        if entry.get("package_digest") == digest:
            if old_identity != identity:
                raise InstallError("package_identity_conflict")
            if entry.get("retired_reviews"):
                raise InstallError("package_review_retired")
            for name, content in files.items():
                target = root / name
                if not target.is_file() or target.read_bytes() != content.encode("utf-8"):
                    raise InstallError("installed_package_files_changed")
            return {"installed": False, "already_installed": True, "package_digest": digest,
                    "mod_root": str(root), "files": sorted(files)}
        if same_version:
            raise InstallError("agreement_version_already_installed")
        slot,previous=prepared['review_slot'],entry.get('review_slot')
        if (slot and previous and not entry.get('retired_reviews')
                and all(slot[key]==previous[key] for key in ('seed','pair'))
                and max(slot['from_day'],previous['from_day'])<=min(slot['through_day'],previous['through_day'])):
            raise InstallError('overlapping_treaty_review_for_same_native_pair')
        if old_identity and set(files).intersection(entry.get("files", {})):
            raise InstallError("package_file_owned_by_another_agreement")
    owned = _owned_files(manifest)
    for name in files:
        target = root / name
        if target.exists() and name not in owned:
            raise InstallError("foreign_file_would_be_overwritten")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = root / MANIFEST_DIR / "backups" / f"{stamp}-{uuid4().hex[:8]}"
    if not backup_dir.resolve().is_relative_to(root):
        raise InstallError("unsafe_backup_path")
    written: list[str] = []
    try:
        for name, content in files.items():
            target = root / name
            if target.exists():
                backup = backup_dir / name
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
            target.parent.mkdir(parents=True, exist_ok=True)
            staged = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
            try:
                staged.write_text(content, encoding="utf-8", newline="\n")
                os.replace(staged, target)
                written.append(name)
            finally:
                staged.unlink(missing_ok=True)
        entry = {
            "package_digest": digest,
            "identity": prepared["identity"],
            "files": {name: hashlib.sha256(content.encode("utf-8")).hexdigest()
                      for name, content in files.items()},
            "result_marker": prepared["result_marker"],
            "installed_at": _utc_now(),
            "backup": str(backup_dir.relative_to(root)) if backup_dir.exists() else None,
            **({'review_slot':prepared['review_slot']} if prepared['review_slot'] else {}),
        }
        manifest["installed"].append(entry)
        _write_manifest(root, manifest)
    except OSError as error:
        rollback_failed = False
        for name in reversed(written):
            backup = backup_dir / name
            target = root / name
            try:
                if backup.is_file():
                    shutil.copy2(backup, target)
                else:
                    target.unlink(missing_ok=True)
            except OSError:
                rollback_failed = True
        code = "install_rollback_failed" if rollback_failed else "install_failed_rolled_back"
        raise InstallError(code) from error
    return {"installed": True, "package_digest": digest, "mod_root": str(root),
            "files": sorted(files), "backup": entry["backup"],
            "note": "HOI4 loads mod files at startup; restart the game to see the decision."}


# Exact files from the two old preloaded test releases. Back up only these
# review definitions, leaving their events and treaty monitors for old saves.
LEGACY_REVIEW_HASHES = {
    "common/decisions/eyes_na_60ee19161111.txt":
        "f8f1a72b72404610aa92bf8e290b5b6ade21e02350a5d88207282cc8b8addb3a",
    "common/decisions/categories/eyes_na_60ee19161111.txt":
        "999248f9fca57461f13c6448d23375c5fcbfcfc60daa79f75ab87f513f5d9494",
    "common/decisions/eyes_na_2265e3bf3ee7.txt":
        "3d276b85e6996d3c179a92a2d3caa0f1c42e445ae5f33d108da078cd02976210",
    "common/decisions/categories/eyes_na_2265e3bf3ee7.txt":
        "9304a4903479ccca94334629f56bddd07eae7b79afdd597ea2bd2227fb57317e",
}


def retire_legacy_reviews(*, mod_root: Any, confirm: bool, dry_run: bool = False):
    """Archive the known legacy test menu files outside HOI4's script trees."""
    root = _check_mod_root(mod_root)
    owned = _owned_files(read_manifest(root))
    matching = {}
    skipped = []
    for name, digest in LEGACY_REVIEW_HASHES.items():
        source = root / name
        if not source.is_file():
            continue
        if (name in owned or not source.resolve().is_relative_to(root)
                or hashlib.sha256(source.read_bytes().replace(b"\r\n", b"\n")).hexdigest() != digest):
            skipped.append(name)
        else:
            matching[name] = source
    if dry_run or not matching:
        return {"archived": [], "matching": sorted(matching), "skipped": sorted(skipped)}
    if confirm is not True:
        raise InstallError("explicit_confirmation_required")
    if hoI4_running() is not False:
        raise InstallError("hoi4_must_be_closed")
    backup = root / MANIFEST_DIR / "backups" / "retired-legacy-test-reviews"
    if not backup.resolve().is_relative_to(root):
        raise InstallError("unsafe_backup_path")
    if any((backup / name).exists() for name in matching):
        raise InstallError("legacy_review_backup_exists")
    moved = []
    try:
        for name, source in matching.items():
            destination = backup / name
            if not destination.resolve().is_relative_to(backup):
                raise InstallError("unsafe_backup_path")
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.rename(destination)
            moved.append(name)
    except (OSError, InstallError):
        for name in reversed(moved):
            (backup / name).rename(matching[name])
        raise
    return {"archived": sorted(moved), "skipped": sorted(skipped), "backup": str(backup)}


def uninstall(*, mod_root: Any, package_digest: str, confirm: bool,
              game_running: Any = _UNSET) -> dict[str, Any]:
    """Uninstall only the exact installed bytes, with all paths verified first."""
    if confirm is not True:
        raise InstallError("explicit_confirmation_required")
    if (hoI4_running() if game_running is _UNSET else game_running) is not False:
        raise InstallError("hoi4_must_be_closed")
    root = _check_mod_root(mod_root)
    with bridge_instance_lock(_manifest_path(root).parent / "package.lock") as acquired:
        if not acquired:
            raise InstallError("package_operation_in_progress")
        return _uninstall_package(root, package_digest)


def _uninstall_package(root, package_digest):
    manifest = read_manifest(root)
    entry = next((item for item in manifest["installed"]
                  if item.get("package_digest") == package_digest), None)
    if entry is None:
        raise InstallError("package_not_installed")
    remaining = [item for item in manifest["installed"] if item is not entry]
    still_owned = _owned_files({"installed": remaining})
    backup_dir = root / entry["backup"] if entry.get("backup") else None
    backup_root = (root / MANIFEST_DIR / "backups").resolve()
    if backup_dir and (not backup_root.is_relative_to(root) or
                       not backup_dir.resolve().is_relative_to(backup_root)):
        raise InstallError("unsafe_backup_path")
    changes = {}
    for name, expected in entry["files"].items():
        target = root / _safe_relative(name)
        if name in entry.get("retired_reviews", ()) and not target.exists():
            continue
        backup = backup_dir / name if backup_dir else None
        if not target.resolve().is_relative_to(root):
            raise InstallError("unsafe_file_destination")
        if backup and not backup.resolve().is_relative_to(backup_dir.resolve()):
            raise InstallError("unsafe_backup_path")
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise InstallError("installed_package_files_changed")
        if backup and backup.is_file():
            changes[name] = backup
        elif name not in still_owned:
            changes[name] = None
    rollback = Path(mkdtemp(prefix="uninstall-", dir=_manifest_path(root).parent))
    removed = []
    preserve_rollback = False
    try:
        for name in changes:
            original = rollback / name
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name, original)
        try:
            for name, backup in changes.items():
                removed.append(name)
                if backup:
                    shutil.copy2(backup, root / name)
                else:
                    (root / name).unlink()
            manifest["installed"] = remaining
            _write_manifest(root, manifest)
        except OSError as error:
            for name in removed:
                try:
                    shutil.copy2(rollback / name, root / name)
                except OSError:
                    preserve_rollback = True
            code = "uninstall_rollback_failed" if preserve_rollback else "uninstall_failed_rolled_back"
            raise InstallError(code) from error
    finally:
        if not preserve_rollback and rollback.resolve().is_relative_to(_manifest_path(root).parent.resolve()):
            shutil.rmtree(rollback, ignore_errors=True)
    return {"uninstalled": True, "files": sorted(removed), "package_digest": package_digest}


def status(*, mod_root: Any) -> dict[str, Any]:
    root = _check_mod_root(mod_root)
    manifest = read_manifest(root)
    return {"mod_root": str(root), "schema_version": manifest.get("schema_version"),
            "installed": [{key: entry[key] for key in ("package_digest", "identity", "installed_at", "result_marker")
                           if key in entry} for entry in manifest.get("installed", [])]}


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Install a compiled EYES package into a mod folder")
    sub = parser.add_subparsers(dest="command", required=True)
    install_cmd = sub.add_parser("install", help="write a PREPARED_NOT_INSTALLED package")
    install_cmd.add_argument("--package", type=Path, required=True)
    install_cmd.add_argument("--mod-root", type=Path, required=True)
    install_cmd.add_argument("--confirm", action="store_true", help="required for any real write")
    install_cmd.add_argument("--dry-run", action="store_true")
    uninstall_cmd = sub.add_parser("uninstall", help="remove one installed package")
    uninstall_cmd.add_argument("--mod-root", type=Path, required=True)
    uninstall_cmd.add_argument("--package-digest", required=True)
    uninstall_cmd.add_argument("--confirm", action="store_true")
    cleanup_cmd = sub.add_parser("retire-legacy-reviews", help="archive known legacy test menu entries")
    cleanup_cmd.add_argument("--mod-root", type=Path, required=True)
    cleanup_cmd.add_argument("--confirm", action="store_true")
    cleanup_cmd.add_argument("--dry-run", action="store_true")
    status_cmd = sub.add_parser("status", help="show installed packages")
    status_cmd.add_argument("--mod-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "install":
            package = json.loads(args.package.read_text(encoding="utf-8-sig"))
            result = install(package, mod_root=args.mod_root, confirm=args.confirm,
                             dry_run=args.dry_run)
        elif args.command == "uninstall":
            result = uninstall(mod_root=args.mod_root, package_digest=args.package_digest,
                               confirm=args.confirm)
        elif args.command == "retire-legacy-reviews":
            result = retire_legacy_reviews(mod_root=args.mod_root, confirm=args.confirm, dry_run=args.dry_run)
        else:
            result = status(mod_root=args.mod_root)
    except InstallError as error:
        print(json.dumps({"error": str(error)}))
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
