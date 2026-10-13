#!/usr/bin/env python3
"""Yeni gelen jobs/*.json dosyalarını render eder, videoyu 'videolar' release'ine yükler,
sonucu results/<id>.json olarak yazar (n8n bunu okur; JSON2Video yanıtıyla aynı biçim)."""
import json, os, subprocess, base64, time, glob, traceback, urllib.request
import render

REPO = os.environ['REPO']; TOKEN = os.environ['GH_TOKEN']
API = f'https://api.github.com/repos/{REPO}'
TAG = 'videolar'

def gh_api(method, url, data=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(data).encode() if data is not None else None,
                                 headers={'Authorization': f'Bearer {TOKEN}', 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b'{}')

def write_result(pid, movie):
    path = f'results/{pid}.json'
    body = {'message': f'sonuç {pid}', 'content': base64.b64encode(json.dumps({'project': pid, 'movie': movie}, ensure_ascii=False).encode()).decode()}
    for attempt in range(6):
        try:
            try:
                body['sha'] = gh_api('GET', f'{API}/contents/{path}')['sha']
            except Exception:
                body.pop('sha', None)
            gh_api('PUT', f'{API}/contents/{path}', body); return
        except Exception as e:
            print('sonuç yazma tekrar', attempt, e); time.sleep(3 + attempt * 3)

def new_jobs():
    before = os.environ.get('BEFORE', '')
    files = []
    if before and not before.startswith('0000'):
        out = subprocess.run(['git', 'diff', '--name-only', '--diff-filter=AM', before, 'HEAD', '--', 'jobs/'], capture_output=True, text=True).stdout
        files = [f for f in out.split() if f.endswith('.json')]
    if not files:   # elle çalıştırma: sonucu olmayan işler
        files = [f for f in glob.glob('jobs/*.json') if not os.path.exists('results/' + os.path.basename(f))]
    return files

def ensure_release():
    r = subprocess.run(['gh', 'release', 'view', TAG, '--repo', REPO], capture_output=True)
    if r.returncode != 0:
        subprocess.run(['gh', 'release', 'create', TAG, '--repo', REPO, '--title', 'Videolar', '--notes', 'Otomatik render çıktıları'], check=True)

def main():
    jobs = new_jobs()
    print('işler:', jobs)
    if not jobs: return
    ensure_release()
    for f in jobs:
        pid = os.path.splitext(os.path.basename(f))[0]
        try:
            job = json.load(open(f, encoding='utf-8'))
            write_result(pid, {'status': 'running', 'message': 'render ediliyor'})
            out = f'{pid}.mp4'
            t0 = time.time()
            render.render(job.get('movie', job), out)
            subprocess.run(['gh', 'release', 'upload', TAG, out, '--clobber', '--repo', REPO], check=True)
            url = f'https://github.com/{REPO}/releases/download/{TAG}/{out}'
            write_result(pid, {'status': 'done', 'url': url, 'message': f'{time.time() - t0:.0f} sn'})
            print('TAMAM', url)
        except Exception as e:
            traceback.print_exc()
            write_result(pid, {'status': 'error', 'message': str(e)[:300]})

if __name__ == '__main__':
    main()
