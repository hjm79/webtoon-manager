"""Archive execution uses the installed executable and never updates it at runtime."""
from pathlib import Path

app = Path(__file__).resolve().parents[2] / "app"
for path in (app / "scheduler/archive_job.py", app / "api/archive.py", app / "main.py"):
    assert "check_and_update" not in path.read_text(encoding="utf-8"), path
print("Archive paths do not perform executable updates")
