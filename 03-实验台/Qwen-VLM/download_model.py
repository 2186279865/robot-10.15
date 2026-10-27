"""Download official Ollama model blobs into this project's model store."""
import concurrent.futures
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE = "https://registry.ollama.ai/v2/library/qwen2.5vl"


def curl(url, output, extra=()):
    subprocess.run(["curl.exe", "-sS", "-L", "--fail", "--retry", "5",
                    "--connect-timeout", "20", "--max-time", "600", *extra,
                    "-o", str(output), url], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="3b", choices=("3b", "7b"))
    args = parser.parse_args()
    downloads = ROOT / "downloads"
    blobs = ROOT / "models" / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    manifest_path = downloads / f"model-manifest-{args.tag}.json"
    curl(BASE + "/manifests/" + args.tag, manifest_path)
    manifest = json.loads(manifest_path.read_text())
    for entry in [manifest["config"], *manifest["layers"]]:
        digest, size = entry["digest"], entry["size"]
        output = blobs / digest.replace(":", "-")
        if output.exists() and output.stat().st_size == size:
            continue
        url = BASE + "/blobs/" + digest
        if size < 8 * 1024 * 1024:
            curl(url, output)
        else:
            chunk = 8 * 1024 * 1024
            starts = list(range(0, size, chunk))
            def fetch(start):
                end = min(size - 1, start + chunk - 1)
                part = downloads / f"model-{digest[7:19]}.part{start:012d}"
                if not part.exists() or part.stat().st_size != end - start + 1:
                    curl(url, part, ("-r", f"{start}-{end}"))
                if part.stat().st_size != end - start + 1:
                    raise RuntimeError(f"Wrong range length: {part}")
                return part
            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
                for count, future in enumerate(concurrent.futures.as_completed(
                    [pool.submit(fetch, start) for start in starts]), 1):
                    future.result()
                    print(f"Model chunks: {count}/{len(starts)}", flush=True)
            with output.open("wb") as target:
                for start in starts:
                    with (downloads / f"model-{digest[7:19]}.part{start:012d}").open("rb") as part:
                        while block := part.read(1024 * 1024):
                            target.write(block)
        hasher = hashlib.sha256()
        with output.open("rb") as source:
            while block := source.read(8 * 1024 * 1024):
                hasher.update(block)
        if hasher.hexdigest() != digest[7:]:
            raise RuntimeError(f"SHA256 mismatch: {output}")
        print(f"Verified {digest}", flush=True)
    destination = ROOT / "models/manifests/registry.ollama.ai/library/qwen2.5vl" / args.tag
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(manifest_path.read_bytes())
    print("Model downloaded and SHA256 verified.", flush=True)


if __name__ == "__main__":
    main()
