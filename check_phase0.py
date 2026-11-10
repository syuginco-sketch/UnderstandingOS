from pathlib import Path


REQUIRED_DIRECTORIES = (
    "src",
    "contracts",
    "runtime",
    "app",
    "app/metadata",
    "app/contracts",
    "app/runtime",
    "tests",
    "tools",
    "docs",
)


def validate_structure(root: str | Path) -> list[str]:
    base = Path(root)
    return [name for name in REQUIRED_DIRECTORIES if not (base / name).is_dir()]


if __name__ == "__main__":
    missing = validate_structure(Path(__file__).resolve().parents[1])
    if missing:
        raise SystemExit(f"Missing required directories: {', '.join(missing)}")
    print("Phase 0 repository structure is present.")
