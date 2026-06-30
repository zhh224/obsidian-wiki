"""obsidian-wiki installer CLI.

Python port of ``setup.sh`` for the pip-installed package. The skill content
lives inside the installed package (``obsidian_wiki/_data/skills``) instead of a
cloned repo, so this wires the bundled skills into every supported AI agent's
skills directory and writes ``~/.obsidian-wiki/config`` so the skills resolve
the vault from any project.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from obsidian_wiki import __version__

HOME = Path.home()
GLOBAL_CONFIG_DIR = HOME / ".obsidian-wiki"
GLOBAL_CONFIG = GLOBAL_CONFIG_DIR / "config"

# Skills usable from any project (no vault context needed beyond the global
# config). These are also installed globally for agents that only scope skills
# per-project, so cross-project sync/query work everywhere.
PORTABLE_SKILLS = ("wiki-update", "wiki-query")


# ── Data resolution ──────────────────────────────────────────────────────────
# Works for both a built wheel (data under <pkg>/_data) and an editable/source
# checkout (data at the repo root next to the package).
def _pkg_dir() -> Path:
    return Path(__file__).resolve().parent


def skills_dir() -> Path:
    """Return the directory holding the bundled skill folders."""
    for cand in (_pkg_dir() / "_data" / "skills", _pkg_dir().parent / ".skills"):
        if cand.is_dir():
            return cand
    raise FileNotFoundError(
        "Could not locate bundled skills. Reinstall obsidian-wiki "
        "(`pip install --force-reinstall obsidian-wiki`)."
    )


def bootstrap_dir() -> Path | None:
    """Return the directory containing agent bootstrap context files.

    For a wheel this is ``_data/bootstrap``; for a source checkout the files are
    spread across the repo root, so we return the repo root and resolve each
    file via the repo-relative layout in ``_bootstrap_files``.
    """
    built = _pkg_dir() / "_data" / "bootstrap"
    if built.is_dir():
        return built
    repo = _pkg_dir().parent
    if (repo / "AGENTS.md").is_file():
        return repo
    return None


def list_skills() -> list[str]:
    return sorted(p.name for p in skills_dir().iterdir() if p.is_dir())


# ── Skill installation ───────────────────────────────────────────────────────
def install_skills(
    target_dir: Path,
    label: str,
    *,
    subset: tuple[str, ...] | None = None,
    mode: str = "symlink",
    quiet: bool = False,
) -> int:
    """Install bundled skills into *target_dir*. Returns the count installed."""
    src_root = skills_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    installed = 0
    for skill in sorted(p for p in src_root.iterdir() if p.is_dir()):
        name = skill.name
        if subset is not None and name not in subset:
            continue
        link_path = target_dir / name

        if link_path.is_symlink() or link_path.is_file():
            link_path.unlink()
        elif link_path.is_dir():
            # A real directory we previously copied here is safe to replace;
            # anything else is the user's and we leave it alone.
            if (link_path / "SKILL.md").exists():
                shutil.rmtree(link_path)
            else:
                print(f"   ⚠️  {link_path} is not a managed skill, skipping")
                continue

        if mode == "symlink":
            link_path.symlink_to(skill, target_is_directory=True)
        else:  # copy
            shutil.copytree(skill, link_path)

        if not (link_path / "SKILL.md").exists():
            raise RuntimeError(f"broken skill install: {link_path} -> {skill}")
        installed += 1

    if not quiet:
        print(f"✅  Installed {installed} skills → {label}")
    return installed


# Agents whose skills directory lives under $HOME. (path-under-home, label,
# subset). All get every skill — pip users have no cloned repo to host
# project-scoped skills, so everything must be globally discoverable.
GLOBAL_AGENT_DIRS: list[tuple[str, str, tuple[str, ...] | None]] = [
    (".claude/skills", "~/.claude/skills/ (Claude Code)", None),
    (".gemini/skills", "~/.gemini/skills/ (Gemini CLI)", None),
    (".gemini/antigravity/skills", "~/.gemini/antigravity/skills/ (Antigravity, legacy)", None),
    (".codex/skills", "~/.codex/skills/ (Codex)", None),
    (".hermes/skills", "~/.hermes/skills/ (Hermes default)", None),
    (".openclaw/skills", "~/.openclaw/skills/ (OpenClaw)", None),
    (".copilot/skills", "~/.copilot/skills/ (GitHub Copilot CLI)", None),
    (".trae/skills", "~/.trae/skills/ (Trae)", None),
    (".trae-cn/skills", "~/.trae-cn/skills/ (Trae CN)", None),
    (".kiro/skills", "~/.kiro/skills/ (Kiro CLI)", None),
    (".pi/agent/skills", "~/.pi/agent/skills/ (Pi)", None),
    (".agents/skills", "~/.agents/skills/ (OpenCode, Aider, Droid, generic)", None),
]


def install_global_skills(mode: str) -> None:
    for rel, label, subset in GLOBAL_AGENT_DIRS:
        install_skills(HOME / rel, label, subset=subset, mode=mode)
    _install_hermes_profiles(mode)


def _install_hermes_profiles(mode: str) -> None:
    """Mirror setup.sh: install into the active and all named Hermes profiles."""
    hermes_home = os.environ.get("HERMES_HOME")
    handled: set[Path] = set()
    if hermes_home:
        hp = Path(hermes_home).expanduser()
        if hp != HOME / ".hermes":
            install_skills(hp / "skills", f"{hp}/skills/ (Hermes active profile)", mode=mode)
            handled.add(hp)
    profiles = HOME / ".hermes" / "profiles"
    if profiles.is_dir():
        for prof in sorted(p for p in profiles.iterdir() if p.is_dir()):
            if prof in handled:
                continue
            install_skills(
                prof / "skills",
                f"~/.hermes/profiles/{prof.name}/skills/ (Hermes profile: {prof.name})",
                mode=mode,
            )


# ── Project-local install (opt-in) ───────────────────────────────────────────
PROJECT_AGENT_DIRS = [
    (".claude/skills", "Claude Code"),
    (".cursor/skills", "Cursor"),
    (".windsurf/skills", "Windsurf"),
    (".agents/skills", "OpenCode / generic"),
    (".pi/skills", "Pi"),
    (".kiro/skills", "Kiro"),
]

# (bootstrap-relative source path, destination relative to project dir).
# The source path is resolved against bootstrap_dir() for a wheel, or mapped to
# the repo layout for a source checkout (see _resolve_bootstrap_src).
BOOTSTRAP_FILES = [
    ("AGENTS.md", "AGENTS.md"),
    ("cursor/rules/obsidian-wiki.mdc", ".cursor/rules/obsidian-wiki.mdc"),
    ("windsurf/rules/obsidian-wiki.md", ".windsurf/rules/obsidian-wiki.md"),
    ("kiro/steering/obsidian-wiki.md", ".kiro/steering/obsidian-wiki.md"),
    ("agent/rules/obsidian-wiki.md", ".agent/rules/obsidian-wiki.md"),
    ("agent/workflows/obsidian-wiki.md", ".agent/workflows/obsidian-wiki.md"),
    ("github/copilot-instructions.md", ".github/copilot-instructions.md"),
]

# AGENTS.md aliases created as symlinks within the project (single source).
AGENTS_ALIASES = ("CLAUDE.md", "GEMINI.md", ".hermes.md")


def _resolve_bootstrap_src(boot_root: Path, rel: str) -> Path | None:
    """Resolve a bootstrap source path under a wheel layout or repo layout."""
    built = boot_root / rel
    if built.exists():
        return built
    # Source checkout: boot_root is the repo root; files use the repo layout.
    repo_rel = {
        "AGENTS.md": "AGENTS.md",
        "cursor/rules/obsidian-wiki.mdc": ".cursor/rules/obsidian-wiki.mdc",
        "windsurf/rules/obsidian-wiki.md": ".windsurf/rules/obsidian-wiki.md",
        "kiro/steering/obsidian-wiki.md": ".kiro/steering/obsidian-wiki.md",
        "agent/rules/obsidian-wiki.md": ".agent/rules/obsidian-wiki.md",
        "agent/workflows/obsidian-wiki.md": ".agent/workflows/obsidian-wiki.md",
        "github/copilot-instructions.md": ".github/copilot-instructions.md",
    }.get(rel)
    if repo_rel and (boot_root / repo_rel).exists():
        return boot_root / repo_rel
    return None


def install_project(project_dir: Path, mode: str) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n📁  Installing project-local files → {project_dir}")
    for rel, _label in PROJECT_AGENT_DIRS:
        install_skills(project_dir / rel, f"{rel}/", mode=mode)

    boot_root = bootstrap_dir()
    if boot_root is None:
        print("   ⚠️  Bootstrap files not found in package; skipping context files")
        return

    for rel, dest in BOOTSTRAP_FILES:
        src = _resolve_bootstrap_src(boot_root, rel)
        if src is None:
            continue
        dst = project_dir / dest
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_symlink() or dst.exists():
            if dst.is_dir() and not dst.is_symlink():
                continue
            dst.unlink()
        shutil.copyfile(src, dst)
    print("✅  Installed bootstrap context files (AGENTS.md, rules, workflows)")

    # AGENTS.md aliases as relative symlinks (copy fallback for symlink-hostile FS).
    for alias in AGENTS_ALIASES:
        link = project_dir / alias
        if link.is_symlink() or link.exists():
            link.unlink()
        try:
            link.symlink_to("AGENTS.md")
        except OSError:
            shutil.copyfile(project_dir / "AGENTS.md", link)
    print(f"✅  Linked AGENTS.md aliases ({', '.join(AGENTS_ALIASES)})")


# ── Config ───────────────────────────────────────────────────────────────────
def _read_config_value(key: str) -> str:
    if not GLOBAL_CONFIG.is_file():
        return ""
    for line in GLOBAL_CONFIG.read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"')
    return ""


def resolve_vault_path(cli_vault: str | None) -> str:
    if cli_vault:
        return os.path.expanduser(cli_vault)
    existing = _read_config_value("OBSIDIAN_VAULT_PATH")
    if existing and existing != "/path/to/your/vault":
        return existing
    if sys.stdin.isatty():
        try:
            entered = input("  Where is your Obsidian vault? (absolute path): ").strip()
        except EOFError:
            entered = ""
        if entered:
            return os.path.expanduser(entered)
    return existing


def write_config(vault_path: str) -> None:
    GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # OBSIDIAN_WIKI_REPO points at the bundled data root so skills that reference
    # framework assets (templates, references) can find them post-install.
    repo_root = skills_dir().parent
    GLOBAL_CONFIG.write_text(
        f'OBSIDIAN_VAULT_PATH="{vault_path}"\n'
        f'OBSIDIAN_WIKI_REPO="{repo_root}"\n'
        f'OBSIDIAN_WIKI_VERSION="{__version__}"\n'
    )
    print(f"✅  Global config written to {GLOBAL_CONFIG}")


def _check_stale() -> None:
    """Warn if the installed version doesn't match when setup last ran, or if skills are missing."""
    if not GLOBAL_CONFIG.is_file():
        print(
            f"⚠️  obsidian-wiki {__version__} is installed but setup has never been run.\n"
            f"   Run: obsidian-wiki setup --vault /path/to/your/vault",
            file=sys.stderr,
        )
        return

    setup_version = _read_config_value("OBSIDIAN_WIKI_VERSION")
    if setup_version and setup_version != __version__:
        print(
            f"⚠️  obsidian-wiki upgraded {setup_version} → {__version__} but setup hasn't been re-run.\n"
            f"   New skills won't be available until you run: obsidian-wiki setup",
            file=sys.stderr,
        )
        return

    # Even if the version matches, check that ~/.claude/skills has the full set.
    claude_skills_dir = HOME / ".claude" / "skills"
    if claude_skills_dir.is_dir():
        bundled = set(list_skills())
        installed = {p.name for p in claude_skills_dir.iterdir() if p.is_dir()}
        missing = bundled - installed
        if missing:
            print(
                f"⚠️  {len(missing)} skill(s) missing from ~/.claude/skills/ "
                f"(e.g. {', '.join(sorted(missing)[:3])}{', ...' if len(missing) > 3 else ''}).\n"
                f"   Run: obsidian-wiki setup",
                file=sys.stderr,
            )


# ── Commands ─────────────────────────────────────────────────────────────────
def cmd_setup(args: argparse.Namespace) -> int:
    mode = "copy" if args.copy else "symlink"
    print("\n╔══════════════════════════════════════════════════╗")
    print("║         obsidian-wiki — Agent Setup              ║")
    print("╚══════════════════════════════════════════════════╝\n")

    vault_path = resolve_vault_path(args.vault)
    write_config(vault_path)
    if not vault_path:
        print("    → Vault path not set yet. Re-run with `--vault /path/to/vault`")
        print("      or edit OBSIDIAN_VAULT_PATH in ~/.obsidian-wiki/config.")

    if not args.project_only:
        print()
        install_global_skills(mode)

    if args.project is not None:
        project_dir = Path(args.project or os.getcwd()).expanduser().resolve()
        install_project(project_dir, mode)

    n = len(list_skills())
    print("\n───────────────────────────────────────────────────")
    print(" Setup complete!\n")
    print(f" Skills installed: {n}  (mode: {mode})")
    if vault_path:
        print(f" Vault:            {vault_path}")
    print("\n Next steps:")
    print("   1. Open a project in your agent")
    print('   2. Say: "set up my wiki"\n')
    print(" From any project:")
    print("   /wiki-update    → sync knowledge into your vault")
    print("   /wiki-query     → ask questions against your wiki")
    print("───────────────────────────────────────────────────\n")
    return 0


def cmd_batch_plan(args: argparse.Namespace) -> int:
    from obsidian_wiki.batch import plan_batches
    source_dir = Path(args.source_dir).expanduser().resolve()
    vault = Path(args.vault).expanduser().resolve()
    if not source_dir.is_dir():
        print(f"error: source directory not found: {source_dir}", file=sys.stderr)
        return 1
    result = plan_batches(
        source_dir,
        vault,
        max_batch_mb=args.max_mb,
        max_batch_files=args.max_files,
        skip_unchanged=not args.no_cache,
        include_code=args.include_code,
    )
    if args.pretty:
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps(result))
    return 0


def cmd_graph_analyse(args: argparse.Namespace) -> int:
    from obsidian_wiki.graph_analysis import analyse_vault
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        print(f"error: vault not found: {vault}", file=sys.stderr)
        return 1
    result = analyse_vault(vault, top_n=args.top)
    if args.pretty:
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps(result))
    return 0


def cmd_cache_check(args: argparse.Namespace) -> int:
    from obsidian_wiki.cache import check_sources
    vault = Path(args.vault).expanduser().resolve()
    sources = [Path(p).expanduser().resolve() for p in args.sources]
    result = check_sources(vault, sources)
    if args.pretty:
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps(result))
    return 0


def cmd_cache_update(args: argparse.Namespace) -> int:
    from obsidian_wiki.cache import update_source
    vault = Path(args.vault).expanduser().resolve()
    source = Path(args.source).expanduser().resolve()
    pages = args.pages or []
    h = update_source(vault, source, pages_produced=pages)
    print(json.dumps({"path": str(source), "content_hash": h}))
    return 0


def cmd_cache_hash(args: argparse.Namespace) -> int:
    from obsidian_wiki.cache import hash_file
    path = Path(args.path).expanduser().resolve()
    if not path.exists():
        print(f"error: {path} does not exist", file=sys.stderr)
        return 1
    print(json.dumps({"path": str(path), "sha256": hash_file(path)}))
    return 0


def cmd_ast_extract(args: argparse.Namespace) -> int:
    from pathlib import Path
    from obsidian_wiki.ast_extractor import extract
    path = Path(args.path).expanduser().resolve()
    try:
        result = extract(path)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.pretty:
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps(result))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    for name in list_skills():
        print(name)
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    bundled = list_skills()
    print(f"obsidian-wiki {__version__}")
    print(f"skills:    {skills_dir()}")
    boot = bootstrap_dir()
    print(f"bootstrap: {boot if boot else '(not found)'}")
    print(f"config:    {GLOBAL_CONFIG}{'' if GLOBAL_CONFIG.exists() else ' (not written yet)'}")
    if GLOBAL_CONFIG.exists():
        vp = _read_config_value("OBSIDIAN_VAULT_PATH")
        setup_ver = _read_config_value("OBSIDIAN_WIKI_VERSION")
        print(f"vault:     {vp or '(unset)'}")
        print(f"setup ran: {setup_ver or '(never)'}")
    print(f"bundled skills: {len(bundled)}")
    print()
    print("Agent skill install status:")
    bundled_set = set(bundled)
    for rel, label, _subset in GLOBAL_AGENT_DIRS:
        agent_dir = HOME / rel
        if not agent_dir.is_dir():
            print(f"  {label}: not installed")
            continue
        installed = {p.name for p in agent_dir.iterdir() if p.is_dir()}
        wiki_installed = installed & bundled_set
        missing = bundled_set - installed
        status = "✅" if not missing else "⚠️ "
        print(f"  {status} {label}: {len(wiki_installed)}/{len(bundled_set)}", end="")
        if missing:
            print(f"  (run: obsidian-wiki setup)", end="")
        print()
    _check_stale()
    return 0


# ── Argument parsing ─────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="obsidian-wiki",
        description="Install the LLM-Wiki agent skills into your AI coding agents.",
    )
    p.add_argument("-V", "--version", action="version", version=f"obsidian-wiki {__version__}")
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("setup", help="install skills into your agents and write config (default)")
    _add_setup_args(sp)
    sp.set_defaults(func=cmd_setup)

    lp = sub.add_parser("list", help="list bundled skills")
    lp.set_defaults(func=cmd_list)

    ip = sub.add_parser("info", help="show install paths, version, and config")
    ip.set_defaults(func=cmd_info)

    bp = sub.add_parser(
        "batch-plan",
        help="split a source directory into parallel-ingest batches, skipping unchanged files",
    )
    bp.add_argument("vault", help="path to the Obsidian vault")
    bp.add_argument("source_dir", help="directory of source documents to ingest")
    bp.add_argument("--max-mb", type=float, default=2.0, help="max MB per batch (default: 2)")
    bp.add_argument("--max-files", type=int, default=20, help="max files per batch (default: 20)")
    bp.add_argument("--no-cache", action="store_true", help="disable manifest-based skip of unchanged files")
    bp.add_argument("--include-code", action="store_true", help="include code files (default: excluded; use ast-extract instead)")
    bp.add_argument("--pretty", action="store_true", help="pretty-print JSON output")
    bp.set_defaults(func=cmd_batch_plan)

    ga = sub.add_parser(
        "graph-analyse",
        help="analyse the vault's wikilink graph: god nodes, communities, surprising connections",
    )
    ga.add_argument("vault", help="path to the Obsidian vault")
    ga.add_argument("--top", type=int, default=20, help="number of top results to return (default: 20)")
    ga.add_argument("--pretty", action="store_true", help="pretty-print JSON output")
    ga.set_defaults(func=cmd_graph_analyse)

    cc = sub.add_parser(
        "cache-check",
        help="check which sources are new/modified/unchanged vs. .manifest.json",
    )
    cc.add_argument("vault", help="path to the Obsidian vault")
    cc.add_argument("sources", nargs="+", help="source file or directory paths to check")
    cc.add_argument("--pretty", action="store_true", help="pretty-print JSON output")
    cc.set_defaults(func=cmd_cache_check)

    cu = sub.add_parser(
        "cache-update",
        help="record a source's current SHA-256 hash in .manifest.json after ingestion",
    )
    cu.add_argument("vault", help="path to the Obsidian vault")
    cu.add_argument("source", help="source file or directory that was just ingested")
    cu.add_argument("--pages", nargs="*", metavar="PAGE", help="vault-relative paths of pages produced")
    cu.set_defaults(func=cmd_cache_update)

    ch = sub.add_parser(
        "cache-hash",
        help="compute the SHA-256 hash of a file or directory (no manifest I/O)",
    )
    ch.add_argument("path", help="file or directory to hash")
    ch.set_defaults(func=cmd_cache_hash)

    ap = sub.add_parser(
        "ast-extract",
        help="extract code structure (classes, functions, imports) from a file or directory — no LLM, no API calls",
    )
    ap.add_argument("path", help="file or directory to extract from")
    ap.add_argument("--pretty", action="store_true", help="pretty-print JSON output")
    ap.set_defaults(func=cmd_ast_extract)

    return p


def _add_setup_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--vault", metavar="PATH", help="absolute path to your Obsidian vault")
    sp.add_argument(
        "--project",
        nargs="?",
        const="",
        default=None,
        metavar="DIR",
        help="also install project-local skills + bootstrap files into DIR "
        "(defaults to the current directory if no DIR given)",
    )
    sp.add_argument(
        "--project-only",
        action="store_true",
        help="skip the global agent install (use with --project)",
    )
    sp.add_argument(
        "--copy",
        action="store_true",
        help="copy skill files instead of symlinking to the installed package",
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    # No subcommand → default to `setup` (the common case).
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help", "-V", "--version")):
        argv = ["setup", *argv]
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    # Warn about stale installs on every command except `setup` (which fixes it)
    # and `info` (which calls _check_stale itself with richer output).
    if getattr(args, "command", None) not in ("setup", "info", None):
        _check_stale()
    try:
        return args.func(args)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
