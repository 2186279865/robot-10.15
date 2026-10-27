"""Resumable official CUDA wheel download with SHA256 verification."""
import concurrent.futures
import hashlib
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent / 'cache/downloads'
URL = 'https://download.pytorch.org/whl/cu128/torch-2.7.1%2Bcu128-cp312-cp312-win_amd64.whl'
SIZE = 3273024349
SHA = '2bb8c05d48ba815b316879a18195d53a6472a03e297d971e916753f8e1053d30'
TARGET = ROOT / 'torch-2.7.1+cu128-cp312-cp312-win_amd64.whl'


def part(index):
    start, end = SIZE*index//8, SIZE*(index+1)//8-1
    path = ROOT/f'torch.part{index}'
    for attempt in range(8):
        offset = path.stat().st_size if path.exists() else 0
        if offset == end-start+1:
            return path
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({'https':'http://127.0.0.1:7897'}))
            request = urllib.request.Request(URL+f'?vision_part={index}', headers={'Range':f'bytes={start+offset}-{end}'})
            with opener.open(request, timeout=60) as response:
                if response.status != 206 or not response.headers['Content-Range'].startswith(f'bytes {start+offset}-{end}/'):
                    raise ValueError('Invalid range response')
                with path.open('ab') as output:
                    while chunk := response.read(1024*1024):
                        output.write(chunk)
            if path.stat().st_size != end-start+1:
                raise ValueError('Incomplete range')
            print(f'part {index} done', flush=True)
            return path
        except Exception as exc:
            print(f'part {index}: retry {attempt}: {exc}', flush=True)
            time.sleep(1)
    raise RuntimeError(f'part {index} failed')


if __name__ == '__main__':
    ROOT.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(part, range(8)))
    digest = hashlib.sha256()
    with TARGET.open('wb') as output:
        for path in paths:
            with path.open('rb') as source:
                while chunk := source.read(8*1024*1024):
                    output.write(chunk)
                    digest.update(chunk)
    if digest.hexdigest() != SHA:
        raise ValueError('CUDA wheel SHA256 mismatch')
    print(f'Verified {TARGET}', flush=True)
