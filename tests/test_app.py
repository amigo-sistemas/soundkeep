import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import shutil
import subprocess
import sys
import threading

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app as service


client = TestClient(service.app)


@pytest.mark.parametrize(
    ("submitted", "expected_id"),
    [
        ("https://www.youtube.com/watch?v=M7lc1UVf-VE&list=PL123", "M7lc1UVf-VE"),
        ("https://youtu.be/M7lc1UVf-VE?t=3", "M7lc1UVf-VE"),
        ("https://youtube.com/shorts/M7lc1UVf-VE", "M7lc1UVf-VE"),
    ],
)
def test_single_video_links_are_canonicalized(submitted, expected_id):
    canonical, actual_id = service.canonical_video_url(submitted)
    assert actual_id == expected_id
    assert canonical == f"https://www.youtube.com/watch?v={expected_id}"


@pytest.mark.parametrize(
    "submitted",
    [
        "https://youtube.com.evil.test/watch?v=M7lc1UVf-VE",
        "https://youtube.com@evil.test/watch?v=M7lc1UVf-VE",
        "https://www.youtube.com/playlist?list=PL123",
        "https://youtu.be/M7lc1UVf-VE/extra",
        "file:///etc/passwd",
        "https://www.youtube.com/watch?v=M7lc1UVf-VE&v=other_id01",
    ],
)
def test_unsafe_or_non_video_links_are_rejected(submitted):
    with pytest.raises(HTTPException) as error:
        service.canonical_video_url(submitted)
    assert error.value.status_code == 422


def test_filename_cannot_escape_destination():
    assert service.safe_filename('../a\\b:<song>?*') == 'absong.mp3'
    assert service.safe_filename('  ... ') == 'youtube-audio.mp3'
    assert service.safe_filename('CON') == 'audio-CON.mp3'


def test_page_is_served_and_cross_origin_requests_are_rejected():
    page = client.get('/')
    assert page.status_code == 200
    assert 'id="converter"' in page.text
    denied = client.post(
        '/api/inspect',
        headers={'Origin': 'https://unrelated.example'},
        json={'url': 'https://youtu.be/M7lc1UVf-VE'},
    )
    assert denied.status_code == 403


def test_metadata_rejects_live_video(monkeypatch):
    fake = subprocess.CompletedProcess([], 0, '{"id":"M7lc1UVf-VE","title":"Live","is_live":true}', '')
    monkeypatch.setattr(service.subprocess, 'run', lambda *args, **kwargs: fake)
    with pytest.raises(HTTPException) as error:
        service.inspect_video('https://youtu.be/M7lc1UVf-VE')
    assert error.value.status_code == 422


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required for pipeline test')
def test_conversion_streams_a_real_mp3_without_network(monkeypatch):
    video_id = 'M7lc1UVf-VE'
    monkeypatch.setattr(
        service,
        'inspect_video',
        lambda url: {
            'id': video_id,
            'title': 'Synthetic tone',
            'duration': 1,
            'thumbnail': f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg',
            'filename': 'Synthetic tone.mp3',
            'url': f'https://www.youtube.com/watch?v={video_id}',
        },
    )
    monkeypatch.setattr(
        service,
        'download_command',
        lambda url: [
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
            '-i', 'sine=frequency=440:duration=1', '-f', 'wav', '-',
        ],
    )

    response = client.post(
        '/api/convert',
        json={'url': f'https://youtu.be/{video_id}', 'quality': 192},
    )
    assert response.status_code == 200, response.text
    assert response.headers['content-type'].startswith('audio/mpeg')
    assert "Synthetic%20tone.mp3" in response.headers['content-disposition']
    assert response.content.startswith(b'ID3')
    assert len(response.content) > 10_000


def test_invalid_bitrate_is_rejected_without_conversion():
    response = client.post(
        '/api/convert',
        json={'url': 'https://youtu.be/M7lc1UVf-VE', 'quality': 320},
    )
    assert response.status_code == 422


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required for pipeline test')
def test_ytdlp_stdout_and_ffmpeg_work_together_on_local_audio(monkeypatch, tmp_path):
    source = tmp_path / 'tone.wav'
    subprocess.run(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
         '-i', 'sine=frequency=330:duration=1', '-y', str(source)],
        check=True,
    )

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(QuietHandler, directory=str(tmp_path))
    local_server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=local_server.serve_forever, daemon=True)
    thread.start()
    video_id = 'M7lc1UVf-VE'
    monkeypatch.setattr(
        service, 'inspect_video',
        lambda url: {'filename': 'Local tone.mp3', 'url': f'https://www.youtube.com/watch?v={video_id}'},
    )
    monkeypatch.setattr(
        service, 'download_command',
        lambda url: [
            sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-progress',
            '--output', '-', f'http://127.0.0.1:{local_server.server_port}/tone.wav',
        ],
    )
    try:
        response = client.post('/api/convert', json={'url': f'https://youtu.be/{video_id}', 'quality': 128})
    finally:
        local_server.shutdown()
        local_server.server_close()
        thread.join(timeout=3)

    assert response.status_code == 200, response.text
    assert response.content.startswith(b'ID3')
    assert len(response.content) > 10_000
