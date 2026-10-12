# Fixing IP blocks

Soundkeep currently runs yt-dlp on Vercel. YouTube therefore sees Vercel's outbound IP, not the customer's IP. If YouTube blocks that address, inspection and conversion can fail even when the customer can watch the video normally.

This document covers two possible approaches:

1. Route backend requests through a rotating proxy.
2. Move extraction and downloading to the customer's device so requests use their IP.

## Current failure and diagnostic gap

The deployed `/api/inspect` endpoint returned HTTP 422 for both `ykxfJ70moV8` and a public control video. Both requests succeeded locally with the repository's pinned dependencies. The deployed `/api/health` endpoint returned `{"ready": true}`.

An IP block is the leading explanation, but it is not confirmed. In `app.py`, `inspect_video` captures yt-dlp's stderr and discards it. Every nonzero exit becomes the same generic "video unavailable" response. The health check confirms that dependencies are present, not that extraction works.

Before implementing either approach, log yt-dlp's exit code and stderr on the server, redeploy, and repeat the failing request. Keep diagnostic details out of the public API response and redact secrets if proxy authentication is added. A missing runtime, extractor failure, or required PO Token needs a different fix from an IP block.

## Option 1: rotating proxy on the backend

Keep the existing FastAPI, yt-dlp, and FFmpeg pipeline. Route YouTube requests through a proxy provider instead of directly from Vercel.

```text
Customer browser
  → Vercel
    → selected proxy IP
      → YouTube
```

YouTube sees the proxy's IP. Vercel still performs the extraction, download, and MP3 conversion.

### Implementation direction

- Start by testing one working proxy through yt-dlp's `--proxy` option. Validate the approach before adding a rotating pool.
- Store the proxy configuration and credentials server-side, such as in a `YTDLP_PROXY` environment variable. Do not expose them in frontend assets or accept arbitrary proxy URLs from customers.
- Apply the proxy to both `inspect_video` and `download_command`. Proxying metadata requests alone leaves media downloads exposed to the server-IP restriction.
- Use a sticky session so one conversion keeps the same outbound IP throughout metadata extraction and media downloading. `/api/convert` performs its own inspection before downloading, so both stages must use that session.
- Rotate between jobs or after a confirmed IP-related failure, not between individual requests within a job. Keep retries bounded and avoid retrying unavailable or restricted videos through the entire pool.
- Keep request rates conservative and measure success rate, latency, and proxy bandwidth costs.

### Trade-offs

This requires less application change than moving yt-dlp to the client and preserves the normal website experience. However, proxy addresses can also be blocked. Rotation does not guarantee access and does not replace required JavaScript support or PO Tokens.

Media downloads consume proxy bandwidth, which may become a significant operating cost. The proxy provider also handles the YouTube traffic, so provider trust and credential handling matter. Adding customers' personal YouTube account cookies should not be the default workaround.

## Option 2: move yt-dlp to the customer's device

Move YouTube extraction and downloading off Vercel. The customer's device makes the requests, so YouTube sees their outbound IP.

```text
Customer device
  → YouTube
  → local audio conversion and save

Vercel serves the website, not the YouTube download.
```

This removes dependence on Vercel's shared outbound IP, but it is not a drop-in frontend change. yt-dlp is a Python command-line application. The current implementation also uses Node.js for JavaScript challenges and native FFmpeg for conversion.

### Possible implementations

| Client approach | Feasibility |
| --- | --- |
| Ordinary webpage | Requires substantial extraction and conversion work. Browser CORS restrictions prevent normal page scripts from reading the YouTube pages needed by the current extractor. |
| Browser extension | Can request cross-origin access with explicit permissions. It still needs a compatible extractor or a local companion to run the existing Python yt-dlp. |
| Local companion or desktop app | Can run yt-dlp and FFmpeg directly using the customer's network. Requires installation and a secure way for the website to start local jobs. |
| Python and FFmpeg through WebAssembly | An experimental browser approach. Packaging the runtimes does not remove CORS restrictions, and large conversions introduce memory and performance constraints. |

The most direct way to preserve the existing yt-dlp behavior is a local companion or desktop app. A browser-only implementation would be a separate technical project, not just moving `app.py` into the frontend.

Using a Vercel relay to fetch YouTube pages for the browser would reintroduce the same server-IP problem. Passing the customer's IP in `X-Forwarded-For` does not change the source IP of Vercel's connection. Embedded playback uses the customer's IP, but does not expose downloadable audio to the application.

### Trade-offs

Client-side downloading avoids server proxy bandwidth costs and spreads requests across customers' networks. Their IPs can still be blocked, and regional or account restrictions can still apply.

The cost shifts to installation, permissions, browser and operating-system support, local resource use, and maintaining the client extractor. A local companion must not accept arbitrary shell commands or let unrelated websites trigger downloads.

## Comparison

| Consideration | Rotating backend proxy | Client-side extraction |
| --- | --- | --- |
| IP seen by YouTube | Proxy IP | Customer's outbound IP |
| Reuse of current pipeline | Mostly unchanged | Straightforward in a local app; substantial work in a normal webpage |
| Customer installation | None | Likely for an extension or local companion |
| Operating cost | Server compute and proxy bandwidth | Lower server download costs; more client-support work |
| Main risk | Proxy blocks, session consistency, and cost | Browser restrictions, packaging, and compatibility |

## Recommended sequence

Confirm the actual yt-dlp error first. If it is an IP block, test a single stable proxy before implementing rotation. Use successful inspection and an actual permitted audio conversion from Vercel as the acceptance checks, not `/api/health` alone.

Treat client-side yt-dlp as a separate product decision. It makes sense if avoiding server download traffic is worth requiring an extension or local application. It is not a quick fix for the existing installation-free website.

Fixing the IP block does not remove the current backend's 240-second conversion timeout or 250 MiB output cap. The reported video is 10 hours long; a full MP3 at the lowest supported 128 kbps would be roughly 549 MiB, exceeding that cap even if inspection succeeds. Moving conversion itself to the client would avoid these server limits, but local memory use and conversion time would need separate testing.

## References

- [yt-dlp known issues, including logged-out IP blocks](https://github.com/yt-dlp/yt-dlp/issues/3766)
- [yt-dlp FAQ on matching download IPs, cookies, and headers](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#i-extracted-a-video-url-but-it-does-not-play-on-another-machine--in-my-web-browser)
- [YouTube extractor guidance and PO Tokens](https://github.com/yt-dlp/yt-dlp/wiki/Extractors)
