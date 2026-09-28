"""
Packages the bundled AI-assistant skills (operations-manager/skills/) into
downloadable zips on demand - the same on-demand-zip approach as
app/sdk_packaging.py, so a download always matches whatever skill content
shipped in this image rather than a prebuilt artifact someone forgot to
rebuild.

Three platforms, one shared integration knowledge base (see each
platform's own file for why the format differs):

  - claude  - a real Claude Skill: skills/claude/SKILL.md (YAML frontmatter
              + instructions body).
  - chatgpt - skills/chatgpt/{README.md, INSTRUCTIONS.md} - ChatGPT has no
              single portable skill-file format, so this ships as plain
              instructions text plus a README on where to paste it
              (Custom GPT instructions, an Assistants/Responses system
              message, or an AGENTS.md file).
  - gemini  - skills/gemini/{README.md, GEMINI.md} - GEMINI.md is the
              context-file convention Gemini CLI/Code Assist read
              automatically, the Gemini analogue of CLAUDE.md.
"""
import io
import zipfile
from pathlib import Path
from typing import Dict

# operations-manager/app/skill_packaging.py -> operations-manager/skills
SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"

PLATFORMS: Dict[str, str] = {
    "claude": "Claude Skill",
    "chatgpt": "ChatGPT Skill",
    "gemini": "Gemini Skill",
}

_EXCLUDE_DIR_NAMES = {"__pycache__", ".pytest_cache"}
_EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


def _source_dir(platform: str) -> Path:
    return SKILLS_ROOT / platform


def _iter_source_files(platform: str):
    source_dir = _source_dir(platform)
    if not source_dir.exists():
        return
    for path in sorted(source_dir.rglob("*")):
        if path.is_dir():
            continue
        if any(part in _EXCLUDE_DIR_NAMES for part in path.parts):
            continue
        if path.suffix in _EXCLUDE_SUFFIXES:
            continue
        yield path


def skill_available(platform: str) -> bool:
    return platform in PLATFORMS and any(_iter_source_files(platform))


def skill_label(platform: str) -> str:
    return PLATFORMS.get(platform, platform)


def skill_filename(platform: str) -> str:
    return f"couchbase-aom-{platform}-skill.zip"


def build_skill_archive(platform: str) -> bytes:
    """Zip one platform's skill folder into an in-memory archive, rooted at
    a single top-level directory so unzipping doesn't scatter files."""
    source_dir = _source_dir(platform)
    root_name = f"couchbase-aom-{platform}-skill"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in _iter_source_files(platform):
            arcname = f"{root_name}/{path.relative_to(source_dir).as_posix()}"
            zf.write(path, arcname)
    return buffer.getvalue()
