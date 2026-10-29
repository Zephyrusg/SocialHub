# Tiny pre-generated test media

- `portrait.jpg`: synthetic blue 800×1000 JPEG, 13,229 bytes.
- `portrait.png`: synthetic blue 800×1000 PNG, approximately 4.5 KiB.
- `reel-h264-3s.mp4`: synthetic black 720×1280 H.264/yuv420p MP4, three seconds, no audio, 5,622 bytes.
- `reel-h264-aac-5s.mp4`: synthetic black 720×1280 H.264/yuv420p, silent stereo AAC at 48 kHz, 25 fps, five seconds, 10,412 bytes. Fast-start MP4 without edit lists; shared Facebook/Instagram Reel fixture.
- `reel-h264-aac-edit-list-5s.mp4`: remux of that same synthetic five-second fixture with edit lists enabled, generated once for normalization regression tests. Video packets are unchanged; normal tests do not encode it.

These files contain no user media. They were generated once during development; normal tests copy these fixtures and run metadata validation only. No encoder or transcoding dependency is needed in runtime or test images. Keep fixtures small, deterministic in meaning and safe to commit. Video tests use the pinned prebuilt static ffprobe copied into both images.
