"""Lightweight static checker for the HOI4-EYES native GUI file.

This is not a substitute for HOI4 Mod Utilities Preview or loading the mod in
HOI4. It validates the project's GUI subset, references, and explicit geometry.
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias


@dataclass
class Block:
    entries: list[tuple[str, str | "Block"]] = field(default_factory=list)

    def values(self, key: str) -> list[str | "Block"]:
        return [value for entry_key, value in self.entries if entry_key == key]

    def first(self, key: str) -> str | "Block" | None:
        values = self.values(key)
        return values[0] if values else None


Token: TypeAlias = str
TOKEN_RE = re.compile(r'\s+|#[^\r\n]*|"(?:\\.|[^"\\])*"|[{}=]|[^\s{}=#]+')
CONTROL_TYPES = {
    "containerWindowType", "instantTextBoxType", "buttonType", "gridBoxType",
    "iconType", "textBoxType", "editBoxType", "scrollbarType",
}


class PdxParseError(ValueError):
    pass


def parse_pdx(text: str) -> Block:
    tokens = [m.group(0) for m in TOKEN_RE.finditer(text)
              if not m.group(0).isspace() and not m.group(0).startswith("#")]
    index = 0

    def parse_pairs(in_braces: bool) -> Block:
        nonlocal index
        if in_braces:
            if index >= len(tokens) or tokens[index] != "{":
                raise PdxParseError(f"Expected '{{' at token {index}")
            index += 1
        result = Block()
        while index < len(tokens) and (not in_braces or tokens[index] != "}"):
            key = tokens[index]
            index += 1
            if index < len(tokens) and tokens[index] in {">", "<", "==", "!="}:
                operator = tokens[index]
                index += 1
                if index >= len(tokens):
                    raise PdxParseError(f"Missing comparison value for {key!r}")
                result.entries.append((key, operator + tokens[index]))
                index += 1
                continue
            if index >= len(tokens) or tokens[index] != "=":
                raise PdxParseError(f"Expected '=' after {key!r} at token {index}")
            index += 1
            if index >= len(tokens):
                raise PdxParseError(f"Missing value for {key!r}")
            value = parse_pairs(True) if tokens[index] == "{" else tokens[index]
            if not isinstance(value, Block):
                index += 1
            result.entries.append((key.strip('"'), value))
        if in_braces and (index >= len(tokens) or tokens[index] != "}"):
            raise PdxParseError("Unclosed '{' block")
        if in_braces:
            index += 1
        return result

    root = parse_pairs(False)
    if index != len(tokens):
        raise PdxParseError(f"Unexpected trailing token {tokens[index]!r}")
    return root


def scalar(block: Block, key: str) -> str | None:
    value = block.first(key)
    if isinstance(value, Block) or value is None:
        return None
    return value.strip('"')


def number(value: str | None) -> float | None:
    if value is None or not re.fullmatch(r"-?\d+(?:\.\d+)?", value):
        return None
    return float(value)


def point(block: Block, key: str) -> tuple[float, float] | None:
    value = block.first(key)
    if not isinstance(value, Block):
        return None
    x, y = number(scalar(value, "x")), number(scalar(value, "y"))
    return (x, y) if x is not None and y is not None else None


def rect_for(control_type: str, node: Block) -> tuple[float, float, float, float] | None:
    pos = point(node, "position")
    if pos is None:
        return None
    size = node.first("size")
    width = height = None
    if isinstance(size, Block):
        width = number(scalar(size, "width"))
        height = number(scalar(size, "height"))
        if width is None:
            width = number(scalar(size, "x"))
        if height is None:
            height = number(scalar(size, "y"))
    if control_type in {"instantTextBoxType", "textBoxType"}:
        width = width if width is not None else number(scalar(node, "maxWidth"))
        height = height if height is not None else number(scalar(node, "maxHeight"))
    if width is None or height is None:
        return None
    # HOI4's CENTER orientation interprets position as the control's center,
    # while upper_left interprets it as the top-left corner.
    if (scalar(node, "Orientation") or "").upper() == "CENTER":
        return pos[0] - width / 2, pos[1] - height / 2, width, height
    return pos[0], pos[1], width, height


def blocks_of_type(parent: Block, wanted: set[str]) -> list[tuple[str, Block]]:
    return [(key, value) for key, value in parent.entries
            if key in wanted and isinstance(value, Block)]


def walk(block: Block):
    yield block
    for _, value in block.entries:
        if isinstance(value, Block):
            yield from walk(value)


def check_geometry(gui_root: Block, allow_overlaps: set[frozenset[str]]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    gui_types = gui_root.first("guiTypes")
    if not isinstance(gui_types, Block):
        return ["Missing guiTypes block"], warnings
    containers = blocks_of_type(gui_types, {"containerWindowType"})
    for _, parent in containers:
        parent_name = scalar(parent, "name") or "<unnamed>"
        parent_size = parent.first("size")
        if not isinstance(parent_size, Block):
            continue
        parent_w = number(scalar(parent_size, "width"))
        parent_h = number(scalar(parent_size, "height"))
        if parent_w is None or parent_h is None:
            continue
        boxes: list[tuple[str, tuple[float, float, float, float]]] = []
        for control_type, child in blocks_of_type(parent, CONTROL_TYPES):
            name = scalar(child, "name") or f"<{control_type}>"
            rect = rect_for(control_type, child)
            if rect is None:
                continue  # Anchored controls, percent-sized grids, and sprite-sized icons.
            x, y, width, height = rect
            # Negative offsets with orientation are anchored to that edge by HOI4.
            if (x < 0 or y < 0) and scalar(child, "Orientation"):
                continue
            if width <= 0 or height <= 0:
                errors.append(f"{parent_name}/{name} has non-positive dimensions {width:g}x{height:g}")
            if x < 0 or y < 0 or x + width > parent_w or y + height > parent_h:
                errors.append(
                    f"{parent_name}/{name} bounds ({x:g},{y:g},{width:g},{height:g}) "
                    f"exceed parent {parent_w:g}x{parent_h:g}"
                )
            boxes.append((name, rect))
        for index, (left_name, left) in enumerate(boxes):
            lx, ly, lw, lh = left
            for right_name, right in boxes[index + 1:]:
                rx, ry, rw, rh = right
                overlap = lx < rx + rw and rx < lx + lw and ly < ry + rh and ry < ly + lh
                if overlap:
                    pair = frozenset((left_name, right_name))
                    message = f"{parent_name}: sibling overlap {left_name} / {right_name}"
                    if pair in allow_overlaps:
                        continue
                    warnings.append(message)
    return errors, warnings


def _steam_library_roots(environ: dict[str, str]) -> list[Path]:
    """Find Steam libraries from common Windows installs and libraryfolders.vdf."""
    roots: list[Path] = []
    for variable, suffix in (
        ("ProgramFiles(x86)", "Steam"),
        ("ProgramFiles", "Steam"),
        ("LOCALAPPDATA", "Programs/Steam"),
        ("STEAM_DIR", ""),
    ):
        value = environ.get(variable)
        if value:
            root = Path(value).expanduser() / suffix if suffix else Path(value).expanduser()
            if root not in roots:
                roots.append(root)

    # Resolve additional libraries listed in each discovered Steam root.
    for root in tuple(roots):
        vdf = root / "steamapps" / "libraryfolders.vdf"
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for encoded in re.findall(r'"path"\s+"((?:[^"\\]|\\.)*)"', text):
            decoded = encoded.replace("\\\\", "\\").replace("\\\"", '"')
            library = Path(decoded)
            if library not in roots:
                roots.append(library)
    return roots


def discover_hoi4_install(environ: dict[str, str] | None = None) -> Path | None:
    """Locate HOI4 through an explicit override or Steam library metadata."""
    env = os.environ if environ is None else environ
    override = env.get("EYES_HOI4_INSTALL_PATH") or env.get("HOI4_GAME_DIR")
    if override:
        candidate = Path(override).expanduser()
        if (candidate / "hoi4.exe").is_file():
            return candidate.resolve()

    for library in _steam_library_roots(env):
        manifest = library / "steamapps" / "appmanifest_394360.acf"
        install_dir = "Hearts of Iron IV"
        try:
            manifest_text = manifest.read_text(encoding="utf-8", errors="replace")
            match = re.search(r'"installdir"\s+"([^"]+)"', manifest_text)
            if match:
                install_dir = match.group(1)
        except OSError:
            pass
        candidate = library / "steamapps" / "common" / install_dir
        if (candidate / "hoi4.exe").is_file():
            return candidate.resolve()
        # Also support non-Steam or manually copied game folders explicitly
        # exposed as a root by the user or environment.
        if (library / "hoi4.exe").is_file():
            return library.resolve()
    return None


def validate_files(mod_dir: Path, game_dir: Path | None,
                   allow_overlaps: set[frozenset[str]] | None = None) -> tuple[list[str], list[str], dict[str, int]]:
    gui_path = mod_dir / "interface" / "ai_assistant_diplomacy_gui.gui"
    scripted_path = mod_dir / "common" / "scripted_guis" / "ai_assistant_diplomacy_gui.txt"
    loc_dir = mod_dir / "localisation" / "english"
    errors: list[str] = []
    warnings: list[str] = []
    for path in (gui_path, scripted_path):
        if not path.is_file():
            errors.append(f"Missing required file: {path}")
    if errors:
        return errors, warnings, {}
    try:
        gui = parse_pdx(gui_path.read_text(encoding="utf-8-sig"))
        scripted = parse_pdx(scripted_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, PdxParseError) as exc:
        return [f"HOI4-EYES GUI parse failed: {exc}"], warnings, {}

    loc_files = list(loc_dir.glob("*.yml")) if loc_dir.exists() else []
    loc_texts = []
    for path in loc_files:
        try:
            raw = path.read_bytes()
            if not raw.startswith(b"\xef\xbb\xbf"):
                errors.append(f"Localization must be UTF-8 with BOM: {path}")
            loc_texts.append(raw.decode("utf-8-sig"))
        except UnicodeError as exc:
            errors.append(f"Localization is not UTF-8-readable: {path}: {exc}")
    loc_text = "\n".join(loc_texts)
    if loc_files and not re.search(r"^[ \t]*l_english[ \t]*:", loc_text, re.M):
        errors.append("English localization header l_english: is missing")
    loc_keys = set(re.findall(r"^[ \t]*([A-Za-z0-9_.-]+)[ \t]*:", loc_text, re.M))

    gui_nodes = list(walk(gui))
    gui_names = {scalar(node, "name") for node in gui_nodes if scalar(node, "name")}
    used_loc_keys = set()
    sprites = set()
    for node in gui_nodes:
        for prop in ("text", "buttonText"):
            value = scalar(node, prop)
            if value and re.fullmatch(r"EYES_[A-Z0-9_]+", value):
                used_loc_keys.add(value)
        for prop in ("spriteType", "quadTextureSprite"):
            value = scalar(node, prop)
            if value and value.startswith("GFX_"):
                sprites.add(value)
    for key in sorted(used_loc_keys - loc_keys):
        errors.append(f"Missing English localization key: {key}")

    gfx_paths = list(mod_dir.rglob("*.gfx"))
    if game_dir is not None:
        gfx_paths.extend((game_dir / "interface").glob("*.gfx"))
    gfx_text = []
    for path in gfx_paths:
        try:
            gfx_text.append(path.read_text(encoding="utf-8-sig", errors="replace"))
        except OSError as exc:
            warnings.append(f"Could not read graphics definition {path}: {exc}")
    defined_sprites = set(re.findall(r"\bname\s*=\s*\"(GFX_[A-Za-z0-9_]+)\"", "\n".join(gfx_text)))
    for sprite in sorted(sprites - defined_sprites):
        if game_dir is None:
            warnings.append(f"Could not verify base-game sprite definition without a game path: {sprite}")
        else:
            errors.append(f"Missing sprite definition: {sprite}")

    script_nodes = list(walk(scripted))
    script_names = {scalar(node, "window_name") for node in script_nodes if scalar(node, "window_name")}
    for window in sorted(name for name in gui_names if name.endswith("_window")):
        if window not in script_names:
            errors.append(f"GUI window is not linked from scripted_gui: {window}")
    buttons: list[str | None] = []
    def collect_buttons(block: Block):
        for key, value in block.entries:
            if isinstance(value, Block):
                if key == "buttonType":
                    buttons.append(scalar(value, "name"))
                collect_buttons(value)
    collect_buttons(gui)
    script_text = scripted_path.read_text(encoding="utf-8-sig")
    for name in sorted(button for button in buttons if button):
        if f"{name}_click" not in script_text:
            warnings.append(f"Button has no matching scripted GUI click handler: {name}")
    for node in script_nodes:
        for value in node.values("country_scope_entry_container"):
            if isinstance(value, str) and value.strip('"') not in gui_names:
                errors.append(f"Dynamic-list row container is missing: {value}")
        for value in node.values("array"):
            if isinstance(value, str) and value.startswith("global."):
                pass  # Array scope is validated by the in-game system, not this file checker.

    geo_errors, geo_warnings = check_geometry(gui, allow_overlaps or set())
    errors.extend(geo_errors)
    warnings.extend(geo_warnings)
    counts = {
        "localized_keys": len(used_loc_keys), "sprites": len(sprites),
        "gui_names": len(gui_names), "buttons": len([item for item in buttons if item]),
    }
    return errors, warnings, counts


def main(argv: list[str] | None = None) -> int:
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mod-dir", type=Path, default=project / "mod" / "ai_assistant")
    parser.add_argument("--game-dir", type=Path, default=None,
                        help="HOI4 install directory; otherwise auto-detect Steam or use EYES_HOI4_INSTALL_PATH")
    parser.add_argument("--allow-overlap", action="append", default=[], metavar="NAME1,NAME2",
                        help="Allow a documented intentional overlap pair")
    args = parser.parse_args(argv)
    allow = {frozenset(part.strip() for part in value.split(",", 1))
             for value in args.allow_overlap if len(value.split(",", 1)) == 2}
    game_dir = args.game_dir or discover_hoi4_install()
    if game_dir is not None and not (game_dir / "hoi4.exe").is_file():
        warnings = [f"HOI4 executable was not found under {game_dir}; vanilla sprite lookup skipped"]
        game_dir = None
    else:
        warnings = []
    if game_dir is None:
        warnings.append("HOI4 install not found; base-game sprite references were not checked")
    errors, file_warnings, counts = validate_files(args.mod_dir, game_dir, allow)
    warnings.extend(file_warnings)
    for warning in warnings:
        print(f"WARN: {warning}")
    for error in errors:
        print(f"ERROR: {error}")
    if errors:
        print(f"GUI_STATIC_CHECK=FAIL ({len(errors)} errors, {len(warnings)} warnings)")
        return 1
    print("GUI_STATIC_CHECK=PASS " + " ".join(f"{key}={value}" for key, value in counts.items()))
    print(f"warnings={len(warnings)} (potential intended overlaps should be reviewed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def parse_mod_descriptor(text: str) -> Block:
    """Read launcher scalar fields; tag/dependency arrays are valid PDX lists.

    Keep the stricter GUI parser unchanged. Duplicate identity fields and
    malformed braces fail closed rather than choosing a convenient value.
    """
    tokens=[m.group(0) for m in TOKEN_RE.finditer(text)
            if not m.group(0).isspace() and not m.group(0).startswith('#')]
    result=Block(); index=0
    while index < len(tokens):
        key=tokens[index].strip('"'); index+=1
        if index>=len(tokens) or tokens[index]!='=':raise PdxParseError('Invalid descriptor assignment')
        index+=1
        if index>=len(tokens):raise PdxParseError('Missing descriptor value')
        value=tokens[index]; index+=1
        if value=='{':
            depth=1
            while index<len(tokens) and depth:
                depth += (tokens[index]=='{')-(tokens[index]=='}'); index+=1
            if depth:raise PdxParseError('Unclosed descriptor list')
            value=Block()
        elif value in {'}','='}:raise PdxParseError('Invalid descriptor value')
        if key in {'name','path','remote_file_id','version','supported_version'}:
            if result.values(key) or isinstance(value,Block):raise PdxParseError('Invalid or duplicate descriptor identity')
        result.entries.append((key,value))
    return result
