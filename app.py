"""ZYROX MUSIC ENHANCE — Flask server. All audio processing runs here."""
import os
import shutil
import tempfile
import threading
import time
import traceback
import urllib.request
import uuid

from flask import Flask, jsonify, request, send_file

import processor

app = Flask(__name__, static_folder='static', static_url_path='')
app.config['MAX_CONTENT_LENGTH'] = 300 * 1024 * 1024   # 300 MB upload cap

# ---- free-tier keep-alive: ping our own public URL so Render never sleeps
#      the instance (sleeping = visitors see a long "starting up" page) ----
KEEPALIVE_URL = os.environ.get('KEEPALIVE_URL') or \
    'https://zyrox-music-enhance.onrender.com/api/health'


def _start_keepalive():
    def ping():
        try:
            urllib.request.urlopen(KEEPALIVE_URL, timeout=15)
        except Exception:
            pass
        threading.Timer(600, ping).start()   # every 10 min

    threading.Timer(45, ping).start()        # first ping 45s after boot


if os.environ.get('DISABLE_KEEPALIVE') != '1':
    _start_keepalive()

# ---- job manager (upload → background processing → poll → download) ----
JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_TTL = 30 * 60   # wipe finished jobs (and their files) after 30 min


def _expire_job(job_id):
    with JOBS_LOCK:
        job = JOBS.pop(job_id, None)
    if job:
        shutil.rmtree(job['tmpdir'], ignore_errors=True)


def _run_job(job_id):
    job = JOBS.get(job_id)
    if not job:
        return
    job.update(status='processing')
    try:
        out_path, dname, mime = processor.process(
            job['tmpdir'], job['inp'], job['tool'], job['params'],
            prog=lambda pct: job.update(progress=round(pct * 100, 1)),
            cancel=lambda: job.get('cancelled', False))
        job.update(status='done', progress=100, out_path=out_path,
                   dname=dname, mime=mime)
    except processor.Cancelled:
        job.update(status='cancelled', progress=job.get('progress', 0))
        shutil.rmtree(job['tmpdir'], ignore_errors=True)
    except processor.ProcessError as e:
        job.update(status='error', error=str(e))
        shutil.rmtree(job['tmpdir'], ignore_errors=True)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        job.update(status='error', error='Processing failed: ' + str(e))
        shutil.rmtree(job['tmpdir'], ignore_errors=True)
    threading.Timer(JOB_TTL, lambda: _expire_job(job_id)).start()


@app.get('/')
def index():
    resp = app.send_static_file('index.html')
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp


@app.get('/api/health')
def health():
    return jsonify(status='ok', app='zyrox-music-enhance', ts=int(time.time()))


@app.post('/api/process')
def process():
    f = request.files.get('file')
    tool = request.form.get('tool', '')
    if f is None or f.filename == '':
        return jsonify(error='No file uploaded.'), 400
    if tool not in processor.TOOLS:
        return jsonify(error='Unknown tool.'), 400

    params = {}
    for k in ('bass', 'preset', 'wet', 'slow', 'start_pct', 'end_pct',
              'clarity', 'loud', 'speed', 'depth', 'song_type', 'bands'):
        v = request.form.get(k)
        if v is not None:
            params[k] = v

    tmpdir = tempfile.mkdtemp(prefix='zyrox_')
    ext = os.path.splitext(f.filename)[1].lower() or '.bin'
    inp = os.path.join(tmpdir, 'input' + ext)
    f.save(inp)

    job_id = uuid.uuid4().hex[:16]
    with JOBS_LOCK:
        JOBS[job_id] = {
            'id': job_id, 'status': 'queued', 'progress': 0,
            'tool': tool, 'params': params, 'tmpdir': tmpdir, 'inp': inp,
            'out_path': None, 'dname': None, 'mime': None,
            'error': None, 'cancelled': False,
        }
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return jsonify(job_id=job_id)


@app.get('/api/status/<job_id>')
def status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(status='missing'), 404
    return jsonify(status=job['status'], progress=job['progress'],
                   error=job.get('error'))


@app.post('/api/cancel/<job_id>')
def cancel(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error='Job not found.'), 404
    job['cancelled'] = True
    return jsonify(status='cancelling')


@app.get('/api/result/<job_id>')
def result(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error='Job expired — please process again.'), 404
    if job['status'] == 'error':
        return jsonify(error=job.get('error') or 'Processing failed.'), 422
    if job['status'] == 'cancelled':
        return jsonify(error='Processing was cancelled.'), 409
    if job['status'] != 'done':
        return jsonify(error='Still processing…'), 409

    resp = send_file(job['out_path'], as_attachment=True,
                     download_name=job['dname'], mimetype=job['mime'])
    tmpdir = job['tmpdir']
    resp.call_on_close(lambda: shutil.rmtree(tmpdir, ignore_errors=True))
    threading.Timer(120, lambda: shutil.rmtree(tmpdir, ignore_errors=True)).start()
    with JOBS_LOCK:
        JOBS.pop(job_id, None)
    return resp


@app.errorhandler(413)
def too_large(_e):
    return jsonify(error='File too big — max upload is 300 MB.'), 413


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)))
