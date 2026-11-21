from pathlib import Path
import re


class SectionManager:
    """Manages recording sections and sequential CSV file allocation."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def get_next_sequence_number(self) -> int:
        max_num = 0
        for path in self.data_dir.glob("*.csv"):
            if path.stem.isdigit():
                num = int(path.stem)
                if num > max_num:
                    max_num = num
        return max_num + 1

    def resolve_section_path(self, section_name: str | None = None) -> Path:
        if section_name is not None:
            stripped = section_name.strip()
            if stripped:
                if stripped.lower().endswith(".csv"):
                    stripped = stripped[:-4].strip()
                clean = re.sub(r"[^\w\- ]+", "_", stripped).strip(" _")
                if clean.lower().endswith(".csv"):
                    clean = clean[:-4].strip(" _")
                if not clean:
                    raise ValueError(
                        f"Invalid section name {section_name!r}: empty after sanitization"
                    )
                base_path = self.data_dir / f"{clean}.csv"
                if not base_path.exists():
                    return base_path

                existing_count = 1
                max_suffix = 0
                prefix = f"{clean}_"
                for path in self.data_dir.glob("*.csv"):
                    stem = path.stem
                    if stem.startswith(prefix):
                        suffix_str = stem[len(prefix) :]
                        if suffix_str.isdigit():
                            existing_count += 1
                            suffix_num = int(suffix_str)
                            if suffix_num > max_suffix:
                                max_suffix = suffix_num

                next_n = max(existing_count, max_suffix + 1)
                while (self.data_dir / f"{clean}_{next_n}.csv").exists():
                    next_n += 1
                return self.data_dir / f"{clean}_{next_n}.csv"

        next_num = self.get_next_sequence_number()
        return self.data_dir / f"{next_num}.csv"
