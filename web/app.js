const $ = (selector) => document.querySelector(selector);

const form = $('#inspect-form');
const urlInput = $('#video-url');
const inspectButton = $('#inspect-button');
const pasteButton = $('#paste-button');
const preview = $('#video-preview');
const thumbnail = $('#video-thumbnail');
const videoTitle = $('#video-title');
const videoDuration = $('#video-duration');
const chooseButton = $('#choose-folder');
const destinationName = $('#destination-name');
const convertButton = $('#convert-button');
const statusBox = $('#status');
const progress = $('#progress');
const progressBar = $('#progress-bar');

const state = { video: null, folder: null, busy: false, abort: null };

function message(text, kind = '') {
  statusBox.hidden = !text;
  statusBox.textContent = text;
  statusBox.className = `status ${kind}`.trim();
}

function setBusy(busy) {
  state.busy = busy;
  urlInput.disabled = busy;
  inspectButton.disabled = busy;
  pasteButton.disabled = busy;
  chooseButton.disabled = busy;
  convertButton.disabled = !busy && !state.video;
  convertButton.innerHTML = busy
    ? 'Cancel conversion <span aria-hidden="true">×</span>'
    : 'Convert &amp; download <span aria-hidden="true">↗</span>';
  $('#converter').setAttribute('aria-busy', String(busy));
}

function durationLabel(seconds) {
  if (seconds == null) return 'Duration unavailable';
  const minutes = Math.floor(seconds / 60);
  const remainder = String(seconds % 60).padStart(2, '0');
  return minutes >= 60
    ? `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, '0')}:${remainder}`
    : `${minutes}:${remainder}`;
}

function readableBytes(bytes) {
  return bytes >= 1024 * 1024
    ? `${(bytes / (1024 * 1024)).toFixed(1)} MB`
    : `${Math.round(bytes / 1024)} KB`;
}

function resetVideo() {
  state.video = null;
  preview.hidden = true;
  convertButton.disabled = true;
  message('');
  progress.hidden = true;
}

async function responseError(response) {
  try {
    const data = await response.json();
    return typeof data.detail === 'string' ? data.detail : 'Please check the link and try again.';
  } catch {
    return 'The server could not complete this request. Please try again.';
  }
}

urlInput.addEventListener('input', resetVideo);

pasteButton.addEventListener('click', async () => {
  try {
    urlInput.value = (await navigator.clipboard.readText()).trim();
    resetVideo();
    urlInput.focus();
  } catch {
    message('Clipboard access is unavailable. Paste the link into the field instead.', 'error');
  }
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const url = urlInput.value.trim();
  if (!url) {
    message('Paste a YouTube video link first.', 'error');
    urlInput.focus();
    return;
  }
  inspectButton.disabled = true;
  inspectButton.innerHTML = 'Finding video…';
  message('Checking this video…');
  try {
    const response = await fetch('/api/inspect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    if (!response.ok) throw new Error(await responseError(response));
    const video = await response.json();
    if (urlInput.value.trim() !== url) return;
    state.video = video;
    thumbnail.src = video.thumbnail;
    videoTitle.textContent = video.title;
    videoDuration.textContent = durationLabel(video.duration);
    preview.hidden = false;
    convertButton.disabled = false;
    message('Video found. Pick a quality and save destination.');
  } catch (error) {
    if (urlInput.value.trim() === url) {
      resetVideo();
      message(error.message || 'This video could not be checked.', 'error');
    }
  } finally {
    inspectButton.disabled = false;
    inspectButton.innerHTML = 'Find video <span aria-hidden="true">→</span>';
  }
});

chooseButton.addEventListener('click', async () => {
  if (!window.showDirectoryPicker) {
    message(window.showSaveFilePicker
      ? 'Your browser will ask where to save the MP3 when you convert.'
      : 'Your browser uses its download settings. Enable “Ask where to save each file” to pick a folder.');
    return;
  }
  try {
    state.folder = await window.showDirectoryPicker({ mode: 'readwrite', startIn: 'music' });
    destinationName.textContent = state.folder.name;
    message(`Files will be saved in ${state.folder.name}.`);
  } catch (error) {
    if (error.name !== 'AbortError') message('The folder could not be opened. Try another folder.', 'error');
  }
});

async function unusedFilename(folder, filename) {
  const stem = filename.slice(0, -4);
  for (let index = 0; index < 100; index += 1) {
    const name = index === 0 ? filename : `${stem} (${index + 1}).mp3`;
    try {
      await folder.getFileHandle(name);
    } catch (error) {
      if (error.name !== 'NotFoundError') throw error;
      return folder.getFileHandle(name, { create: true });
    }
  }
  throw new Error('This folder already has too many copies of that file.');
}

async function saveStream(response, fileHandle) {
  const writable = await fileHandle.createWritable();
  const reader = response.body.getReader();
  const total = Number(response.headers.get('Content-Length')) || 0;
  let received = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      await writable.write(value);
      received += value.byteLength;
      if (total) {
        progress.classList.add('determinate');
        progressBar.style.width = `${Math.min(100, (received / total) * 100)}%`;
        message(`Saving ${readableBytes(received)} of ${readableBytes(total)}…`);
      } else {
        message(`Saving ${readableBytes(received)}…`);
      }
    }
    await writable.close();
  } catch (error) {
    await writable.abort().catch(() => {});
    throw error;
  } finally {
    reader.releaseLock();
  }
}

async function browserDownload(response, filename) {
  message('Preparing your browser download…');
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

convertButton.addEventListener('click', async () => {
  if (state.busy) {
    state.abort?.abort();
    return;
  }
  if (!state.video) return;

  let saveHandle = null;
  const video = state.video;
  try {
    if (window.showDirectoryPicker && !state.folder) {
      state.folder = await window.showDirectoryPicker({ mode: 'readwrite', startIn: 'music' });
      destinationName.textContent = state.folder.name;
    } else if (!state.folder && window.showSaveFilePicker) {
      saveHandle = await window.showSaveFilePicker({
        suggestedName: video.filename,
        types: [{ description: 'MP3 audio', accept: { 'audio/mpeg': ['.mp3'] } }],
      });
    }
  } catch (error) {
    if (error.name !== 'AbortError') message('The save location could not be opened.', 'error');
    return;
  }

  const quality = Number($('input[name="quality"]:checked').value);
  state.abort = new AbortController();
  setBusy(true);
  message('Fetching and converting audio. This can take a few minutes…');
  progress.hidden = false;
  progress.classList.remove('determinate');
  progressBar.style.width = '30%';
  try {
    const response = await fetch('/api/convert', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: video.url, quality }),
      signal: state.abort.signal,
    });
    if (!response.ok) throw new Error(await responseError(response));
    if (state.folder) {
      const fileHandle = await unusedFilename(state.folder, video.filename);
      await saveStream(response, fileHandle);
      message(`${fileHandle.name} saved to ${state.folder.name}.`, 'success');
    } else if (saveHandle) {
      await saveStream(response, saveHandle);
      message(`${saveHandle.name} saved.`, 'success');
    } else {
      await browserDownload(response, video.filename);
      message(`${video.filename} sent to your browser downloads.`, 'success');
    }
  } catch (error) {
    message(error.name === 'AbortError' ? 'Conversion cancelled.' : (error.message || 'Conversion failed.'), 'error');
  } finally {
    state.abort = null;
    progress.hidden = true;
    setBusy(false);
  }
});

if (!window.showDirectoryPicker) {
  destinationName.textContent = window.showSaveFilePicker
    ? 'Choose a file location when converting'
    : 'Your browser download location';
  chooseButton.textContent = 'How?';
}
