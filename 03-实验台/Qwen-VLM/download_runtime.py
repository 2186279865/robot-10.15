"""Resumable parallel download of the official portable Ollama archive."""
import concurrent.futures
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
URL = "https://github.com/ollama/ollama/releases/download/v0.40.5/ollama-windows-amd64.zip"


def main():
    folder = ROOT / "downloads"
    folder.mkdir(exist_ok=True)
    metadata = subprocess.check_output(
        ["curl.exe", "-sS", "--retry", "5", "-I", "-L", "-H", "Accept: application/octet-stream",
         "--max-time", "60", "-o", "NUL", "-w", "%{json}",
         "https://api.github.com/repos/ollama/ollama/releases/assets/644826943?download=1"], text=True,
    )
    info = json.loads(metadata)
    # Pin the known official release size, and validate each returned range.
    size = 1471226485
    final_url = info["url_effective"]
    chunk_size = 1024 * 1024
    # This NVIDIA machine needs CUDA 13 plus shared runtime and licenses.
    # Omit the much larger alternative CUDA 12 and Vulkan backends.
    ranges = [(0, 820), (914638869, 1448728261), (1460805699, size)]
    starts = sorted({offset for first, last in ranges
                     for offset in range(first // chunk_size * chunk_size, last, chunk_size)})

    def fetch(start):
        end = min(size - 1, start + chunk_size - 1)
        part = folder / f"runtime.part{start:012d}"
        if part.exists() and part.stat().st_size == end - start + 1:
            return part
        for _ in range(5):
            result = subprocess.run(
                ["curl.exe", "-sS", "-L", "--fail", "--connect-timeout", "20",
                 "--max-time", "180", "-r", f"{start}-{end}", "-o", str(part), final_url],
                capture_output=True,
            )
            if result.returncode == 0 and part.stat().st_size == end - start + 1:
                return part
        raise RuntimeError(f"Failed range {start}-{end}; rerun to resume")

    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        for count, future in enumerate(concurrent.futures.as_completed(
            [pool.submit(fetch, start) for start in starts]
        ), 1):
            future.result()
            print(f"Runtime chunks: {count}/{len(starts)}", flush=True)
    archive = folder / "ollama-portable-complete.zip"
    with archive.open("wb") as output:
        output.truncate(size)
        for start in starts:
            output.seek(start)
            with (folder / f"runtime.part{start:012d}").open("rb") as part:
                while block := part.read(1024 * 1024):
                    output.write(block)
    import zipfile
    with zipfile.ZipFile(archive) as package:
        selected = [entry for entry in package.infolist()
                    if not entry.filename.startswith(("lib/ollama/cuda_v12/", "lib/ollama/vulkan/"))]
        for entry in selected:
            package.extract(entry, ROOT / "runtime")
    print("Portable CUDA 13 runtime extracted and ZIP entry CRC verified.", flush=True)


if __name__ == "__main__":
    main()
