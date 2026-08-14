"""ZYROX MUSIC ENHANCE — Flask server. All audio processing runs here."""
import os
import shutil
import tempfile
import threading
import time
import traceback

from flask import Flask, jsonify, request, send_file

import processor

app = Flask(__name__, static_folder='static', static_url_path='')
app.config['MAX_CONTENT_LENGTH'] = 300 * 1024 * 1024   # 300 MB upload cap


def _schedule_cleanup(tmpdir, delay=60):
    """Guaranteed wipe: force-delete the temp folder shortly after the response
    is sent (works even where WSGI close-callbacks are never invoked)."""
    def wipe():
        shutil.rmtree(tmpdir, ignore_errors=True)
    t = threading.Timer(delay, wipe)
    t.daemon = True
    t.start()


@app.get('/')
def index():
    return app.send_static_file('index.html')


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
    try:
        ext = os.path.splitext(f.filename)[1].lower() or '.bin'
        inp = os.path.join(tmpdir, 'input' + ext)
        f.save(inp)

        out_path, dname, mime = processor.process(tmpdir, inp, tool, params)

        resp = send_file(out_path, as_attachment=True,
                         download_name=dname, mimetype=mime)
        resp.call_on_close(lambda: shutil.rmtree(tmpdir, ignore_errors=True))
        _schedule_cleanup(tmpdir)
        return resp
    except processor.ProcessError as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify(error=str(e)), 422
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify(error='Processing failed: ' + str(e)), 500


@app.errorhandler(413)
def too_large(_e):
    return jsonify(error='File too big — max upload is 300 MB.'), 413


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)))
